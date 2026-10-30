"""Fails to deliver at CNS on our clients' shorts, and Reg SHO Rule 204.

A client's short settles the business day after the trade. If we don't have the shares in our account then, the
delivery fails at CNS: the gateway's books carry the fail, per client (``ingest``). Rule 204(a) gives us until
regular trading opens the next business day to close it out, by borrowing or buying the shares. A fail is owed
on the short list like any other short (it's the same client position, not yet delivered): what changes is the
deadline, so the name's cover card carries it and the list puts the name first.

``assess`` runs on every sync. Per name, the fails take the box oldest first: as far as the short list's cover
(borrows done, booked or not) reaches past the clients' other shorts, a fail is closed out; as far as shares
already settled into our account reach, they're delivered against it (``policy:CNS-DELIVER``). One still open at
the deadline is missed, and Rule 204(b) puts the name in the penalty box: until its shares are delivered, a locate
in it needs shares we hold (pre-borrow), not street supply (see ``locates``).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from refdata import calendar

from .. import clock, events
from ..adapters import AdapterError, systems
from ..models import Action, DeliveryFail, Instrument, Trade
from ..settings import get_settings
from . import inventory
from .booking import open_qty

MARKET_OPEN = time(9, 30)  # New York: regular trading hours start
ACTIVE = ("OPEN", "CLOSED_OUT")  # not delivered yet


def deadline(settle_date: date) -> datetime:
    """Rule 204(a) for a short sale: closed out by the start of regular trading the business day after the fail."""
    return datetime.combine(calendar.add_business_days(settle_date, 1), MARKET_OPEN, tzinfo=clock.NEW_YORK)


def fail_json(f: DeliveryFail, inst: Instrument | None = None) -> dict[str, Any]:
    return {"id": f.id, "counterparty": f.counterparty, "instrumentId": f.instrument_id,
            "composite": inst.composite if inst else None, "settlementDate": f.settle_date.isoformat(),
            "quantity": f.qty, "openQty": f.open_qty, "status": f.status, "missed": f.missed,
            "preBorrow": f.missed and f.status != "DELIVERED", "deadline": clock.iso(f.deadline),
            "receivedAt": clock.iso(f.received_at), "closedOutAt": clock.iso(f.closed_out_at),
            "deliveredAt": clock.iso(f.delivered_at), "deliveries": f.deliveries or []}


async def ingest(s: AsyncSession, rows: list[dict[str, Any]]) -> list[DeliveryFail]:
    """New fails from the gateway. One we hold already is left alone: we track its delivery ourselves."""
    made = []
    for r in rows:
        if r["status"] != "OPEN" or await s.get(DeliveryFail, r["failId"]) is not None:
            continue
        settle = date.fromisoformat(r["settlementDate"])
        f = DeliveryFail(id=r["failId"], counterparty=r["counterparty"], instrument_id=r["instrumentId"],
                         settle_date=settle, qty=int(r["quantity"]), open_qty=int(r["outstanding"]),
                         deadline=deadline(settle).astimezone(UTC), status="OPEN", missed=False,
                         received_at=clock.now(), deliveries=[])
        s.add(f)
        made.append(f)
        events.record(s, "fail", f.id, "failed_to_deliver", fail_json(f), actor="gateway:cns")
    if made:
        await s.flush()
        await inventory.refresh(s)
        await _redraft(s, {f.instrument_id for f in made}, ", ".join(f.id for f in made))
        events.publish_later(s, "short_list", {"type": "fails", "fails": [f.id for f in made]})
    return made


async def _redraft(s: AsyncSession, names: set[str], cause: str) -> None:
    """A cover drafted before the fail doesn't carry its deadline: supersede it and draft again."""
    from .. import gate
    from . import shortlist

    if not get_settings().draft_covers:
        return
    for a in (await s.execute(select(Action).where(
            Action.status == "PENDING", Action.source_ref.in_([f"{shortlist.SOURCE}:{i}" for i in names])))).scalars():
        await gate.reject(s, a, "rules:rule-204", f"superseded by the CNS fail {cause}: redrafted with its close-out",
                          status="SUPERSEDED")
    await shortlist.draft(s, by="rules:rule-204")


async def active(s: AsyncSession, instrument_ids: list[str] | None = None) -> dict[str, list[DeliveryFail]]:
    """Fails not delivered yet, per name, oldest first."""
    q = select(DeliveryFail).where(DeliveryFail.status.in_(ACTIVE)).order_by(DeliveryFail.settle_date,
                                                                            DeliveryFail.id)
    if instrument_ids is not None:
        q = q.where(DeliveryFail.instrument_id.in_(instrument_ids))
    out: dict[str, list[DeliveryFail]] = defaultdict(list)
    for f in (await s.execute(q)).scalars():
        out[f.instrument_id].append(f)
    return out


async def pre_borrow(s: AsyncSession, instrument_id: str) -> DeliveryFail | None:
    """Is the name in the penalty box (Rule 204(b))? The missed fail that put it there."""
    return (await s.execute(select(DeliveryFail).where(
        DeliveryFail.instrument_id == instrument_id, DeliveryFail.missed.is_(True),
        DeliveryFail.status.in_(ACTIVE)).order_by(DeliveryFail.deadline).limit(1))).scalar()


async def pre_borrow_names(s: AsyncSession) -> set[str]:
    return set((await s.execute(select(DeliveryFail.instrument_id).where(
        DeliveryFail.missed.is_(True), DeliveryFail.status.in_(ACTIVE)))).scalars())


async def unsettled(s: AsyncSession, instrument_ids: list[str]) -> dict[str, int]:
    """Borrows the short list counts as cover whose shares haven't settled into our account yet."""
    out: dict[str, int] = defaultdict(int)
    for t in (await s.execute(select(Trade).where(
            Trade.instrument_id.in_(instrument_ids), Trade.side == "BORROW", Trade.loan_status == "OPEN",
            Trade.settle_status.is_distinct_from("SETTLED"), Trade.settle_status.is_distinct_from("FAILING")))
              ).scalars():
        out[t.instrument_id] += max(open_qty(t) - (t.recalled_qty or 0), 0)
    return out


async def assess(s: AsyncSession) -> list[str]:
    """Close out, deliver and flag every fail not delivered yet, against the latest short list."""
    from . import shortlist

    by_name = await active(s)
    if not by_name:
        return []
    sl = await shortlist.build(s)
    lines = {ln["instrumentId"]: ln for ln in sl["lines"]}
    waiting = await unsettled(s, list(by_name))
    now, auto, notes = clock.now(), get_settings().auto_deliver_fails, []
    for iid, fs in by_name.items():
        ln = lines.get(iid) or {"need": 0, "cover": 0}
        failing = sum(f.open_qty for f in fs)
        need, cover = max(ln["need"], failing), ln["cover"]
        # The clients' other shorts were delivered: what the cover reaches past them goes to the fails.
        closable = max(failing - max(need - cover, 0), 0)
        deliverable = max(failing - max(need - (cover - waiting[iid]), 0), 0) if auto else 0
        for f in fs:
            before = fail_json(f)
            # Closed out late is still missed: the name stays pre-borrow until the shares are delivered.
            if f.status == "OPEN" and not f.missed and now >= clock.parse_ts(clock.iso(f.deadline)):
                f.missed = True
                events.record(s, "fail", f.id, "close_out_missed", fail_json(f), before=before,
                              actor="rules:rule-204")
                notes.append(f"{f.id}: not closed out by the open; {iid} is pre-borrow until it's delivered")
            take = min(f.open_qty, closable)
            closable -= take
            if take == f.open_qty and f.status == "OPEN":
                f.closed_out_at, f.status = now, "CLOSED_OUT"
                events.record(s, "fail", f.id, "closed_out", fail_json(f), before=before, actor="rules:rule-204")
            give = min(f.open_qty, deliverable)
            if give > 0:
                deliverable -= give
                if (err := await deliver(s, f, give, by="policy:CNS-DELIVER")) is not None:
                    notes.append(err)
                    deliverable = 0
    return notes


async def deliver(s: AsyncSession, f: DeliveryFail, qty: int, *, by: str) -> str | None:
    """Deliver ``qty`` against a fail at CNS. Returns why not, if the gateway refused."""
    before = fail_json(f)
    try:
        await systems().gateway.deliver_fail(f.id, qty, ref=f"{by}:{clock.iso(clock.now())}"[:64])
    except AdapterError as e:
        events.record(s, "fail", f.id, "delivery_failed", {**before, "error": str(e)}, actor=by)
        return f"{f.id}: delivery refused: {e}"
    f.open_qty -= qty
    f.deliveries = [*(f.deliveries or []), {"ts": clock.iso(clock.now()), "quantity": qty, "by": by}]
    if f.open_qty == 0:
        f.status, f.delivered_at = "DELIVERED", clock.now()
        f.closed_out_at = f.closed_out_at or f.delivered_at
    events.record(s, "fail", f.id, "delivered" if f.open_qty == 0 else "delivered_part", fail_json(f),
                  before=before, actor=by)
    events.publish_later(s, "short_list", {"type": "fails", "fails": [f.id]})
    return None


async def listing(s: AsyncSession, *, everything: bool = False, limit: int = 300) -> list[dict[str, Any]]:
    q = select(DeliveryFail).order_by(DeliveryFail.deadline.desc(), DeliveryFail.id).limit(limit)
    if not everything:
        q = q.where(DeliveryFail.status.in_(ACTIVE))
    rows = list((await s.execute(q)).scalars())
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(
        Instrument.id.in_({f.instrument_id for f in rows})))).scalars()}
    return [fail_json(f, insts.get(f.instrument_id)) for f in rows]
