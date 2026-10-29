"""Paste-anything ingestion: deterministic parsers first, a model only for what they can't map.

Handles spreadsheet ranges (tab-separated, header detected), Bloomberg Excel output (``#N/A`` cells,
``AAPL US Equity`` tickers, field mnemonics kept as data) and desk-shorthand chat lines
("250k ABC, 75k XYZ @ 25bps"). Every cell carries a confidence for the preview grid's shading.
Lines nothing here understands come back as ``unparsed``: that's the hand-off point to the parser model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from refdata import identifiers as ids
from refdata.universe import Instrument, resolve

from ..secmaster import id_shape

QTY_HEADERS = re.compile(r"^(qty|quantity|shares|shs|amount|size|req(uest(ed)?)?|locate|notional_?shs)", re.I)
FEE_HEADERS = re.compile(r"^(fee|rate|bps|fee_?bps|rebate|px_fee)", re.I)
CP_HEADERS = re.compile(r"^(cp|counterparty|client|requester|account)", re.I)
ID_HEADERS = re.compile(r"^(id|identifier|security|ticker|isin|sedol|cusip|figi|symbol|name|instrument)", re.I)
NA_CELL = re.compile(r"^#N/A\b|^#VALUE!|^#REF!|^N\.?A\.?$", re.I)
QTY_TOKEN = re.compile(r"^(\d+(?:[.,]\d+)?)(k|m|mm|mln)?$", re.I)
FEE_TOKEN = re.compile(r"@\s*(\d+(?:\.\d+)?)\s*(?:bps?|bp)?", re.I)
EXCH = re.compile(r"^[A-Z]{2}$")
NOISE = {"PLS", "PLEASE", "THX", "THANKS", "LOC", "LOCATE", "CAN", "U", "YOU", "NEED", "BORROW", "HI", "AND", "FOR",
         "SHS", "SHARES", "OF", "IN", "ON", "ME", "GET", "TY", "PL", "QTY", "EQUITY", "OPEN", "BPS", "BP"}


@dataclass
class Row:
    row_no: int
    identifier: str | None = None
    instrument: Instrument | None = None
    quantity: int | None = None
    fee_bps: float | None = None
    counterparty: str | None = None
    confidence: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_json(self) -> dict[str, Any]:
        i = self.instrument
        return {"rowNo": self.row_no, "identifier": self.identifier, "quantity": self.quantity, "feeBps": self.fee_bps,
                "counterparty": self.counterparty, "confidence": self.confidence, "error": self.error,
                "extra": self.extra, "source": self.source,
                "instrument": {"id": i.id, "composite": i.composite, "name": i.name, "isin": i.isin,
                               "ccy": i.ccy} if i else None}


@dataclass
class Parsed:
    kind: str
    rows: list[Row]
    columns: list[dict[str, Any]] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


def parse_qty(tok: str) -> int | None:
    t = tok.strip().replace(" ", "")
    if "," in t and re.fullmatch(r"\d{1,3}(,\d{3})+", t):
        t = t.replace(",", "")
    m = QTY_TOKEN.match(t)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    mult = {"k": 1e3, "m": 1e6, "mm": 1e6, "mln": 1e6}.get((m.group(2) or "").lower(), 1)
    return int(round(n * mult))


def resolve_id(tok: str) -> tuple[Instrument | None, float, str | None]:
    """(instrument, confidence, error). Check-digit-valid identifiers score highest."""
    t = " ".join(tok.strip().upper().split())
    t = re.sub(r"\s+EQUITY$", " EQUITY", t)
    if not t:
        return None, 0.0, None
    kind = ids.detect(t)
    if kind:
        inst = resolve(t)
        return (inst, 1.0, None) if inst else (None, 0.3, f"valid {kind} not in the security master")
    if id_shape(t) in ("ISIN", "CUSIP", "SEDOL") and any(c.isdigit() for c in t[:-1]):
        hints = [c for c in ids.suggest_fix(t) if resolve(c)]
        return None, 0.2, "bad check digit" + (f"; did you mean {hints[0]}?" if hints else "")
    inst = resolve(t) or (resolve(f"{t} US") if " " not in t else None)
    return (inst, 0.9 if " " in t else 0.75, None) if inst else (None, 0.0, None)


def _delimited(line: str) -> bool:
    return "\t" in line or "|" in line


def strip_preamble(text: str) -> str:
    """Drop a sentence or two ("Morning, locates for today please:") above a pasted table."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    first = next((i for i, ln in enumerate(lines) if _delimited(ln)), None)
    if first and first <= 2 and all(_delimited(ln) for ln in lines[first:]):
        return "\n".join(lines[first:])
    return text


def detect_kind(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "EMPTY"
    sheet = len(lines) >= 2 or _delimited(lines[0]) or lines[0].count(",") >= 2
    if "#N/A" in text or "PX_LAST" in text.upper() or (
            sheet and re.search(r"\b[A-Z0-9./]+ [A-Z]{2} Equity\b", text, re.I)):
        return "BLOOMBERG"
    tabbed = sum(1 for ln in lines if _delimited(ln))
    if tabbed >= max(1, len(lines) // 2):
        return "TABLE"
    if len(lines) >= 2 and sum(1 for ln in lines if ln.count(",") >= 2) >= len(lines) - 1:
        return "TABLE"  # CSV
    return "CHAT"


def parse(text: str) -> Parsed:
    text = strip_preamble(text)
    kind = detect_kind(text)
    if kind == "EMPTY":
        return Parsed(kind, [])
    if kind == "CHAT":
        return _parse_chat(text)
    return _parse_table(text, kind)


# -- tables --------------------------------------------------------------------------------------
def _split(line: str) -> list[str]:
    sep = "\t" if "\t" in line else "|" if "|" in line else ","
    return [c.strip().strip('"') for c in line.split(sep)]


def _parse_table(text: str, kind: str) -> Parsed:
    grid = [_split(ln) for ln in text.splitlines() if ln.strip()]
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    if kind == "BLOOMBERG":
        grid = [[("" if NA_CELL.match(c) else c) for c in r] for r in grid]
    header = _is_header(grid[0])
    names = grid[0] if header else [f"col{i + 1}" for i in range(width)]
    body = grid[1:] if header else grid
    cols = _map_columns(names, body)
    rows: list[Row] = []
    for n, r in enumerate(body, start=1):
        row = Row(row_no=n, source="\t".join(r))
        if cols["id"] is not None:
            row.identifier = r[cols["id"]] or None
            row.instrument, row.confidence["identifier"], row.error = resolve_id(r[cols["id"]])
        if cols["qty"] is not None:
            row.quantity = parse_qty(r[cols["qty"]])
            row.confidence["quantity"] = (1.0 if cols["qty_by_header"] else 0.7) if row.quantity else 0.0
        if cols["fee"] is not None:
            try:
                row.fee_bps = float(r[cols["fee"]].lower().replace("bps", "").strip())
                row.confidence["feeBps"] = 1.0
            except ValueError:
                pass
        if cols["cp"] is not None and r[cols["cp"]]:
            row.counterparty = r[cols["cp"]].upper().replace("-", "")
            row.confidence["counterparty"] = 0.9
        used = {cols[k] for k in ("id", "qty", "fee", "cp")}
        row.extra = {names[i]: r[i] for i in range(width) if i not in used and r[i]}
        if row.instrument is None and not row.error:
            row.error = "no identifier recognised"
        rows.append(row)
    columns = [{"index": i, "name": names[i], "role": next((k for k in ("id", "qty", "fee", "cp") if cols[k] == i),
                                                           "data")} for i in range(width)]
    return Parsed(kind, rows, columns)


def _is_header(cells: list[str]) -> bool:
    filled = [c for c in cells if c]
    if not filled:
        return False
    looks_data = sum(1 for c in filled if parse_qty(c) is not None or ids.detect(c) or resolve(c))
    return looks_data <= len(filled) // 3


def _map_columns(names: list[str], body: list[list[str]]) -> dict[str, Any]:
    width = len(names)
    out: dict[str, Any] = {"id": None, "qty": None, "fee": None, "cp": None, "qty_by_header": False}
    for i, name in enumerate(names):
        key = name.strip().replace(" ", "_")
        if out["qty"] is None and QTY_HEADERS.match(key):
            out["qty"], out["qty_by_header"] = i, True
        elif out["fee"] is None and FEE_HEADERS.match(key):
            out["fee"] = i
        elif out["cp"] is None and CP_HEADERS.match(key):
            out["cp"] = i
    # The identifier column is the one whose cells resolve most often (a column of check-digit-valid 12-char
    # strings is ISIN, whatever its header says).
    sample = body[:50]
    best, best_hits = None, 0
    for i in range(width):
        if i in (out["qty"], out["fee"], out["cp"]):
            continue
        hits = sum(1 for r in sample if r[i] and resolve_id(r[i])[0] is not None)
        bonus = 0.5 if ID_HEADERS.match(names[i].strip()) else 0
        if hits + bonus > best_hits:
            best, best_hits = i, hits + bonus
    out["id"] = best
    if out["qty"] is None:  # fall back to the numeric column with the largest values
        cands = []
        for i in range(width):
            if i in (out["id"], out["fee"], out["cp"]):
                continue
            vals = [parse_qty(r[i]) for r in sample if r[i]]
            vals = [v for v in vals if v is not None]
            if vals and len(vals) >= len(sample) // 2:
                cands.append((sum(vals) / len(vals), i))
        if cands:
            out["qty"] = max(cands)[1]
    return out


# -- chat shorthand ----------------------------------------------------------------------------
def _parse_chat(text: str) -> Parsed:
    rows: list[Row] = []
    unparsed: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        found = _chat_line(line, len(rows))
        if found:
            rows.extend(found)
        else:
            unparsed.append(line)
    return Parsed("CHAT", rows, [], unparsed)


def _chat_line(line: str, offset: int) -> list[Row]:
    fee_m = FEE_TOKEN.search(line)
    fee = float(fee_m.group(1)) if fee_m else None
    clean = re.sub(r"(?<=\d),(?=\d{3}\b)", "", FEE_TOKEN.sub(" ", line))  # "500,000" is one number
    # "/" joins names ("TTWO 65k / 45k REGN", "bid/offer") but is part of a ticker before an exchange ("BP/ LN")
    # or a share class ("BRK/B", "BT/A LN").
    toks = [t.strip("?!.:()~") for t in re.split(r"[\s,;]+|(?<=\s)/|/(?=[^\s/]{2,})",
                                                 re.sub(r"\bX\b", " ", clean.upper()))]
    toks = [t for t in toks if t]  # "250k x ABC", "PWR LN?"
    out: list[Row] = []
    i, pending_qty = 0, None
    while i < len(toks):
        t = toks[i]
        q = parse_qty(t)
        q = q if q is None or q >= 100 else None  # "basket goes out at 3" is a time, not 3 shares
        numeric_ticker = q is not None and i + 1 < len(toks) and EXCH.match(toks[i + 1]) and bool(
            resolve_id(f"{t} {toks[i + 1]}")[0])  # "3479 JT"
        if q is not None and not ids.detect(t) and not numeric_ticker:
            pending_qty = q
            i += 1
            continue
        # Try "TICK XX" first (even for a word like CAN: "CAN LN" is a stock), then the bare token.
        pair = f"{t} {toks[i + 1]}" if i + 1 < len(toks) and EXCH.match(toks[i + 1]) else None
        inst, conf, err = resolve_id(pair) if pair else (None, 0.0, None)
        if inst is None and t in NOISE:
            i += 1
            continue
        step = 2 if inst else 1
        if inst is None:
            inst, conf, err = resolve_id(t)
        if inst is None and err is None:
            i += 1
            continue
        qty = pending_qty
        if qty is None and i + step < len(toks):  # "ABC 250k" order
            q2 = parse_qty(toks[i + step])
            if q2 is not None:
                qty, step = q2, step + 1
        out.append(Row(row_no=offset + len(out) + 1, identifier=pair if inst and pair and step >= 2 else t,
                       instrument=inst, quantity=qty, fee_bps=fee, error=err, source=line.strip(),
                       confidence={"identifier": conf, "quantity": 0.9 if qty else 0.0,
                                   **({"feeBps": 0.9} if fee is not None else {})}))
        pending_qty = None
        i += step
    if pending_qty is not None and out and out[-1].quantity is None:  # "show PWR LN? looking for 550k"
        out[-1].quantity, out[-1].confidence["quantity"] = pending_qty, 0.8
    if any(r.quantity for r in out):  # words that happen to be tickers ("can SL cover 1m NANO LN")
        out = [r for r in out if r.quantity]
        for n, r in enumerate(out):
            r.row_no = offset + n + 1
    return out
