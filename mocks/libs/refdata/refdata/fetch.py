"""``refdata fetch``: build the public-data snapshot the instrument universe is built from.

Run by a developer, never by a mock at runtime: the result is committed as
``refdata/data/public_universe.json.gz``, so every service still builds the same universe offline.

    uv run --extra fetch refdata fetch [--out refdata/data/public_universe.json.gz]

Sources, all free and without an account:

* Yahoo Finance screener (yfinance): which names trade where, sector, currency, last close, shares
  outstanding, 3-month average volume. Per market and sector, the largest names plus an even spread
  of smaller ones, so the universe has small caps (where lending gets special) as well as mega caps.
* Yahoo daily closes (yfinance): six months of history for each name's realised volatility.
* OpenFIGI mapping API: FIGI, composite and share-class FIGI, Bloomberg-style ticker, security type.
  Names already in the previous snapshot reuse its FIGIs; set ``OPENFIGI_API_KEY`` (free) to map the
  rest at ~250 a minute instead of ~25.
* Yahoo's company profile, only for names listed in two of our markets: the home market keeps them.

CUSIP, ISIN and SEDOL are licensed (CGS, LSEG), so the snapshot has none; the universe generates
fictitious ones (see identifiers.py). Yahoo data is for personal use under Yahoo's terms: the
snapshot keeps only rounded aggregates, and the source is recorded in its header.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_OUT = Path(__file__).parent / "data" / "public_universe.json.gz"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


@dataclass(frozen=True)
class Venue:
    country: str
    region: str  # Yahoo screener region
    exchanges: tuple[str, ...]  # Yahoo exchange codes
    suffix: str  # Yahoo symbol suffix
    exch_code: str  # Bloomberg-style composite code (OpenFIGI exchCode)
    ccy: str
    count: int
    min_mcap: float  # in local currency


VENUES: tuple[Venue, ...] = (
    Venue("US", "us", ("NMS", "NYQ", "NGM", "NCM", "ASE"), "", "US", "USD", 870, 3e8),
    Venue("GB", "gb", ("LSE",), ".L", "LN", "GBP", 400, 1.5e8),
    Venue("DE", "de", ("GER",), ".DE", "GY", "EUR", 140, 2e8),
    Venue("FR", "fr", ("PAR",), ".PA", "FP", "EUR", 140, 2e8),
    Venue("NL", "nl", ("AMS",), ".AS", "NA", "EUR", 60, 1e8),
    Venue("JP", "jp", ("JPX",), ".T", "JT", "JPY", 300, 3e10),
)

# Yahoo exchange code -> ISO 10383 MIC.
MICS = {"NMS": "XNAS", "NGM": "XNAS", "NCM": "XNAS", "NYQ": "XNYS", "ASE": "XASE", "PCX": "ARCX", "BTS": "BATS",
        "LSE": "XLON", "GER": "XETR", "PAR": "XPAR", "AMS": "XAMS", "JPX": "XTKS"}

# Yahoo sectors -> the GICS names the rest of refdata uses.
SECTORS = {
    "Technology": "Information Technology",
    "Healthcare": "Health Care",
    "Financial Services": "Financials",
    "Consumer Cyclical": "Consumer Discretionary",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Consumer Defensive": "Consumer Staples",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Basic Materials": "Materials",
}

# Widely held US ETFs: the screener only covers operating companies.
US_ETFS = ("SPY", "QQQ", "IWM", "DIA", "VTI", "EEM", "EFA", "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP",
           "XLU", "XLB", "KRE", "XBI", "SMH", "ARKK", "TLT", "HYG", "LQD", "GLD", "SLV", "GDX", "XOP", "IYR",
           "SOXX", "KWEB")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# -- Yahoo ---------------------------------------------------------------------------------------
def _screen(v: Venue, sector: str) -> list[dict[str, Any]]:
    import yfinance as yf
    from yfinance import EquityQuery as Q

    exch = Q("is-in", ["exchange", *v.exchanges]) if len(v.exchanges) > 1 else Q("eq", ["exchange", v.exchanges[0]])
    query = Q("and", [Q("eq", ["region", v.region]), exch, Q("eq", ["sector", sector]),
                      Q("gt", ["intradaymarketcap", v.min_mcap]), Q("gt", ["avgdailyvol3m", 0])])
    for attempt in range(4):
        try:
            return yf.screen(query, size=250, sortField="intradaymarketcap", sortAsc=False).get("quotes", [])
        except Exception as e:  # rate limits and transient errors
            _log(f"  screen {v.country}/{sector} failed ({e}); retrying")
            time.sleep(5 * (attempt + 1))
    return []


def _norm_name(name: str) -> str:
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    drop = {"inc", "corp", "corporation", "plc", "ag", "se", "sa", "nv", "co", "ltd", "the", "group", "holdings",
            "holding", "company", "limited", "class", "a", "b", "s", "n", "v"}
    return " ".join(w for w in name.split() if w not in drop)


def _pick(quotes: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """The largest 60% by market cap, then an even spread over the rest, so small caps make it in."""
    quotes = sorted(quotes, key=lambda q: -(q.get("marketCap") or 0))
    head = quotes[: int(count * 0.6)]
    rest = quotes[len(head):]
    need = count - len(head)
    if need <= 0 or not rest:
        return head
    step = max(len(rest) / need, 1.0)
    return head + [rest[int(i * step)] for i in range(min(need, len(rest)))]


# Yahoo's "country" (domicile) -> our market.
DOMICILES = {"United States": "US", "United Kingdom": "GB", "Germany": "DE", "France": "FR", "Netherlands": "NL",
             "Japan": "JP"}


def _domicile(symbol: str) -> str | None:
    import yfinance as yf

    try:
        return DOMICILES.get(yf.Ticker(symbol).info.get("country", ""))
    except Exception as e:
        _log(f"  domicile of {symbol}: {e}")
        return None


def _candidates() -> list[dict[str, Any]]:
    pools: dict[str, list[dict[str, Any]]] = {}
    for v in VENUES:
        best: dict[str, dict[str, Any]] = {}  # one line per company (GOOGL, not GOOG too)
        for ysector, sector in SECTORS.items():
            for q in _screen(v, ysector):
                if q.get("quoteType") != "EQUITY" or not q.get("regularMarketPrice") or not q.get("sharesOutstanding"):
                    continue
                if (q.get("currency") or "").upper() != v.ccy:  # foreign listings quoted in their home currency
                    continue
                key = _norm_name(q.get("longName") or q.get("shortName") or q["symbol"])
                if key and (key not in best or (q.get("marketCap") or 0) > (best[key].get("marketCap") or 0)):
                    best[key] = {**q, "_sector": sector, "_venue": v, "_key": key}
            time.sleep(0.5)
        pools[v.country] = list(best.values())

    # A company listed in two of our markets (Shell in London and New York, NVIDIA in New York and on
    # Xetra) stays in its home market only. Ask Yahoo for the domicile of just those.
    markets: dict[str, list[dict[str, Any]]] = {}
    for qs in pools.values():
        for q in qs:
            markets.setdefault(q["_key"], []).append(q)
    dups = {k: qs for k, qs in markets.items() if len(qs) > 1}
    _log(f"{len(dups)} companies listed in more than one market; looking up their domicile")
    drop: set[int] = set()
    for qs in dups.values():
        home = _domicile(max(qs, key=lambda q: q.get("marketCap") or 0)["symbol"])
        listed = [q["_venue"].country for q in qs]
        keep = home if home in listed else listed[0]  # domiciled elsewhere (Ireland, say): first in VENUES order
        drop.update(id(q) for q in qs if q["_venue"].country != keep)

    out: list[dict[str, Any]] = []
    for v in VENUES:
        pool = [q for q in pools[v.country] if id(q) not in drop]
        picked = _pick(pool, v.count)
        _log(f"{v.country}: {len(pool)} candidates, picked {len(picked)}")
        out.extend(picked)
    return out


def _etfs() -> list[dict[str, Any]]:
    import yfinance as yf

    us = VENUES[0]
    out = []
    for sym in US_ETFS:
        try:
            info = yf.Ticker(sym).info
        except Exception as e:
            _log(f"  ETF {sym}: {e}")
            continue
        px = info.get("regularMarketPrice") or info.get("previousClose")
        if not px:
            continue
        out.append({"symbol": sym, "longName": info.get("longName") or sym, "exchange": info.get("exchange", "PCX"),
                    "currency": "USD", "regularMarketPrice": px, "sharesOutstanding": info.get("sharesOutstanding"),
                    "averageDailyVolume3Month": info.get("averageVolume") or 0, "marketCap": info.get("totalAssets"),
                    "_sector": "Fund", "_venue": us, "_etf": True})
    _log(f"ETFs: {len(out)}")
    return out


def _realised_vol(symbols: list[str]) -> dict[str, float]:
    import yfinance as yf

    vols: dict[str, float] = {}
    for i in range(0, len(symbols), 200):
        batch = symbols[i : i + 200]
        try:
            closes = yf.download(batch, period="6mo", interval="1d", auto_adjust=True, progress=False,
                                 threads=True)["Close"]
        except Exception as e:
            _log(f"  history batch {i} failed: {e}")
            continue
        for sym in batch:
            if sym not in closes:
                continue
            px = [float(x) for x in closes[sym].dropna().tolist() if x and x > 0]
            rets = [math.log(b / a) for a, b in zip(px, px[1:], strict=False)]
            if len(rets) >= 40:
                mean = sum(rets) / len(rets)
                sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
                vols[sym] = round(sd * math.sqrt(252), 4)
        _log(f"  history {min(i + 200, len(symbols))}/{len(symbols)}")
    return vols


# -- OpenFIGI ------------------------------------------------------------------------------------
def _figi_ticker(symbol: str, v: Venue) -> list[str]:
    """Yahoo symbol -> OpenFIGI ticker candidates (``BRK-B`` -> ``BRK/B``; ``BP.L`` -> ``BP/``)."""
    base = symbol.removesuffix(v.suffix) if v.suffix else symbol
    t = base.replace("-", "/").rstrip(".")
    out = [t]
    if v.exch_code == "LN" and len(t) <= 2:
        out.insert(0, t + "/")
    return out


def _openfigi(jobs: list[dict[str, str]]) -> list[dict[str, Any] | None]:
    key = os.environ.get("OPENFIGI_API_KEY", "")
    size, pause = (100, 0.25) if key else (10, 2.5)
    headers = {"content-type": "application/json", **({"X-OPENFIGI-APIKEY": key} if key else {})}
    out: list[dict[str, Any] | None] = []
    with httpx.Client(timeout=30) as http:
        for i in range(0, len(jobs), size):
            batch = jobs[i : i + size]
            while True:
                r = http.post(OPENFIGI_URL, headers=headers, json=batch)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("ratelimit-reset", "10")) + 1)
                    continue
                r.raise_for_status()
                break
            for res in r.json():
                data = [d for d in res.get("data", []) if d.get("marketSector") == "Equity"]
                out.append(data[0] if data else None)
            if (i // size) % 10 == 0:
                _log(f"  openfigi {min(i + size, len(jobs))}/{len(jobs)}")
            time.sleep(pause)
    return out


def _map_figis(quotes: list[dict[str, Any]], known: dict[tuple[str, str], dict[str, Any]]
               ) -> list[dict[str, Any] | None]:
    """OpenFIGI records for each quote; ``known`` (from the previous snapshot) saves the API calls."""
    tries = [_figi_ticker(q["symbol"], q["_venue"]) for q in quotes]
    found: list[dict[str, Any] | None] = [
        next((known[(t, q["_venue"].exch_code)] for t in ts if (t, q["_venue"].exch_code) in known), None)
        for q, ts in zip(quotes, tries, strict=True)]
    _log(f"  {sum(f is not None for f in found)}/{len(quotes)} FIGIs reused from the previous snapshot")
    for attempt in range(max(len(t) for t in tries)):
        todo = [i for i, t in enumerate(tries) if found[i] is None and attempt < len(t)]
        if not todo:
            break
        jobs = [{"idType": "TICKER", "idValue": tries[i][attempt], "exchCode": quotes[i]["_venue"].exch_code}
                for i in todo]
        for i, res in zip(todo, _openfigi(jobs), strict=True):
            found[i] = res
    return found


# -- assemble ------------------------------------------------------------------------------------
def _known_figis(previous: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not previous.exists():
        return {}
    with gzip.open(previous, "rt") as f:
        rows = json.load(f)["instruments"]
    return {(r["ticker"], r["exchCode"]): {"ticker": r["ticker"], "figi": r["figi"], "name": r["name"],
                                          "compositeFIGI": r["compositeFigi"], "shareClassFIGI": r["shareClassFigi"],
                                          "securityType": r["securityType"]} for r in rows}


def build(previous: Path | None = None) -> dict[str, Any]:
    quotes = _candidates() + _etfs()
    vols = _realised_vol([q["symbol"] for q in quotes])
    figis = _map_figis(quotes, _known_figis(previous) if previous else {})
    rows = []
    missing = 0
    for q, f in zip(quotes, figis, strict=True):
        v: Venue = q["_venue"]
        if f is None:
            missing += 1
            continue
        px = float(q["regularMarketPrice"])
        if (q.get("currency") or "") == "GBp":  # LSE quotes in pence
            px /= 100
        if px < (10 if v.ccy == "JPY" else 0.05):  # sub-penny shells aren't lendable names
            missing += 1
            continue
        rows.append({
            "ticker": f["ticker"],
            "exchCode": v.exch_code,
            "mic": MICS.get(q.get("exchange", ""), MICS[v.exchanges[0]]),
            "name": f.get("name") or q.get("longName"),
            "longName": q.get("longName") or q.get("shortName"),
            "figi": f["figi"],
            "compositeFigi": f.get("compositeFIGI"),
            "shareClassFigi": f.get("shareClassFIGI"),
            "securityType": f.get("securityType"),
            "country": v.country,
            "ccy": v.ccy,
            "sector": q["_sector"],
            "isEtf": bool(q.get("_etf")),
            "lastPx": round(px, 4 if px < 10 else 2),
            "sharesOut": int(q.get("sharesOutstanding") or 0),
            "marketCap": int(q.get("marketCap") or 0),
            "adv3m": int(q.get("averageDailyVolume3Month") or 0),
            "volAnn": vols.get(q["symbol"]),
        })
    _log(f"{len(rows)} instruments ({missing} without a FIGI or a usable price dropped)")
    return {
        "asOf": datetime.now(UTC).date().isoformat(),
        "sources": {
            "listings": "Yahoo Finance screener via yfinance (personal-use terms; rounded aggregates only)",
            "history": "Yahoo Finance daily closes via yfinance, 6 months, for realised volatility",
            "identifiers": "OpenFIGI mapping API (FIGI, composite FIGI, share-class FIGI, ticker)",
        },
        "instruments": rows,
    }


def fetch(out: Path) -> None:
    snap = build(previous=out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Fixed mtime: re-running with the same data gives the same bytes.
    with gzip.GzipFile(out, "wb", mtime=0) as f:
        f.write(json.dumps(snap, separators=(",", ":"), sort_keys=True).encode())
    _log(f"wrote {out} ({out.stat().st_size // 1024} KiB)")
