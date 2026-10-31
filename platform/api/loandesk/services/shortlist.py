"""The short list: what the desk must borrow today to deliver its clients' shorts.

A client granted a locate may short against it before it expires, or not (it went elsewhere). After the
close the stock record lands (``apply_stock_record``): every client's short position settling the next
business day, and how much of each locate was used. Locates the client shorted against become ``USED``.

Per name, the shares to deliver are the clients' shorts. What covers them is the box: the net of every
open borrow and loan on our books at the processor (``InternalPosition``), plus fills not booked there
yet, less borrows whose delivery is failing and shares their lenders recalled. The rest is the shortfall.
Covers already working (open borrow orders, and cover cards waiting at the gate) count against it; what's
left is drafted as covers (``draft``): a borrow order the router places (a firm bid on Venue A for a GC
name), or for a hot name a bid in chat to an agent lender we already talk to. The trader approves each card.

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
from . import buyins, inventory
from . import fails as cns
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
    await s.flush()
    await inventory.refresh(s)  # the clients' shorts are delivered out of the box: no longer ours to lend
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
    failing = await cns.active(s)  # and every name with a client short that failed at CNS
    bought = await buyins.covered_since(s, d)
    iids = [*by_name, *sorted((held | set(failing)) - by_name.keys())]
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(Instrument.id.in_(iids)))).scalars()}
    box: dict[str, int] = defaultdict(int)
    for p in (await s.execute(select(InternalPosition).where(InternalPosition.instrument_id.in_(iids)))).scalars():
        box[p.instrument_id] += p.borrowed - p.lent
    box_at = await s.get(SyncCursor, inventory.AS_OF)
    box_at = clock.parse_ts(box_at.value) if box_at else None
    unbooked: dict[str, int] = defaultdict(int)  # fills the box doesn't show yet: unbooked, or booked since it was read
    fails: dict[str, int] = defaultdict(int)  # borrows booked but failing to arrive
    recalled: dict[str, int] = defaultdict(int)  # borrows their lenders recalled: owed back, not cover
    for t in (await s.execute(select(Trade).where(Trade.instrument_id.in_(iids), Trade.loan_status == "OPEN"))
              ).scalars():
        q = (t.open_qty if t.open_qty is not None else t.qty) * (1 if t.side == "BORROW" else -1)
        booked = clock.parse_ts(clock.iso(t.booked_at)) if t.booked_at else None
        if t.sfp_contract_id is None or box_at is None or (booked is not None and booked > box_at):
            unbooked[t.instrument_id] += q
        elif t.side == "BORROW" and t.settle_status == "FAILING":
            fails[t.instrument_id] += q
            continue  # all of it is off the cover already
        if t.side == "BORROW" and t.recalled_qty:
            recalled[t.instrument_id] += t.recalled_qty
    since: dict[str, int] = defaultdict(int)  # returns and recalls since the box was read: it still shows them
    if box_at is not None:
        for t in (await s.execute(select(Trade).where(Trade.instrument_id.in_(iids),
                                                      Trade.sfp_contract_id.is_not(None)))).scalars():
            booked = clock.parse_ts(clock.iso(t.booked_at)) if t.booked_at else None
            if booked is None or booked > box_at:  # not in the box: its open quantity already counts
                continue
            for e in t.lifecycle or []:
                if e.get("event") in ("RETURN", "RECALL", "BOUGHT_IN") and clock.parse_ts(e["ts"]) > box_at:
                    since[t.instrument_id] += int(e["quantity"]) * (1 if t.side == "BORROW" else -1)
    working: dict[str, int] = defaultdict(int)  # open borrow orders, and chat bids not answered yet
    for o in (await s.execute(select(Order).where(Order.instrument_id.in_(iids), Order.side == "BORROW",
                                                  Order.status.in_(OPEN_ORDERS)))).scalars():
        working[o.instrument_id] += max(o.qty - (o.filled_qty or 0), 0)
    for iid, bid in (await chat_bids(s, iids)).items():
        working[iid] += sum(int((a.original or {}).get("quantity") or 0) for a, answered in bid if not answered)
    # Cards at the gate: our cover drafts, and any borrow card for the name (one agreed in chat, say).
    # A basket card covers a line per name; a recall of a loan of ours covers its name as a borrow would.
    drafted: dict[str, list[Action]] = defaultdict(list)
    asked: dict[tuple[str, str], int] = {}  # (card, name): shares the card would bring in
    for a in (await s.execute(select(Action).where(Action.status == "PENDING", Action.kind.in_(
            ("order", "chat_send", "basket", "lifecycle"))))).scalars():
        ref = a.source_ref or ""
        if a.kind == "basket":
            for x in a.payload.get("lines", []):
                drafted[x["instrument"]].append(a)
                asked[(a.id, x["instrument"])] = int(x["quantity"])
        elif a.kind == "lifecycle":
            if ref.startswith(f"{SOURCE}:") and a.payload.get("event") == "RECALL":
                drafted[ref.split(":", 1)[1]].append(a)
        elif ref.startswith(f"{SOURCE}:"):
            drafted[ref.split(":", 1)[1]].append(a)
        elif a.kind == "order" and a.payload.get("side") == "BORROW" and a.payload.get("instrument") in by_name:
            drafted[a.payload["instrument"]].append(a)
    returning: dict[str, list[tuple[Action, int]]] = defaultdict(list)  # return cards at the gate, and how many
    cards = (await s.execute(select(Action).where(Action.status == "PENDING", Action.kind == "lifecycle"))).scalars()
    cards = [a for a in cards if a.payload.get("event") == "RETURN"]
    on = {t.id: t for t in (await s.execute(select(Trade).where(
        Trade.id.in_([a.payload["tradeId"] for a in cards])))).scalars()} if cards else {}
    for a in cards:
        if (a.source_ref or "").startswith("recall:"):  # owed on a recall: already off the cover
            continue
        if (tr := on.get(a.payload["tradeId"])) is not None:
            returning[tr.instrument_id].append((a, int(a.payload.get("quantity") or open_qty(tr))))
    lines = []
    for iid in iids:
        rows = by_name.get(iid, [])
        inst = insts.get(iid)
        lv = secmaster.live(iid)
        # A short that failed at CNS is owed even if the stock record no longer shows it (bought back since); one
        # bought in at CNS since the close is covered, though the record still shows it.
        need = max(sum(c.short_qty for c in rows) - bought.get(iid, 0),
                   sum(f.open_qty for f in failing.get(iid, [])), 0)
        cover = max(box[iid] - since[iid], 0) + unbooked[iid] - fails[iid] - recalled[iid]
        shortfall = max(need - cover, 0)
        pending = working[iid] + sum(_card_qty(a, iid, asked) for a in drafted[iid])
        uncovered = max(shortfall - pending, 0)
        px = lv["lastPx"] or 0.0
        fx = rules.FX_USD.get(inst.ccy if inst else "USD", 1.0)
        excess = max(cover - need, 0)
        open_fails = [f for f in failing.get(iid, []) if f.status == "OPEN"]
        to_return = max(excess - sum(q for _, q in returning[iid]), 0)
        lines.append({
            "instrumentId": iid, "composite": inst.composite if inst else iid, "name": inst.name if inst else None,
            "ccy": inst.ccy if inst else None, "tier": lv["tier"],
            "etb": bool(inst and secmaster.is_etb(inst, lv["tier"])),
            "feeBps": lv["feeBps"], "lastPx": px,
            "clientShort": sum(c.short_qty for c in rows), "need": need, "soldToday": sum(c.sold_qty for c in rows),
            "coveredToday": sum(c.covered_qty for c in rows),
            "clients": sorted(({"counterparty": c.counterparty, "short": c.short_qty, "sold": c.sold_qty,
                                "covered": c.covered_qty} for c in rows), key=lambda x: -x["short"]),
            "box": box[iid], "unbooked": unbooked[iid], "fails": fails[iid], "recalled": recalled[iid],
            "cover": cover,
            "shortfall": shortfall, "working": working[iid],
            "drafted": [{"actionId": a.id, "kind": a.kind, "title": a.title, "quantity": _card_qty(a, iid, asked)}
                        for a in drafted[iid]],
            "uncovered": uncovered, "uncoveredNotional": round(uncovered * px * fx, 2),
            "returnedSinceBox": since[iid], "excess": excess, "excessNotional": round(excess * px * fx, 2),
            # What the excess costs a day at the market fee (actual/360).
            "excessCostUsd": round(excess * px * fx * (lv["feeBps"] or 0.0) / 10_000 / 360, 2),
            "returning": [{"actionId": a.id, "title": a.title, "quantity": q} for a, q in returning[iid]],
            "toReturn": to_return,
            # Shorts that failed at CNS and aren't delivered yet; the Rule 204 close-out for those still open.
            "cnsFails": sum(f.open_qty for f in failing.get(iid, [])),
            "closeOutBy": clock.iso(min(f.deadline for f in open_fails)) if open_fails else None,
            "preBorrow": any(f.missed for f in failing.get(iid, [])),
            "status": ("EXCESS" if excess else "COVERED") if shortfall == 0
            else ("WORKING" if uncovered == 0 else "SHORT"),
        })
    # A Rule 204 close-out first (soonest deadline), then the biggest uncovered notional.
    lines.sort(key=lambda x: (x["closeOutBy"] is None, x["closeOutBy"] or "", -x["uncoveredNotional"], -x["shortfall"],
                              -x["excessNotional"], x["composite"]))
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
                    "cnsFails": sum(x["cnsFails"] for x in lines),
                    "closeOut": sum(1 for x in lines if x["closeOutBy"]),
                    "preBorrow": sum(1 for x in lines if x["preBorrow"]),
                    "clientShort": sum(x["clientShort"] for x in lines),
                    "shortfall": sum(x["shortfall"] for x in short),
                    "uncovered": sum(x["uncovered"] for x in short),
                    "uncoveredNotional": round(sum(x["uncoveredNotional"] for x in short), 2)},
    }


def _card_qty(a: Action, iid: str, asked: dict[tuple[str, str], int]) -> int:
    """How many shares a cover card at the gate brings in for a name."""
    if (a.id, iid) in asked:
        return asked[(a.id, iid)]
    return int(a.payload.get("quantity") or (a.original or {}).get("quantity") or 0)


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

    cfg = get_settings()
    await _supersede_stale(s, by=by)  # their names are uncovered again: drafted afresh below, at today's fee
    await _supersede_oversized(s, await build(s, trade_date), by=by)
    sl = await build(s, trade_date)
    bids = await chat_bids(s, [ln["instrumentId"] for ln in sl["lines"]])
    made, notes = [], []
    basket: list[dict[str, Any]] = []  # warm names, to cover together on Venue B if there are enough of them
    for ln in sl["lines"]:
        if ln["uncovered"] <= 0 or ln["drafted"]:
            continue
        fee = round(max(ln["feeBps"] or 0.0, 1.0) * (1 + cfg.cover_markup_pct / 100), 1)
        left = ln["uncovered"]
        if cfg.recall_loans_for_shorts:  # shares we lent out come back cheaper than the street lends them
            got, ids = await _recall_loans(s, ln, left, fee, by=by, notes=notes)
            left -= got
            made += ids
            if left <= 0:
                continue
        qty = _lot(left)
        why = (f"clients short {ln['clientShort']:,} {ln['composite']} settling {sl['settlementDate']}; box "
               f"{ln['box']:,}" + (f", unbooked {ln['unbooked']:,}" if ln["unbooked"] else "")
               + (f", failing {ln['fails']:,}" if ln["fails"] else "")
               + (f", recalled {ln['recalled']:,}" if ln["recalled"] else "")
               + (f", working {ln['working']:,}" if ln["working"] else "") + f": {ln['uncovered']:,} to borrow")
        flags = []
        if ln["closeOutBy"]:  # a short failed at CNS: Rule 204 wants it closed out by the open
            by_when = clock.parse_ts(ln["closeOutBy"]).astimezone(clock.NEW_YORK).strftime("%H:%M ET on %a %d %b")
            why += f"; {ln['cnsFails']:,} failed to deliver at CNS: Rule 204 close-out by {by_when}"
            flags.append(f"warning: Rule 204 close-out due by {by_when}")
        if ln["preBorrow"]:
            flags.append("warning: missed a Rule 204 close-out: the name is pre-borrow until the shares deliver")
        ref = f"{SOURCE}:{ln['instrumentId']}"
        # A hot name is asked for in chat, once: if the lender countered or passed, the next cover goes to a venue.
        asked = bids.get(ln["instrumentId"])
        room = await lender_room(s, ln["instrumentId"]) if ln["tier"] == "HOT" and not asked else None
        if asked:
            why += "; the lender we asked in chat didn't do it"
        if left < ln["uncovered"]:
            why += f"; recalling {ln['uncovered'] - left:,} we lent: {qty:,} left to borrow"
        if not room and ln["tier"] == "WARM" and cfg.basket_min_names:
            basket.append({"line": ln, "qty": qty, "fee": fee, "why": why, "flags": flags})
            continue
        try:
            if room:
                stream, cp = room
                a = await gate.propose(
                    s, kind="chat_send", payload={"roomId": stream, "text": f"Bid {_fmt_qty(qty)} {ln['composite']}"
                                                                             f" @ {fee:g}bps open?"},
                    proposed_by=by, title=f"cover short: bid {qty:,} {ln['composite']} @ {fee:g} bps to {cp} in chat",
                    rationale=f"{why}. Hot name: asked in chat of {cp}, an agent lender we deal with; their "
                              f"\"done\" drafts the borrow.", risk_flags=flags, source_ref=ref,
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
                    risk_flags=flags + [f"{'BLOCKED' if c['blocking'] else 'warning'}: {c['name']} ({c['detail']})"
                                        for c in failed],
                    source_ref=ref, original={"shortList": sl["tradeDate"], "line": ln["instrumentId"]})
            made.append(a.id)
        except (OrderError, gate.GateError) as e:
            notes.append(f"{ln['composite']}: {e}")
    made += await _draft_basket(s, sl, basket, by=by, notes=notes,
                                single=len(basket) < max(cfg.basket_min_names, 1))
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
               + (f", recalled {ln['recalled']:,}" if ln["recalled"] else "")
               + f": {ln['excess']:,} more than they need")
        flags = [f"warning: borrow orders for {ln['working']:,} still working on the name; cancel them too"
                 ] if ln["working"] else []
        for t in trades:
            if left <= 0:
                break
            have = open_qty(t) - (t.recalled_qty or 0)  # what's recalled goes back on its own card
            if t.id in busy or have <= 0:
                continue
            qty = have if have <= left else left
            whole = qty == open_qty(t)
            left -= qty
            daily = qty * ln["lastPx"] * rules.FX_USD.get(ln["ccy"] or "USD", 1.0) * t.fee_bps / 10_000 / 360
            try:
                a = await gate.propose(
                    s, kind="lifecycle", payload={"tradeId": t.id, "event": "RETURN", "quantity": qty},
                    proposed_by=by,
                    title=f"return excess: {qty:,} {ln['composite']} to {t.cp_code} @ {t.fee_bps:g} bps",
                    rationale=f"{why}. Returning {'all' if whole else f'{qty:,} of {open_qty(t):,}'} of {t.id}, the "
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


async def _supersede_stale(s: AsyncSession, *, by: str) -> int:
    """A cover order drafted at an earlier fee that's now under the market (a card left from yesterday, say) would
    be rejected under a venue's floor: supersede it, so the name is drafted again at today's fee."""
    from .. import gate

    n = 0
    for a in (await s.execute(select(Action).where(Action.status == "PENDING", Action.kind.in_(("order", "basket")),
                                                   Action.source_ref.like(f"{SOURCE}:%")))).scalars().all():
        for x in a.payload["lines"] if a.kind == "basket" else [a.payload]:
            fee, market_bps = x.get("feeBps"), secmaster.live(x["instrument"])["feeBps"]
            if fee is not None and market_bps and fee < market_bps:
                await gate.reject(s, a, by, f"bid {fee:g} bps is under the market fee now ({market_bps:g})"
                                            + (f" in {x['instrument']}" if a.kind == "basket" else "") + ": redrafted",
                                  status="SUPERSEDED")
                n += 1
                break
    return n


async def _supersede_oversized(s: AsyncSession, sl: dict[str, Any], *, by: str) -> int:
    """A cover order bigger than what the name is still short by (the clients bought back, a buy-in covered them, a
    borrow came in another way) would borrow shares nobody needs: supersede it; the draft sizes it again."""
    from .. import gate

    n, gone = 0, set()
    for ln in sl["lines"]:
        ours = [d for d in ln["drafted"] if d["kind"] in ("order", "basket") and d["actionId"] not in gone
                and ((await s.get(Action, d["actionId"])).source_ref or "").startswith(f"{SOURCE}:")]
        asked = sum(d["quantity"] for d in ours)
        need = _lot(max(ln["shortfall"] - ln["working"], 0)) if ln["shortfall"] > ln["working"] else 0
        if ours and asked > need:
            for d in ours:
                await gate.reject(s, await s.get(Action, d["actionId"]), by,
                                  f"{ln['composite']} is short {need:,} now, not {asked:,}: "
                                  + ("redrafted" if need else "no cover needed"), status="SUPERSEDED")
                gone.add(d["actionId"])  # a basket goes once, for its first name that's over
                n += 1
    return n


async def _recall_loans(s: AsyncSession, ln: dict[str, Any], left: int, cover_fee: float, *, by: str,
                        notes: list[str]) -> tuple[int, list[str]]:
    """Recall shares we lent, cheapest loan first, for as much of ``left`` as they cover: when a loan earns less than
    the cover would cost to borrow (or the name is hot, where the street may not lend at all). Each is a RECALL
    card at the gate on the loan; the borrower has the standard settlement cycle to return the shares."""
    from .. import gate

    got, ids = 0, []
    loans = (await s.execute(select(Trade).where(
        Trade.instrument_id == ln["instrumentId"], Trade.side == "LEND", Trade.loan_status == "OPEN",
        Trade.sfp_contract_id.is_not(None)).order_by(Trade.fee_bps, Trade.executed_at))).scalars().all()
    for t in loans:
        if got >= left:
            break
        if t.fee_bps >= cover_fee and ln["tier"] != "HOT":
            break  # the rest earn more than the borrow would cost: keep them out, borrow instead
        q = min(open_qty(t), _lot(left - got))
        if q <= 0:
            continue
        lost = q * ln["lastPx"] * rules.FX_USD.get(ln["ccy"] or "USD", 1.0) * t.fee_bps / 10_000 / 360
        hot = " (and it's a hot name)" if ln["tier"] == "HOT" else ""
        try:
            a = await gate.propose(
                s, kind="lifecycle", payload={"tradeId": t.id, "event": "RECALL", "quantity": q}, proposed_by=by,
                title=f"cover short: recall {q:,} {ln['composite']} lent to {t.cp_code} @ {t.fee_bps:g} bps",
                rationale=f"clients short {ln['clientShort']:,} {ln['composite']}; {ln['uncovered']:,} still to "
                          f"cover. We lend {open_qty(t):,} to {t.cp_code} on {t.id} at {t.fee_bps:g} bps, under the "
                          f"{cover_fee:g} bps a borrow would cost{hot}: recalling {q:,} gives up about "
                          f"${lost:,.0f} a day of lending fee.",
                risk_flags=["warning: the borrower has the settlement cycle to return them; a cover borrowed today "
                            "settles sooner"] if ln["closeOutBy"] else [],
                source_ref=f"{SOURCE}:{ln['instrumentId']}", original={"shortList": ln["instrumentId"], "quantity": q})
        except gate.GateError as e:
            notes.append(f"{ln['composite']}: {e}")
            break
        got += q
        ids.append(a.id)
    return got, ids


async def _draft_basket(s: AsyncSession, sl: dict[str, Any], rows: list[dict[str, Any]], *, by: str,
                        notes: list[str], single: bool) -> list[str]:
    """Warm names to cover: enough of them go to Venue B as one basket (an interest list, a line per name, each at
    its own limit); too few, and each gets its own order as usual."""
    from .. import gate

    made, lines, flags, parts = [], [], [], []
    for r in rows:
        ln = r["line"]
        req = OrderIn(side="BORROW", instrument=ln["instrumentId"], quantity=r["qty"], feeBps=r["fee"],
                      **({} if single else {"venue": "VENUE_B"}))
        try:
            p = await preview(s, req)
        except OrderError as e:
            notes.append(f"{ln['composite']}: {e}")
            continue
        failed = [c for c in p["checks"] if not c["ok"]]
        req.override = any(not c["blocking"] for c in failed)
        line_flags = r["flags"] + [f"{'BLOCKED' if c['blocking'] else 'warning'}: {c['name']} ({c['detail']})"
                                   for c in failed]
        if single:
            try:
                a = await gate.propose(
                    s, kind="order", payload=req.model_dump(mode="json"), proposed_by=by,
                    title=f"cover short: borrow {r['qty']:,} {ln['composite']} @ {r['fee']:g} bps",
                    rationale=f"{r['why']}. {p['routeReason']}.", risk_flags=line_flags,
                    source_ref=f"{SOURCE}:{ln['instrumentId']}",
                    original={"shortList": sl["tradeDate"], "line": ln["instrumentId"]})
                made.append(a.id)
            except gate.GateError as e:
                notes.append(f"{ln['composite']}: {e}")
            continue
        if any(c["blocking"] for c in failed):
            notes.append(f"{ln['composite']}: left out of the basket: " + "; ".join(
                c["name"] for c in failed if c["blocking"]))
            continue
        lines.append(req.model_dump(mode="json"))
        flags += [f"{ln['composite']}: {f}" for f in line_flags]
        parts.append(f"{r['qty']:,} {ln['composite']} @ {r['fee']:g} bps")
    if lines:
        try:
            a = await gate.propose(
                s, kind="basket", payload={"lines": lines}, proposed_by=by,
                title=f"cover shorts: borrow a basket of {len(lines)} names on Venue B "
                      f"({sum(x['quantity'] for x in lines):,} shares)",
                rationale=f"{len(lines)} warm names short at the {sl['tradeDate']} close, settling "
                          f"{sl['settlementDate']}: one Venue B interest list rather than {len(lines)} negotiated "
                          f"bids, each line at its own limit, accepted on our side when a dealer meets it: "
                          + "; ".join(parts) + ".",
                risk_flags=flags, source_ref=f"{SOURCE}:basket:{sl['tradeDate']}",
                original={"shortList": sl["tradeDate"], "lines": [x["instrument"] for x in lines]})
            made.append(a.id)
        except gate.GateError as e:
            notes.append(f"basket: {e}")
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
