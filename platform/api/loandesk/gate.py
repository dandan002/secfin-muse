"""The approval gate (spec module 6). The only door from a proposal to an external side effect.

The agent (or any proposer) may only ``propose()``; a trader approves, edits, rejects or takes over,
and ``approve()`` dispatches to the executor for that kind. The kill switch freezes every
non-trader proposal and execution at once. Trader actions from the manual UI go straight to the
services: the trader is the human in the loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import clock, events
from .models import Action, Locate, Order, Trade

Executor = Callable[[AsyncSession, Action, str], Awaitable[dict[str, Any]]]
_executors: dict[str, Executor] = {}
_kill = {"on": False, "by": None, "at": None}


class GateError(ValueError):
    pass


def executor(kind: str) -> Callable[[Executor], Executor]:
    def deco(fn: Executor) -> Executor:
        _executors[kind] = fn
        return fn

    return deco


def kinds() -> list[str]:
    return sorted(_executors)


def kill_switch() -> dict[str, Any]:
    return dict(_kill)


def set_kill_switch(on: bool, by: str) -> dict[str, Any]:
    _kill.update(on=on, by=by, at=clock.iso(clock.now()))
    return kill_switch()


def action_json(a: Action) -> dict[str, Any]:
    return {"id": a.id, "kind": a.kind, "title": a.title, "payload": a.payload, "original": a.original,
            "proposedBy": a.proposed_by, "rationale": a.rationale, "riskFlags": a.risk_flags, "status": a.status,
            "approver": a.approver, "decidedAt": clock.iso(a.decided_at), "result": a.result,
            "transcript": a.transcript, "sourceRef": a.source_ref, "createdAt": clock.iso(a.created_at)}


async def propose(s: AsyncSession, *, kind: str, title: str, payload: dict[str, Any], proposed_by: str,
                  rationale: str | None = None, risk_flags: list[str] | None = None,
                  original: dict[str, Any] | None = None, transcript: list[Any] | None = None,
                  source_ref: str | None = None) -> Action:
    if kind not in _executors:
        raise GateError(f"unknown action kind {kind!r}")
    if _kill["on"] and not proposed_by.startswith("trader:"):
        raise GateError("kill switch is on: agent proposals are frozen")
    a = Action(id=events.new_id("act"), kind=kind, title=title, payload=payload, original=original,
               proposed_by=proposed_by, rationale=rationale, risk_flags=risk_flags or [], status="PENDING",
               transcript=transcript or [], source_ref=source_ref, created_at=clock.now())
    s.add(a)
    await s.flush()
    events.record(s, "action", a.id, "proposed", action_json(a), actor=proposed_by)
    return a


async def approve(s: AsyncSession, a: Action, approver: str, payload: dict[str, Any] | None = None) -> Action:
    if a.status != "PENDING":
        raise GateError(f"action {a.id} is {a.status}")
    if _kill["on"]:
        raise GateError("kill switch is on: nothing executes until it's off")
    before = action_json(a)
    if payload is not None:
        a.transcript = [*a.transcript, {"ts": clock.iso(clock.now()), "step": "edited", "by": approver,
                                        "from": a.payload, "to": payload}]
        a.payload = payload
    a.status, a.approver, a.decided_at = "APPROVED", approver, clock.now()
    try:
        a.result = await _executors[a.kind](s, a, approver)
        a.status = "EXECUTED"
    except Exception as e:  # the card shows why; the action stays auditable
        a.status, a.result = "FAILED", {"error": str(e)}
    events.record(s, "action", a.id, a.status.lower(), action_json(a), before=before, actor=approver)
    return a


async def reject(s: AsyncSession, a: Action, approver: str, reason: str | None = None,
                 status: str = "REJECTED") -> Action:
    if a.status != "PENDING":
        raise GateError(f"action {a.id} is {a.status}")
    before = action_json(a)
    a.status, a.approver, a.decided_at = status, approver, clock.now()
    a.result = {"reason": reason} if reason else None
    events.record(s, "action", a.id, status.lower(), action_json(a), before=before, actor=approver)
    return a


async def listing(s: AsyncSession, status: str | None = "PENDING", limit: int = 200) -> list[dict[str, Any]]:
    q = select(Action).order_by(Action.created_at.desc()).limit(limit)
    if status:
        q = q.where(Action.status == status.upper())
    return [action_json(a) for a in (await s.execute(q)).scalars()]


# -- executors ----------------------------------------------------------------------------------
@executor("locate_decision")
async def _locate_decision(s: AsyncSession, a: Action, approver: str) -> dict[str, Any]:
    from .services import locates

    p = a.payload
    loc = await s.get(Locate, p["locateId"])
    if loc is None:
        raise GateError(f"no locate {p['locateId']}")
    await locates.decide(s, loc, p["decision"], p.get("quantity"), by=f"{a.proposed_by}+{approver}",
                         rationale=a.rationale, fee_bps=p.get("feeBps"))
    return {"locateId": loc.id, "status": loc.status, "qtyApproved": loc.qty_approved}


@executor("order")
async def _order(s: AsyncSession, a: Action, approver: str) -> dict[str, Any]:
    from .services import orders

    o = await orders.create(s, orders.OrderIn.model_validate(a.payload), by=f"{a.proposed_by}+{approver}",
                            action_id=a.id)
    return {"orderId": o.id, "status": o.status, "venueOrderId": o.venue_order_id}


@executor("booking")
async def _booking(s: AsyncSession, a: Action, approver: str) -> dict[str, Any]:
    from .services import booking

    t = await s.get(Trade, a.payload["tradeId"])
    if t is None:
        raise GateError(f"no trade {a.payload['tradeId']}")
    t = await booking.book(s, t, by=approver)
    return {"tradeId": t.id, "contractId": t.sfp_contract_id, "compareStatus": t.compare_status}


@executor("amend")
async def _amend(s: AsyncSession, a: Action, approver: str) -> dict[str, Any]:
    from .services import booking

    t = await s.get(Trade, a.payload["tradeId"])
    if t is None:
        raise GateError(f"no trade {a.payload['tradeId']}")
    t = await booking.amend_to_fill(s, t, by=approver)
    return {"tradeId": t.id, "compareStatus": t.compare_status}


@executor("chat_send")
async def _chat_send(s: AsyncSession, a: Action, approver: str) -> dict[str, Any]:
    from .adapters import systems

    # Books-and-records: the sent text and its approver live on this action and in the event log.
    r = await systems().chat.send(a.payload["roomId"], a.payload["text"], client_msg_id=a.id)
    return {"messageId": r.get("id") if isinstance(r, dict) else None}


@executor("cancel")
async def _cancel(s: AsyncSession, a: Action, approver: str) -> dict[str, Any]:
    from .services import orders

    o = await s.get(Order, a.payload["orderId"])
    if o is None:
        raise GateError(f"no order {a.payload['orderId']}")
    o = await orders.venue_command(s, o, "cancel", by=approver)
    return {"orderId": o.id, "status": o.status}
