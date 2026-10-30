"""Loan lifecycle after booking (spec module 5): returns, recalls, re-rates and marks.

Venue B loans live on the venue too, so the venue goes first and the processor follows; a Venue A fill has no
venue lifecycle, so the processor contract is the loan. Re-rates on Venue B wait for the counterparty: the
trade carries ``pending_rerate_bps`` until ``apply_loan`` sees the venue agree or decline.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, events
from ..adapters import AdapterError, systems
from ..models import Trade
from ..settings import get_settings
from . import inventory
from .booking import STATUS_FROM_CUSTODIAN, instruct, open_qty, trade_json


class LifecycleError(ValueError):
    pass


def _log(t: Trade, event: str, by: str, **data: Any) -> None:
    t.lifecycle = [*(t.lifecycle or []), {"ts": clock.iso(clock.now()), "event": event, "by": by, **data}]


def _need_contract(t: Trade) -> str:
    if (t.loan_status or "OPEN") != "OPEN":
        raise LifecycleError(f"trade {t.id} is {t.loan_status}")
    if not t.sfp_contract_id:
        raise LifecycleError(f"trade {t.id} isn't booked yet")
    return t.sfp_contract_id


async def close(s: AsyncSession, t: Trade, quantity: int | None, *, by: str) -> Trade:
    """Return what we borrowed, or recall what we lent: all of it, or ``quantity``."""
    contract = _need_contract(t)
    have = open_qty(t)
    qty = quantity or have
    if qty <= 0 or qty > have:
        raise LifecycleError(f"only {have:,} open")
    event = "RETURN" if t.side == "BORROW" else "RECALL"
    before = trade_json(t)
    try:
        if t.venue == "VENUE_B":
            await systems().venue_b.return_qty(t.venue_trade_id, qty)
    except AdapterError as e:
        raise LifecycleError(str(e)) from e
    t.open_qty = have - qty
    if t.open_qty == 0:
        t.loan_status = "CLOSED"
    entry: dict[str, Any] = {"quantity": qty, "remaining": t.open_qty}
    if event == "RETURN" and t.recalled_qty:  # a return answers the lender's recall first; it stays due until the
        entry["againstRecall"] = min(qty, t.recalled_qty)  # shares settle back with the lender
        t.recalled_qty -= entry["againstRecall"]
    if event == "RETURN" and qty > entry.get("againstRecall", 0):  # recalled shares left the lendable box already
        await inventory.take(s, t.instrument_id, qty - entry.get("againstRecall", 0))
    try:
        await systems().processor.lifecycle(contract, event, quantity=qty)
    except AdapterError as e:  # the venue leg stands; the books need a retry
        entry["booked"], entry["error"] = False, str(e)
    if get_settings().auto_settle:
        try:
            i = await instruct(s, t, leg="RETURN", quantity=qty, by=by)
            entry["settleRef"], entry["settleStatus"] = i["instructionId"], STATUS_FROM_CUSTODIAN.get(i["status"],
                                                                                                      "PENDING")
            t.returns_settling = (t.returns_settling or 0) + 1
        except AdapterError as e:
            entry["settleError"] = str(e)
    elif not t.recalled_qty:
        t.recall_due = None  # nothing instructed to follow: the recall is answered on the books
    _log(t, event, by, **entry)
    events.record(s, "trade", t.id, event.lower(), trade_json(t), before=before, actor=by)
    return t


async def rerate(s: AsyncSession, t: Trade, fee_bps: float, *, by: str) -> Trade:
    contract = _need_contract(t)
    if fee_bps < 0:
        raise LifecycleError("fee can't be negative")
    before = trade_json(t)
    try:
        if t.venue == "VENUE_B":
            await systems().venue_b.rerate(t.venue_trade_id, fee_bps)
            t.pending_rerate_bps = fee_bps
            _log(t, "RERATE_REQUESTED", by, feeBps=fee_bps)
        else:
            await systems().processor.lifecycle(contract, "RERATE", fee_bps=fee_bps)
            _log(t, "RERATE", by, fromBps=t.fee_bps, toBps=fee_bps)
            t.fee_bps = fee_bps
    except AdapterError as e:
        raise LifecycleError(str(e)) from e
    events.record(s, "trade", t.id, "rerate_requested" if t.pending_rerate_bps else "rerated", trade_json(t),
                  before=before, actor=by)
    return t


async def apply_loan(s: AsyncSession, t: Trade, loan: dict[str, Any]) -> bool:
    """Fold a Venue B loan snapshot into our trade: an agreed or declined re-rate. Returns True on a change."""
    if t.pending_rerate_bps is None or loan.get("pendingRerateBps") is not None:
        return False
    before = trade_json(t)
    asked, now_fee = t.pending_rerate_bps, float(loan["feeBps"])
    t.pending_rerate_bps = None
    if abs(now_fee - asked) < 1e-9:
        entry: dict[str, Any] = {"fromBps": t.fee_bps, "toBps": now_fee}
        try:
            await systems().processor.lifecycle(t.sfp_contract_id, "RERATE", fee_bps=now_fee)
        except AdapterError as e:
            entry["booked"], entry["error"] = False, str(e)
        t.fee_bps = now_fee
        _log(t, "RERATE", "venue_b", **entry)
        events.record(s, "trade", t.id, "rerated", trade_json(t), before=before, actor="venue_b")
    else:
        _log(t, "RERATE_DECLINED", "venue_b", feeBps=asked)
        events.record(s, "trade", t.id, "rerate_declined", trade_json(t), before=before, actor="venue_b")
    return True


async def mark(s: AsyncSession, t: Trade, *, by: str) -> dict[str, Any]:
    contract = _need_contract(t)
    try:
        r = await systems().processor.lifecycle(contract, "MARK")
    except AdapterError as e:
        raise LifecycleError(str(e)) from e
    m = r.get("mark", {})
    _log(t, "MARK", by, price=m.get("price"), callAmount=m.get("callAmount"),
         direction=(m.get("marginCall") or {}).get("direction"))
    events.record(s, "trade", t.id, "marked", {"mark": m}, actor=by)
    return m


async def run_marks(s: AsyncSession, *, by: str) -> dict[str, Any]:
    """The processor's daily mark across every open contract; margin calls land on their trades."""
    try:
        r = await systems().processor.run_marks()
    except AdapterError as e:
        raise LifecycleError(str(e)) from e
    calls = {c["contractId"]: c for c in r.get("calls", [])}
    trades = (await s.execute(select(Trade).where(Trade.sfp_contract_id.in_(calls)))).scalars().all() if calls else []
    for t in trades:
        c = calls[t.sfp_contract_id]
        _log(t, "MARGIN_CALL", by, price=c["price"], callAmount=c["callAmount"], ccy=c["ccy"],
             direction=c["marginCall"]["direction"])
        events.record(s, "trade", t.id, "margin_call", c, actor=by)
    return {"marked": r.get("marked", 0), "marginCalls": len(calls), "ours": len(trades),
            "calls": [{**c, "tradeId": next((t.id for t in trades if t.sfp_contract_id == k), None)}
                      for k, c in calls.items()]}
