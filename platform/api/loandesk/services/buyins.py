"""Buy-ins: shares bought in against us, because we didn't deliver them in time.

A lender's: a recall not returned by its due date (and a business day's grace) is bought in under the lending
agreement. The lender buys the shares in the market, the loan closes for them, and the lender keeps that share of
our collateral against the cost; we owe the rest (``amount_due``). It arrives at the processor for a Venue A fill or
a bilateral trade, and on the venue for a Venue B loan, where we book it at the processor ourselves. On the
borrow it answers the recall for those shares: they come off the loan and off what's recalled, and any return card
for them at the gate is superseded (one for what's still recalled is drafted again).

The buyer's at CNS: a client's short that failed and stayed open. The buyer buys the shares in; we owe the purchase
price over the sale price, charged on to the client (Rule 204(d)), and the shares cover the client's short, so the
fail is closed (``BOUGHT_IN``) and the name leaves the penalty box.
"""

from __future__ import annotations

from datetime import UTC, date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from refdata import calendar

from .. import clock, events, rules
from ..adapters import AdapterError, systems
from ..models import Action, BuyIn, DeliveryFail, Instrument, Trade
from ..settings import get_settings
from . import inventory
from .booking import open_qty, trade_json

SOURCE = "recall"  # return cards for recalls carry source_ref "recall:<key>"


def buy_in_json(b: BuyIn, inst: Instrument | None = None) -> dict[str, Any]:
    return {"id": b.id, "kind": b.kind, "origin": b.origin, "tradeId": b.trade_id, "failId": b.fail_id,
            "counterparty": b.counterparty, "instrumentId": b.instrument_id,
            "composite": inst.composite if inst else None, "quantity": b.qty, "price": b.price, "ccy": b.ccy,
            "amountDue": b.amount_due, "amountDueUsd": round(b.amount_due * rules.FX_USD.get(b.ccy, 1.0), 2),
            "boughtInAt": clock.iso(b.bought_in_at), "receivedAt": clock.iso(b.received_at)}


async def on_recall(s: AsyncSession, t: Trade, r: dict[str, Any], *, origin: str) -> BuyIn | None:
    """A lender bought in shares on one of our borrows. ``r``: the processor's buy-in, or the venue's loan entry."""
    if await s.get(BuyIn, r["buyInId"]) is not None:
        return None
    qty = min(int(r["quantity"]), open_qty(t))
    before = trade_json(t)
    b = BuyIn(id=r["buyInId"], kind="RECALL", origin=origin, trade_id=t.id, counterparty=t.cp_code,
              instrument_id=t.instrument_id, qty=qty, price=float(r["price"]), ccy=r.get("ccy") or t.ccy,
              amount_due=float(r["amountDue"]), detail=r,
              bought_in_at=clock.parse_ts(r.get("ts") or clock.iso(clock.now())), received_at=clock.now())
    s.add(b)
    entry: dict[str, Any] = {"quantity": qty, "price": b.price, "amountDue": b.amount_due, "buyInId": b.id}
    if origin == "venue_b" and t.sfp_contract_id:  # the venue closed the loan: our books at the processor follow
        try:
            await systems().processor.lifecycle(t.sfp_contract_id, "BUY_IN", quantity=qty, price=b.price)
        except AdapterError as e:
            entry["booked"], entry["error"] = False, str(e)
    t.open_qty = open_qty(t) - qty
    t.recalled_qty = max((t.recalled_qty or 0) - qty, 0)
    if t.open_qty == 0:
        t.loan_status = "CLOSED"
    if not t.recalled_qty and not any(e.get("againstRecall") and e.get("settleStatus") != "SETTLED"
                                      for e in t.lifecycle or []):
        t.recall_due = None  # nothing left owed on the recall: the buy-in answered it
    t.lifecycle = [*(t.lifecycle or []), {"ts": clock.iso(clock.now()), "event": "BOUGHT_IN", "by": origin, **entry}]
    events.record(s, "trade", t.id, "bought_in", trade_json(t), before=before, actor=f"{origin}:{t.cp_code}")
    events.record(s, "buy_in", b.id, "received", buy_in_json(b), actor=f"{origin}:{t.cp_code}")
    await _redraft_returns(s, t, b)
    events.publish_later(s, "trade", {"type": "bought_in", "tradeId": t.id})
    return b


async def _redraft_returns(s: AsyncSession, t: Trade, b: BuyIn) -> None:
    """The return cards for the recall asked for shares now bought in: supersede them, and draft one for whatever
    is still recalled. What's spare in the name changed too, so the short list redrafts."""
    from .. import gate
    from .recalls import _after

    keys = []
    for a in (await s.execute(select(Action).where(Action.status == "PENDING", Action.kind == "lifecycle",
                                                   Action.source_ref.like(f"{SOURCE}:%")))).scalars().all():
        if a.payload.get("tradeId") == t.id:
            keys.append(a.source_ref.split(":", 1)[1])
            await gate.reject(s, a, f"rules:{SOURCE}", f"bought in ({b.id}): {b.qty:,} no longer to return",
                              status="SUPERSEDED")
    if keys and t.recalled_qty and t.loan_status == "OPEN":
        inst = await s.get(Instrument, t.instrument_id)
        name = inst.composite if inst else t.instrument_id
        await gate.propose(
            s, kind="lifecycle", payload={"tradeId": t.id, "event": "RETURN", "quantity": t.recalled_qty},
            proposed_by=f"rules:{SOURCE}", title=f"recall from {t.cp_code}: return {t.recalled_qty:,} {name} "
                                                 f"(after a buy-in)",
            rationale=f"{t.cp_code} bought in {b.qty:,} {name} at {b.price:g} ({b.id}); {t.recalled_qty:,} is still "
                      f"recalled on {t.id}, overdue since {t.recall_due.isoformat() if t.recall_due else '?'}: "
                      f"return it before that's bought in too.",
            risk_flags=["warning: overdue: the lender is buying in"], source_ref=f"{SOURCE}:{keys[0]}",
            original={"buyIn": b.id})
    await _after(s, {t.instrument_id}, [], f"the buy-in {b.id}")


async def on_cns(s: AsyncSession, r: dict[str, Any]) -> BuyIn | None:
    """The buyer bought in a client fail we didn't deliver."""
    if await s.get(BuyIn, r["buyInId"]) is not None:
        return None
    f = await s.get(DeliveryFail, r["failId"])
    inst = await s.get(Instrument, r["instrumentId"])
    b = BuyIn(id=r["buyInId"], kind="CNS", origin="cns", fail_id=r["failId"], counterparty=r["counterparty"],
              instrument_id=r["instrumentId"], qty=int(r["quantity"]), price=float(r["price"]),
              ccy=inst.ccy if inst else "USD", amount_due=float(r["amountDue"]), detail=r,
              bought_in_at=clock.parse_ts(r["boughtInAt"]), received_at=clock.now())
    s.add(b)
    if f is not None:
        from .fails import fail_json

        before = fail_json(f)
        f.open_qty = max(f.open_qty - b.qty, 0)
        if f.open_qty == 0:
            f.status = "BOUGHT_IN"
        f.deliveries = [*(f.deliveries or []), {"ts": clock.iso(clock.now()), "quantity": b.qty,
                                                "by": "cns:buy-in", "buyInId": b.id, "price": b.price}]
        events.record(s, "fail", f.id, "bought_in", fail_json(f), before=before, actor="cns")
    events.record(s, "buy_in", b.id, "received", buy_in_json(b, inst), actor="cns")
    await s.flush()
    await inventory.refresh(s)
    if get_settings().draft_covers:  # the shares cover the client's short: a cover drafted for it isn't needed
        from . import shortlist

        await shortlist.draft(s, by="rules:buy-in")
    events.publish_later(s, "short_list", {"type": "bought_in", "buyIn": b.id})
    return b


async def covered_since(s: AsyncSession, trade_date: date | None) -> dict[str, int]:
    """CNS buy-ins since a close, per name: they cover the clients' shorts, which its stock record still shows."""
    if trade_date is None:
        return {}
    since = calendar.close_at(trade_date).astimezone(UTC)
    return {iid: int(q or 0) for iid, q in (await s.execute(
        select(BuyIn.instrument_id, func.sum(BuyIn.qty)).where(BuyIn.kind == "CNS", BuyIn.bought_in_at > since)
        .group_by(BuyIn.instrument_id))).all()}


async def listing(s: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
    rows = list((await s.execute(select(BuyIn).order_by(BuyIn.bought_in_at.desc()).limit(limit))).scalars())
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(
        Instrument.id.in_({b.instrument_id for b in rows})))).scalars()}
    return [buy_in_json(b, insts.get(b.instrument_id)) for b in rows]
