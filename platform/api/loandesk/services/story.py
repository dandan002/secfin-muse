"""A trade's story: every event on it and on what it came from or led to, as one timeline.

The event log is per entity. Compliance asks about a trade, but its story starts in a chat message or a locate,
runs through the agent's card and the trader's approval to an order, a fill, a booking, a break and its fix. This
follows the links the desk already keeps (a trade's order, an order's card, a card's source, the cards that name
a trade) out from any one id, a few hops, and returns their events in time order.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock
from ..models import Action, ChatMessage, Event, Locate, Order, Trade

HOPS = 4
CAP = 60  # ids: enough for a trade and its lifecycle, never the whole book


async def _neighbours(s: AsyncSession, i: str) -> set[str]:
    out: set[str] = set()
    if (t := await s.get(Trade, i)) is not None:
        if t.order_id:
            out.add(t.order_id)
        out |= set((await s.execute(select(Action.id).where(Action.source_ref == t.id))).scalars())
    elif (o := await s.get(Order, i)) is not None:
        if o.action_id:
            out.add(o.action_id)
        out |= set((await s.execute(select(Trade.id).where(Trade.order_id == o.id))).scalars())
    elif (a := await s.get(Action, i)) is not None:
        if a.source_ref and ":" not in a.source_ref:  # "kind:key" refs name a short-list line, not an entity
            out.add(a.source_ref)
        for k in ("tradeId", "locateId"):
            if isinstance(v := (a.payload or {}).get(k), str):
                out.add(v)
        out |= set((await s.execute(select(Order.id).where(Order.action_id == a.id))).scalars())
    elif (loc := await s.get(Locate, i)) is not None:
        if loc.source_ref:
            out.add(loc.source_ref)
        out |= set((await s.execute(select(Action.id).where(Action.source_ref == loc.id))).scalars())
    elif await s.get(ChatMessage, i) is not None:
        out |= set((await s.execute(select(Action.id).where(Action.source_ref == i))).scalars())
        out |= set((await s.execute(select(Locate.id).where(Locate.source_ref == i))).scalars())
    return out


async def story(s: AsyncSession, root: str) -> dict[str, Any]:
    seen, frontier = {root}, {root}
    for _ in range(HOPS):
        nxt: set[str] = set()
        for i in frontier:
            nxt |= await _neighbours(s, i)
        frontier = nxt - seen
        seen |= frontier
        if not frontier or len(seen) >= CAP:
            break
    ids = sorted(seen)[:CAP]
    rows = (await s.execute(select(Event).where(Event.entity_id.in_(ids)).order_by(Event.ts, Event.id))).scalars()
    events = [{"id": e.id, "ts": clock.iso(e.ts), "entity": e.entity, "entityId": e.entity_id, "type": e.type,
               "actor": e.actor, "after": e.after} for e in rows]
    return {"root": root, "ids": ids, "events": events}
