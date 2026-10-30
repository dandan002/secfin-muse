"""The short list: what the desk must borrow today to deliver its clients' shorts.

A client granted a locate may short against it before it expires, or not (it went elsewhere). After the
close the stock record lands (``apply_stock_record``): every client's short position settling the next
business day, and how much of each locate was used. Locates the client shorted against become ``USED``.

Per name, the shares to deliver are the clients' shorts. What covers them is the box: the net of every
open borrow and loan on our books at the processor (``InternalPosition``), plus fills not booked there
yet, less borrows whose delivery is failing. The rest is the shortfall. Covers already working (open
borrow orders, and cover cards waiting at the gate) count against it; what's left is drafted as covers
(``draft``): a borrow order the router places (a firm bid on Venue A for a GC name), or for a hot name a
bid in chat to an agent lender we already talk to. The trader approves each card.

Borrows the clients no longer need are excess: a name we're net borrowed in that nobody is short, or where the
box is more than the shorts. Each costs its fee every day it stays out, so ``draft`` also drafts returns for
the latest list, the dearest borrow first. Returns made since the box was read still show in it, so they're
counted off, or the next draft would return the same shares again.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from refdata import calendar

from .. import clock, events, rules, secmaster
from ..models import (
    Action,
    ChatMessage,
    ClientShort,
    Counterparty,
    Instrument,
    InternalPosition,
    Locate,
    Order,
    SyncCursor,
    Trade,
)
from ..settings import get_settings
from . import inventory
from .booking import open_qty
from .orders import OPEN as OPEN_ORDERS
from .orders import OrderError, OrderIn, preview

SOURCE = "short"  # cover cards carry source_ref "short:<instrument id>"
RETURN = "return"  # return cards carry source_ref "return:<instrument id>"


async def latest_date(s: AsyncSession) -> date | None:
    return (await s.execute(select(func.max(ClientShort.trade_date)))).scalar()


async def apply_stock_record(s: AsyncSession, rec: dict[str, Any]) -> bool:
    """Store one close's stock record and mark the locates its clients used. False if it was already stored."""
    d = date.fromisoformat(rec["tradeDate"])
    if (await s.execute(select(ClientShort.trade_date).where(ClientShort.trade_date == d).limit(1))).first():
        return False
    settle = date.fromisoformat(rec["settlementDate"])
    for p in rec["positions"]:
        s.add(ClientShort(trade_date=d, counterparty=p["counterparty"], instrument_id=p["instrumentId"],
                          short_qty=p["shortQuantity"], sold_qty=p["soldQuantity"],
                          covered_qty=p["coveredQuantity"], settle_date=settle))
    used = 0
    for u in rec.get("locateUsage", []):
        loc = (await s.execute(select(Locate).where(Locate.source_channel == "API",
                                                    Locate.request_ref == u["locateId"],
                                                    Locate.line_no == u["lineNo"]))).scalar()
        if loc is None or loc.status not in ("APPROVED", "PARTIAL", "EXPIRED"):
            continue
        before = {"status": loc.status, "qtyUsed": loc.qty_used}
        loc.qty_used = int(u["usedQuantity"] or 0)
        loc.status = "USED" if loc.qty_used else "EXPIRED"
        used += bool(loc.qty_used)
        events.record(s, "locate", loc.id, "used" if loc.qty_used else "expired",
                      {"status": loc.status, "qtyUsed": loc.qty_used, "tradeDate": d.isoformat()}, before=before,
                      actor="gateway:stock-record")
    if not (await s.execute(select(ClientShort.trade_date).where(ClientShort.trade_date == d).limit(1))).first():
        # A close with no client shorts at all still counts as received.
        s.add(ClientShort(trade_date=d, counterparty="-", instrument_id="-", short_qty=0, settle_date=settle))
    events.record(s, "short_list", d.isoformat(), "stock_record",
                  {"tradeDate": d.isoformat(), "positions": len(rec["positions"]), "locatesUsed": used},
                  actor="gateway:stock-record")
    events.publish_later(s, "short_list", {"type": "stock_record", "tradeDate": d.isoformat()})
    return True


async def build(s: AsyncSession, trade_date: date | None = None) -> dict[str, Any]:
    """The short list for a close (default the latest): one line per name the clients are short."""
    d = trade_date or await latest_date(s)
    if d is None:
        return {"tradeDate": None, "settlementDate": None, "lines": [], "summary": {}}
    shorts = (await s.execute(select(ClientShort).where(ClientShort.trade_date == d,
                                                        ClientShort.counterparty != "-"))).scalars().all()
    by_name: dict[str, list[ClientShort]] = defaultdict(list)
    for c in shorts:
        by_name[c.instrument_id].append(c)
    # And every name we're net borrowed in, so a borrow nobody is short against any more shows as excess.
    held = {iid for iid, n in (await s.execute(
        select(InternalPosition.instrument_id, func.sum(InternalPosition.borrowed - InternalPosition.lent))
        .group_by(InternalPosition.instrument_id))).all() if (n or 0) > 0}
    held |= set((await s.execute(select(Trade.instrument_id).where(Trade.side == "BORROW",
                                                                   Trade.loan_status == "OPEN"))).scalars())
    iids = [*by_name, *sorted(held - by_name.keys())]
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(Instrument.id.in_(iids)))).scalars()}
    box: dict[str, int] = defaultdict(int)
    for p in (await s.execute(select(InternalPosition).where(InternalPosition.instrument_id.in_(iids)))).scalars():
        box[p.instrument_id] += p.borrowed - p.lent
    box_at = await s.get(SyncCursor, inventory.AS_OF)
    box_at = clock.parse_ts(box_at.value) if box_at else None
    unbooked: dict[str, int] = defaultdict(int)  # fills the box doesn't show yet: unbooked, or booked since it was read
    fails: dict[str, int] = defaultdict(int)  # borrows booked but failing to arrive
    for t in (await s.execute(select(Trade).where(Trade.instrument_id.in_(iids), Trade.loan_status == "OPEN"))
              ).scalars():
        q = (t.open_qty if t.open_qty is not None else t.qty) * (1 if t.side == "BORROW" else -1)
        booked = clock.parse_ts(clock.iso(t.booked_at)) if t.booked_at else None
        if t.sfp_contract_id is None or box_at is None or (booked is not None and booked > box_at):
            unbooked[t.instrument_id] += q
        elif t.side == "BORROW" and t.settle_status == "FAILING":
            fails[t.instrument_id] += q
    since: dict[str, int] = defaultdict(int)  # returns and recalls since the box was read: it still shows them
    if box_at is not None:
        for t in (await s.execute(select(Trade).where(Trade.instrument_id.in_(iids),
                                                      Trade.sfp_contract_id.is_not(None)))).scalars():
            booked = clock.parse_ts(clock.iso(t.booked_at)) if t.booked_at else None
            if booked is None or booked > box_at:  # not in the box: its open quantity already counts
                continue
            for e in t.lifecycle or []:
                if e.get("event") in ("RETURN", "RECALL") and clock.parse_ts(e["ts"]) > box_at:
                    since[t.instrument_id] += int(e["quantity"]) * (1 if t.side == "BORROW" else -1)
    working: dict[str, int] = defaultdict(int)  # open borrow orders, and chat bids not answered yet
    for o in (await s.execute(select(Order).where(Order.instrument_id.in_(iids), Order.side == "BORROW",
                                                  Order.status.in_(OPEN_ORDERS)))).scalars():
        working[o.instrument_id] += max(o.qty - (o.filled_qty or 0), 0)
    for iid, bid in (await chat_bids(s, iids)).items():
        working[iid] += sum(int((a.original or {}).get("quantity") or 0) for a, answered in bid if not answered)
    # Cards at the gate: our cover drafts, and any borrow card for the name (one agreed in chat, say).
    drafted: dict[str, list[Action]] = defaultdict(list)
    for a in (await s.execute(select(Action).where(Action.status == "PENDING",
                                                   Action.kind.in_(("order", "chat_send"))))).scalars():
        if (a.source_ref or "").startswith(f"{SOURCE}:"):
            drafted[a.source_ref.split(":", 1)[1]].append(a)
        elif a.kind == "order" and a.payload.get("side") == "BORROW" and a.payload.get("instrument") in by_name:
            drafted[a.payload["instrument"]].append(a)
    returning: dict[str, list[tuple[Action, int]]] = defaultdict(list)  # return cards at the gate, and how many
    cards = (await s.execute(select(Action).where(Action.status == "PENDING", Action.kind == "lifecycle"))).scalars()
    cards = [a for a in cards if a.payload.get("event") == "RETURN"]
    on = {t.id: t for t in (await s.execute(select(Trade).where(
        Trade.id.in_([a.payload["tradeId"] for a in cards])))).scalars()} if cards else {}
    for a in cards:
        if (tr := on.get(a.payload["tradeId"])) is not None:
            returning[tr.instrument_id].append((a, int(a.payload.get("quantity") or open_qty(tr))))
    lines = []
    for iid in iids:
        rows = by_name.get(iid, [])
        inst = insts.get(iid)
        lv = secmaster.live(iid)
        need = sum(c.short_qty for c in rows)
        cover = max(box[iid] - since[iid], 0) + unbooked[iid] - fails[iid]
        shortfall = max(need - cover, 0)
        pending = working[iid] + sum(int(a.payload.get("quantity") or (a.original or {}).get("quantity") or 0)
                                     for a in drafted[iid])
        uncovered = max(shortfall - pending, 0)
        px = lv["lastPx"] or 0.0
        fx = rules.FX_USD.get(inst.ccy if inst else "USD", 1.0)
        excess = max(cover - need, 0)
        to_return = max(excess - sum(q for _, q in returning[iid]), 0)
        lines.append({
            "instrumentId": iid, "composite": inst.composite if inst else iid, "name": inst.name if inst else None,
            "ccy": inst.ccy if inst else None, "tier": lv["tier"],
            "etb": bool(inst and secmaster.is_etb(inst, lv["tier"])),
            "feeBps": lv["feeBps"], "lastPx": px,
            "clientShort": need, "soldToday": sum(c.sold_qty for c in rows),
            "coveredToday": sum(c.covered_qty for c in rows),
            "clients": sorted(({"counterparty": c.counterparty, "short": c.short_qty, "sold": c.sold_qty,
                                "covered": c.covered_qty} for c in rows), key=lambda x: -x["short"]),
            "box": box[iid], "unbooked": unbooked[iid], "fails": fails[iid], "cover": cover,
            "shortfall": shortfall, "working": working[iid],
            "drafted": [{"actionId": a.id, "kind": a.kind, "title": a.title} for a in drafted[iid]],
            "uncovered": uncovered, "uncoveredNotional": round(uncovered * px * fx, 2),
            "returnedSinceBox": since[iid], "excess": excess, "excessNotional": round(excess * px * fx, 2),
            # What the excess costs a day at the market fee (actual/360).
            "excessCostUsd": round(excess * px * fx * (lv["feeBps"] or 0.0) / 10_000 / 360, 2),
            "returning": [{"actionId": a.id, "title": a.title, "quantity": q} for a, q in returning[iid]],
            "toReturn": to_return,
            "status": ("EXCESS" if excess else "COVERED") if shortfall == 0
            else ("WORKING" if uncovered == 0 else "SHORT"),
        })
    lines.sort(key=lambda x: (-x["uncoveredNotional"], -x["shortfall"], -x["excessNotional"], x["composite"]))
    short = [x for x in lines if x["status"] != "COVERED"]
    return {
        "tradeDate": d.isoformat(), "settlementDate": calendar.add_business_days(d, 1).isoformat(),
        "lines": lines,
        "summary": {"names": len(lines), "short": sum(1 for x in lines if x["status"] == "SHORT"),
                    "working": sum(1 for x in lines if x["status"] == "WORKING"),
                    "covered": sum(1 for x in lines if x["status"] == "COVERED"),
                    "excess": sum(1 for x in lines if x["status"] == "EXCESS"),
                    "excessNotional": round(sum(x["excessNotional"] for x in lines), 2),
                    "excessCostUsd": round(sum(x["excessCostUsd"] for x in lines), 2),
                    "toReturn": sum(x["toReturn"] for x in lines),
                    "clientShort": sum(x["clientShort"] for x in lines),
                    "shortfall": sum(x["shortfall"] for x in short),
                    "uncovered": sum(x["uncovered"] for x in short),
                    "uncoveredNotional": round(sum(x["uncoveredNotional"] for x in short), 2)},
    }


def _lot(q: int) -> int:
    return int(math.ceil(q / 100) * 100)


def _fmt_qty(q: int) -> str:
    if q >= 1_000_000 and q % 100_000 == 0:
        return f"{q / 1_000_000:g}m"
    return f"{q / 1000:g}k" if q % 1000 == 0 else f"{q:,}"


async def chat_bids(s: AsyncSession, iids: list[str]) -> dict[str, list[tuple[Action, bool]]]:
    """Our cover bids sent in chat, per name, and whether the lender has answered each since: a later message of
    theirs in that room about the name, or a bare counter, pass or "done" that names nothing."""
    out: dict[str, list[tuple[Action, bool]]] = defaultdict(list)
    sent = (await s.execute(select(Action).where(Action.kind == "chat_send", Action.status == "EXECUTED",
                                                 Action.source_ref.in_([f"{SOURCE}:{i}" for i in iids])))).scalars()
    for a in sent:
        iid = a.source_ref.split(":", 1)[1]
        later = (await s.execute(select(ChatMessage).where(
            ChatMessage.stream_id == a.payload["roomId"], ChatMessage.side == "THEM",
            ChatMessage.sent_at > a.decided_at))).scalars().all()
        answered = any(any(ln.get("instrumentId") == iid for ln in m.lines or [])
                       or (not m.lines and m.intent in ("COUNTER", "DECLINE", "ACCEPT")) for m in later)
        out[iid].append((a, answered))
    return out


async def lender_room(s: AsyncSession, instrument_id: str) -> tuple[str, str] | None:
    """An agent lender's chat room to bid in: one that showed us this name lately, else the last one we spoke in."""
    rows = (await s.execute(
        select(ChatMessage.stream_id, ChatMessage.counterparty, ChatMessage.lines, ChatMessage.intent)
        .join(Counterparty, Counterparty.code == ChatMessage.counterparty)
        .where(Counterparty.kind == "AGENT_LENDER").order_by(ChatMessage.sent_at.desc()).limit(200))).all()
    for stream, cp, lines, intent in rows:
        if intent in ("OFFER", "IOI") and any(ln.get("instrumentId") == instrument_id for ln in lines or []):
            return stream, cp
    return (rows[0][0], rows[0][1]) if rows else None


async def draft(s: AsyncSession, *, by: str = "rules:short-list", trade_date: date | None = None) -> dict[str, Any]:
    """A cover card for every name still uncovered. A name with a card already waiting gets no second one."""
    from .. import gate

    sl = await build(s, trade_date)
    cfg = get_settings()
    bids = await chat_bids(s, [ln["instrumentId"] for ln in sl["lines"]])
    made, notes = [], []
    for ln in sl["lines"]:
        if ln["uncovered"] <= 0 or ln["drafted"]:
            continue
        qty = _lot(ln["uncovered"])
        fee = round(max(ln["feeBps"] or 0.0, 1.0) * (1 + cfg.cover_markup_pct / 100), 1)
        why = (f"clients short {ln['clientShort']:,} {ln['composite']} settling {sl['settlementDate']}; box "
               f"{ln['box']:,}" + (f", unbooked {ln['unbooked']:,}" if ln["unbooked"] else "")
               + (f", failing {ln['fails']:,}" if ln["fails"] else "")
               + (f", working {ln['working']:,}" if ln["working"] else "") + f": {ln['uncovered']:,} to borrow")
        ref = f"{SOURCE}:{ln['instrumentId']}"
        # A hot name is asked for in chat, once: if the lender countered or passed, the next cover goes to a venue.
        asked = bids.get(ln["instrumentId"])
        room = await lender_room(s, ln["instrumentId"]) if ln["tier"] == "HOT" and not asked else None
        if asked:
            why += "; the lender we asked in chat didn't do it"
        try:
            if room:
                stream, cp = room
                a = await gate.propose(
                    s, kind="chat_send", payload={"roomId": stream, "text": f"Bid {_fmt_qty(qty)} {ln['composite']}"
                                                                             f" @ {fee:g}bps open?"},
                    proposed_by=by, title=f"cover short: bid {qty:,} {ln['composite']} @ {fee:g} bps to {cp} in chat",
                    rationale=f"{why}. Hot name: asked in chat of {cp}, an agent lender we deal with; their "
                              f"\"done\" drafts the borrow.", source_ref=ref,
                    original={"shortList": sl["tradeDate"], "line": ln["instrumentId"], "quantity": qty})
            else:
                req = OrderIn(side="BORROW", instrument=ln["instrumentId"], quantity=qty, feeBps=fee)
                p = await preview(s, req)
                failed = [c for c in p["checks"] if not c["ok"]]
                req.override = any(not c["blocking"] for c in failed)
                a = await gate.propose(
                    s, kind="order", payload=req.model_dump(mode="json"), proposed_by=by,
                    title=f"cover short: borrow {qty:,} {ln['composite']} @ {fee:g} bps",
                    rationale=f"{why}. {p['routeReason']}.",
                    risk_flags=[f"{'BLOCKED' if c['blocking'] else 'warning'}: {c['name']} ({c['detail']})"
                                for c in failed],
                    source_ref=ref, original={"shortList": sl["tradeDate"], "line": ln["instrumentId"]})
            made.append(a.id)
        except (OrderError, gate.GateError) as e:
            notes.append(f"{ln['composite']}: {e}")
    if trade_date is None or trade_date == await latest_date(s):  # an old list's excess is history
        made += await _draft_returns(s, sl, by=by, notes=notes)
    if made:
        events.publish_later(s, "short_list", {"type": "drafted", "actions": made})
    return {"tradeDate": sl["tradeDate"], "drafted": made, "notes": notes}


async def _draft_returns(s: AsyncSession, sl: dict[str, Any], *, by: str, notes: list[str]) -> list[str]:
    """A return card per borrow for each name's excess (less returns already at the gate), dearest fee first.
    Only borrows booked at the processor and not failing can be returned."""
    from .. import gate

    made = []
    for ln in sl["lines"]:
        left = ln["toReturn"] // 100 * 100
        if left <= 0:
            continue
        carded = {a["actionId"] for a in ln["returning"]}
        busy = {(await s.get(Action, i)).payload["tradeId"] for i in carded}
        trades = (await s.execute(select(Trade).where(
            Trade.instrument_id == ln["instrumentId"], Trade.side == "BORROW", Trade.loan_status == "OPEN",
            Trade.sfp_contract_id.is_not(None), Trade.settle_status.is_distinct_from("FAILING"))
            .order_by(Trade.fee_bps.desc(), Trade.executed_at.desc()))).scalars().all()
        why = (f"clients short {ln['clientShort']:,} {ln['composite']} settling {sl['settlementDate']}; box "
               f"{ln['box']:,}" + (f", unbooked {ln['unbooked']:,}" if ln["unbooked"] else "")
               + (f", returned since the box was read {ln['returnedSinceBox']:,}" if ln["returnedSinceBox"] else "")
               + (f", failing {ln['fails']:,}" if ln["fails"] else "")
               + f": {ln['excess']:,} more than they need")
        flags = [f"warning: borrow orders for {ln['working']:,} still working on the name; cancel them too"
                 ] if ln["working"] else []
        for t in trades:
            if left <= 0:
                break
            have = open_qty(t)
            if t.id in busy or have <= 0:
                continue
            qty = have if have <= left else left
            left -= qty
            daily = qty * ln["lastPx"] * rules.FX_USD.get(ln["ccy"] or "USD", 1.0) * t.fee_bps / 10_000 / 360
            try:
                a = await gate.propose(
                    s, kind="lifecycle", payload={"tradeId": t.id, "event": "RETURN", "quantity": qty},
                    proposed_by=by,
                    title=f"return excess: {qty:,} {ln['composite']} to {t.cp_code} @ {t.fee_bps:g} bps",
                    rationale=f"{why}. Returning {'all' if qty == have else f'{qty:,} of {have:,}'} of {t.id}, the "
                              f"dearest borrow left, saves about ${daily:,.0f} a day.",
                    risk_flags=flags, source_ref=f"{RETURN}:{ln['instrumentId']}",
                    original={"shortList": sl["tradeDate"], "line": ln["instrumentId"], "excess": ln["excess"]})
                made.append(a.id)
            except gate.GateError as e:
                notes.append(f"{ln['composite']}: {e}")
                break
        if left >= 100:
            notes.append(f"{ln['composite']}: {left:,} excess isn't on a returnable borrow yet (unbooked or failing)")
    return made


def missed_closes(have: date | None, latest: date) -> list[date]:
    """Closes between the last record we stored and the latest one, oldest first (at most a fortnight back).
    With none stored yet, only the latest: yesterday's locates are history, not today's work."""
    if have is None:
        return [latest]
    d, out = max(have, calendar.add_business_days(latest, -10)), []
    while d < latest:
        d = calendar.add_business_days(d, 1)
        out.append(d)
    return out
