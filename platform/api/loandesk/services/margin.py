"""Margin calls (spec module 5, marks): a card per call on our trades, worked in the desk's counterparty order.

The processor marks every open contract (nightly, or ``POST /marks/run``) and a mark that moves the required
collateral makes a call: ``WE_ARE_CALLED`` we pay cash collateral (a lender wants more, or a borrower wants some
back), ``WE_CALL`` we're owed it. Each becomes a ``margin`` card at the gate: approved, it instructs the payment
(or the expected receipt) at the custodian.

The desk's margin order (``policy``) decides which calls are drafted first, then the largest. Paying calls count
against the day's collateral budget, if the desk set one, in that order: a payment past it carries a warning, so
the autopilot leaves it for a trader.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, events, policy, rules
from ..adapters import AdapterError, systems
from ..models import Action, Counterparty, Trade
from .booking import open_qty
from .lifecycle import _log

SOURCE = "margin"  # margin cards carry source_ref "margin:<contract id>:<mark date>"
OVER = "warning: over today's collateral budget"


class MarginError(ValueError):
    pass


def normalise(c: dict[str, Any]) -> dict[str, Any]:
    """A call from ``/marks/run`` (signed ``callAmount``) or ``/margin-calls`` (``amount``), in one shape."""
    direction = c.get("direction") or (c.get("marginCall") or {}).get("direction")
    amount = c["amount"] if c.get("amount") is not None else c.get("callAmount")
    return {"contractId": c["contractId"], "markDate": c["markDate"], "direction": direction,
            "amount": round(abs(float(amount or 0)), 2), "ccy": c.get("ccy") or "USD",
            "loanValue": c.get("loanValue"), "requiredCollateral": c.get("requiredCollateral"),
            "postedCollateral": c.get("postedCollateral")}


def _usd(amount: float, ccy: str) -> float:
    return amount * rules.FX_USD.get(ccy, 1.0)


def _money(amount: float, ccy: str) -> str:
    return f"{ccy} {amount:,.2f}"


async def paid_today(s: AsyncSession) -> float:
    """Margin we've committed to pay today, in dollars: payment cards inside the budget, not rejected."""
    day, usd = clock.local_date(), 0.0
    rows = (await s.execute(select(Action).where(Action.kind == "margin", Action.status.in_(
        ("PENDING", "APPROVED", "EXECUTED"))))).scalars()
    for a in rows:
        p = a.payload or {}
        if p.get("direction") == "PAY" and clock.local_date(a.created_at) == day and OVER not in (a.risk_flags or []):
            usd += _usd(float(p.get("amount") or 0), p.get("ccy") or "USD")
    return usd


async def draft(s: AsyncSession, calls: list[dict[str, Any]], *, by: str) -> list[str]:
    """A card per new call on one of our trades, the desk's margin order first, then the largest."""
    from .. import gate

    calls = [normalise(c) for c in calls]
    calls = [c for c in calls if c["amount"] >= 1 and c["direction"] in ("WE_CALL", "WE_ARE_CALLED")]
    if not calls:
        return []
    trades = {t.sfp_contract_id: t for t in (await s.execute(select(Trade).where(
        Trade.sfp_contract_id.in_({c["contractId"] for c in calls})))).scalars()}
    refs = {f"{SOURCE}:{c['contractId']}:{c['markDate']}" for c in calls}
    have = set((await s.execute(select(Action.source_ref).where(  # a failed card's call is drafted again
        Action.source_ref.in_(refs), Action.status != "FAILED"))).scalars())
    todo = [(c, trades[c["contractId"]]) for c in calls
            if c["contractId"] in trades and f"{SOURCE}:{c['contractId']}:{c['markDate']}" not in have]
    todo.sort(key=lambda ct: (policy.rank("margin", ct[1].cp_code), -_usd(ct[0]["amount"], ct[0]["ccy"])))
    pr = policy.priority()
    budget, used = pr.marginBudgetUsd, await paid_today(s)
    made = []
    for c, t in todo:
        pay = c["direction"] == "WE_ARE_CALLED"
        usd = _usd(c["amount"], c["ccy"])
        place = policy.rank("margin", t.cp_code)
        order = (f"{t.cp_code} is #{place + 1} in the desk's margin order" if place < len(pr.margin)
                 else f"{t.cp_code} isn't in the desk's margin order: after those, largest first")
        flags = []
        if pay and budget is not None:
            if used + usd > budget:
                flags.append(OVER)
                order += (f"; ${used:,.0f} of today's ${budget:,.0f} collateral budget is committed, so this "
                          f"${usd:,.0f} waits for a trader")
            else:
                used += usd
        what = (f"pay {_money(c['amount'], c['ccy'])} to {t.cp_code}" if pay
                else f"receive {_money(c['amount'], c['ccy'])} from {t.cp_code}")
        try:
            a = await gate.propose(
                s, kind="margin", proposed_by=by, source_ref=f"{SOURCE}:{c['contractId']}:{c['markDate']}",
                payload={"tradeId": t.id, "contractId": c["contractId"], "markDate": c["markDate"],
                         "direction": "PAY" if pay else "RECEIVE", "amount": c["amount"], "ccy": c["ccy"],
                         "counterparty": t.cp_code},
                title=f"margin call: {what} on {t.id}",
                rationale=(f"The {c['markDate']} mark of {t.id} ({t.side.lower()} {open_qty(t):,} with {t.cp_code}): "
                           + (f"loan value {c['loanValue']:,.2f}, collateral required {c['requiredCollateral']:,.2f}"
                              f" against {c['postedCollateral']:,.2f} posted. "
                              if c["loanValue"] is not None and c["postedCollateral"] is not None else "")
                           + ("They call us: we deliver cash collateral. " if pay
                              else "We call them: the cash is ours to receive. ") + order + "."),
                risk_flags=flags, original={"call": c})
            made.append(a.id)
        except gate.GateError:
            break  # the kill switch: the rest are drafted on the next mark or poll
    if made:
        events.publish_later(s, "margin", {"type": "drafted", "actions": made})
    return made


async def settle(s: AsyncSession, a: Action, *, by: str) -> dict[str, Any]:
    """Instruct a margin card's cash at the custodian."""
    p = a.payload
    t = await s.get(Trade, p["tradeId"])
    if t is None:
        raise MarginError(f"no trade {p['tradeId']}")
    cp = await s.get(Counterparty, t.cp_code)
    if cp is None or not cp.bic:
        raise MarginError(f"no BIC for {t.cp_code}")
    try:
        r = await systems().custodian.pay({
            "txId": f"MG-{a.id}"[:35], "direction": p["direction"], "amount": p["amount"], "ccy": p["ccy"],
            "counterpartyBic": cp.bic, "reference": f"margin call {p['contractId']} {p['markDate']}"[:140]})
    except AdapterError as e:
        raise MarginError(str(e)) from e
    before = {"lifecycle": len(t.lifecycle or [])}
    _log(t, "MARGIN_PAID" if p["direction"] == "PAY" else "MARGIN_RECEIVABLE", by, amount=p["amount"],
         ccy=p["ccy"], paymentId=r.get("paymentId"), markDate=p["markDate"])
    events.record(s, "trade", t.id, "margin_paid" if p["direction"] == "PAY" else "margin_receivable",
                  {"amount": p["amount"], "ccy": p["ccy"], "paymentId": r.get("paymentId"),
                   "markDate": p["markDate"], "counterparty": t.cp_code}, before=before, actor=by)
    return {"tradeId": t.id, "paymentId": r.get("paymentId"), "status": r.get("status")}
