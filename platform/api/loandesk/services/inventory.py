"""Internal inventory: our own books, from the processor's positions, netted per name.

Every open contract is a position on one of our books (the processor's booking entity): a BORROW puts
shares in the box, a LEND takes them out. Netting borrowed less lent across all books gives what the
firm holds and can lend again without going to the street. Less what lenders have recalled: those shares are
owed back, so they come off the moment the recall lands (``take``). And less the clients' shorts on the latest
stock record (or the CNS fails in the name, if more): those shares went out to the street on their sales, so
only what's left over is ours to lend again. That net long becomes the ``INTERNAL``
availability row, which locates cross against first (``market.cross``): it is firm, where venue
supply is only indicative. A book that is net short elsewhere still nets against it; a name that is
net short overall has no internal supply.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, events
from ..models import Availability, ClientShort, DeliveryFail, InternalPosition, SyncCursor, Trade
from .market import INTERNAL

AS_OF = "inventory_as_of"


def flatten(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Processor positions (instrument, side, booking entity) to one figure per name across books."""
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {"borrowed": 0, "lent": 0, "fee_qty": 0.0,
                                                           "books": defaultdict(lambda: {"borrowed": 0, "lent": 0})})
    for p in positions:
        n, side, qty = out[p["instrumentId"]], p["side"], int(p["openQuantity"])
        book = n["books"][p["bookingEntity"]]
        if side == "BORROW":
            n["borrowed"] += qty
            book["borrowed"] += qty
            n["fee_qty"] += (p.get("avgFeeBps") or 0) * qty
        else:
            n["lent"] += qty
            book["lent"] += qty
    return {iid: {"borrowed": n["borrowed"], "lent": n["lent"], "net": n["borrowed"] - n["lent"],
                  "costBps": round(n["fee_qty"] / n["borrowed"], 2) if n["borrowed"] else None,
                  "books": {b: dict(v) for b, v in n["books"].items()}} for iid, n in out.items()}


async def apply(s: AsyncSession, positions: list[dict[str, Any]], as_of: datetime | None = None) -> int:
    """Replace the per-book positions and the netted INTERNAL availability. Returns names with internal supply."""
    as_of = as_of or clock.now()
    flat = flatten(positions)
    # When the box was last read: a fill booked after this isn't in it yet (the short list counts it apart).
    cur = await s.get(SyncCursor, AS_OF)
    if cur is None:
        s.add(SyncCursor(name=AS_OF, value=clock.iso(as_of)))
    else:
        cur.value = clock.iso(as_of)
    await s.execute(delete(InternalPosition))
    for iid, n in flat.items():
        for book, b in n["books"].items():
            s.add(InternalPosition(instrument_id=iid, book=book, borrowed=b["borrowed"], lent=b["lent"],
                                   avg_borrow_fee_bps=n["costBps"], as_of=as_of))
    have = {a.instrument_id: a for a in
            (await s.execute(select(Availability).where(Availability.source == INTERNAL))).scalars()}
    for iid, q in (await s.execute(select(Trade.instrument_id, Trade.recalled_qty).where(
            Trade.side == "BORROW", Trade.loan_status == "OPEN", Trade.recalled_qty > 0,
            Trade.sfp_contract_id.is_not(None)))).all():
        if iid in flat:
            flat[iid]["net"] -= q  # owed back on a recall: not ours to lend
    for iid, q in (await owed(s)).items():
        if iid in flat:
            flat[iid]["net"] -= q  # delivered on the clients' shorts
    long = {iid: n for iid, n in flat.items() if n["net"] > 0}
    for iid, a in have.items():  # names we no longer hold go to zero, so the ledger never lends what's gone
        if iid not in long:
            a.qty, a.as_of = 0, as_of
    for iid, n in long.items():
        a = have.get(iid)
        if a is None:
            a = Availability(instrument_id=iid, source=INTERNAL, qty=0, lenders=0, as_of=as_of)
            s.add(a)
        # ``lenders`` counts our books holding the name; the fee is what the shares cost us to borrow.
        a.qty, a.lenders, a.indicative_fee_bps, a.as_of = n["net"], sum(
            1 for b in n["books"].values() if b["borrowed"] > b["lent"]), n["costBps"], as_of
    events.publish_later(s, "market", {"type": "inventory", "instruments": len(long)})
    return len(long)


async def refresh(s: AsyncSession) -> int:
    """What's ours to lend changed without a new read of the box (a stock record, a CNS fail): net it again from
    the positions we last read, keeping when that was."""
    rows = (await s.execute(select(InternalPosition))).scalars().all()
    cur = await s.get(SyncCursor, AS_OF)
    if cur is None:
        return 0
    positions = [p for r in rows for p in (
        {"instrumentId": r.instrument_id, "side": "BORROW", "openQuantity": r.borrowed, "bookingEntity": r.book,
         "avgFeeBps": r.avg_borrow_fee_bps},
        {"instrumentId": r.instrument_id, "side": "LEND", "openQuantity": r.lent, "bookingEntity": r.book}) if
        p["openQuantity"]]
    return await apply(s, positions, clock.parse_ts(cur.value))


async def owed(s: AsyncSession) -> dict[str, int]:
    """Per name, the shares the box delivers for clients: their shorts at the latest close, or the name's CNS
    fails not delivered yet if those are more (as the short list counts its need)."""
    latest = (await s.execute(select(func.max(ClientShort.trade_date)))).scalar()
    out: dict[str, int] = defaultdict(int)
    if latest is not None:
        for iid, q in (await s.execute(select(ClientShort.instrument_id, func.sum(ClientShort.short_qty)).where(
                ClientShort.trade_date == latest, ClientShort.counterparty != "-")
                .group_by(ClientShort.instrument_id))).all():
            out[iid] = int(q or 0)
    for iid, q in (await s.execute(select(DeliveryFail.instrument_id, func.sum(DeliveryFail.open_qty)).where(
            DeliveryFail.status.in_(("OPEN", "CLOSED_OUT"))).group_by(DeliveryFail.instrument_id))).all():
        out[iid] = max(out[iid], int(q or 0))
    return out


async def take(s: AsyncSession, instrument_id: str, qty: int) -> None:
    """Shares leave the box before the next read of the processor shows it (a recall, a return)."""
    a = (await s.execute(select(Availability).where(Availability.source == INTERNAL,
                                                    Availability.instrument_id == instrument_id))).scalar()
    if a is not None and qty > 0:
        a.qty = max(a.qty - qty, 0)
        events.publish_later(s, "market", {"type": "inventory", "instrumentId": instrument_id})


async def for_instrument(s: AsyncSession, instrument_id: str) -> list[dict[str, Any]]:
    rows = (await s.execute(select(InternalPosition).where(InternalPosition.instrument_id == instrument_id)
                            .order_by(InternalPosition.book))).scalars()
    return [{"book": r.book, "borrowed": r.borrowed, "lent": r.lent, "net": r.borrowed - r.lent,
             "asOf": clock.iso(r.as_of)} for r in rows]
