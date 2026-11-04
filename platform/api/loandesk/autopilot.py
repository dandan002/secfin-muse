"""Autopilot: the desk approving its own cards, inside limits the desk sets.

Three modes, switched at run time like the kill switch (``PUT /autopilot``) and starting from
``LOANDESK_AUTOPILOT_MODE``:

The limits below are the desk's to set (``PUT /policy/{key}``, see ``policy.py``); settings only start them.

* ``off``: every card waits for a trader (the default, and the desk's normal stance).
* ``autopilot``: the agent starts its own reviews of Warm locates, and a card approves itself when it is one of
  the allowed kinds (locate decisions, orders, baskets), carries no risk flag at all, is not on a Hot name, the
  agent was confident enough, and it fits the per-card and daily limits. Anything else waits for a trader.
* ``danger``: end to end. Every kind of card (break fixes, recalls and returns, chat bids, cancels, payments),
  Hot names, cards with warnings, Hot-name lines sent to the agent too, and a lender's counter accepted when it is
  within a tolerance of our bid. Wider limits, not none.

What no mode loosens: a card with a ``BLOCKED`` flag (a hard pre-trade check failed) never approves itself, nor
does a margin payment past the desk's collateral budget; the
executors still enforce counterparty limits, fee floors and the restricted list; the kill switch stops everything
(a card that can't execute stays pending); the model budget still caps spend. A run of failed auto-approvals trips
the breaker and drops the desk back to ``off``. Every auto-approval is recorded under its policy
(``policy:AUTOPILOT`` or ``policy:DANGER``) with the notional it counted against the day's limit, and every card
it passes over records why.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select

from . import clock, db, events, gate, policy, rules, secmaster
from .models import Action, Event, Locate, Order
from .services import margin
from .settings import get_settings

log = logging.getLogger("loandesk.autopilot")
Mode = Literal["off", "autopilot", "danger"]
ACTOR = {"autopilot": "policy:AUTOPILOT", "danger": "policy:DANGER"}
ENTITY = "autopilot"  # its events: mode changes, approvals (with the notional counted), cards passed over

_state: dict[str, Any] = {"mode": None, "by": None, "at": None, "tripped": None}
_failures = 0


@dataclass(frozen=True)
class Envelope:
    kinds: frozenset[str] | None  # None: every kind the gate executes
    max_card_usd: float
    day_usd: float
    day_count: int
    min_confidence: float
    allow_warnings: bool  # a card with "warning:" or agent flags may still go; BLOCKED never does
    allow_hot: bool


def envelope(mode: Mode) -> Envelope | None:
    """The mode's limits, as the desk set them (``policy``), else as the settings start them."""
    if mode not in ACTOR:
        return None
    lim = policy.limits(mode)
    return Envelope(None if lim.kinds is None else frozenset(lim.kinds), lim.maxCardUsd, lim.dayUsd, lim.dayCount,
                    lim.minConfidence, allow_warnings=mode == "danger", allow_hot=mode == "danger")


# -- mode ------------------------------------------------------------------------------------------
def mode() -> Mode:
    if _state["mode"] is None:
        _state["mode"] = get_settings().autopilot_mode
    return _state["mode"]


def state() -> dict[str, Any]:
    m = mode()
    env = envelope(m)
    return {"mode": m, "by": _state["by"], "at": _state["at"], "tripped": _state["tripped"],
            "limits": None if env is None else {
                "kinds": sorted(env.kinds) if env.kinds else "all", "maxCardUsd": env.max_card_usd,
                "dayUsd": env.day_usd, "dayCount": env.day_count, "minConfidence": env.min_confidence,
                "warnings": env.allow_warnings, "hot": env.allow_hot, "tripAfter": policy.trip_after(),
                **({"counterTolerancePct": policy.limits("danger").counterTolerancePct} if m == "danger" else {})}}


async def set_mode(m: Mode, by: str, reason: str | None = None) -> dict[str, Any]:
    global _failures
    _state.update(mode=m, by=by, at=clock.iso(clock.now()), tripped=reason if by == "policy:BREAKER" else None)
    _failures = 0
    async with db.session() as s:
        events.record(s, ENTITY, "mode", m, {"mode": m, "reason": reason}, actor=by)
        await s.commit()
    if m != "off" and worker is not None:
        worker.sweep()
    return state()


# -- the decision ------------------------------------------------------------------------------------
async def _instrument(s: Any, a: Action) -> str | None:
    p = a.payload or {}
    if a.kind == "locate_decision" and (loc := await s.get(Locate, p.get("locateId"))) is not None:
        return loc.instrument_id
    if isinstance(p.get("instrument"), str):
        res = await secmaster.resolve(s, p["instrument"])
        return res.instrument.id if res.instrument else None
    if isinstance(p.get("instrumentId"), str):
        return p["instrumentId"]
    return None


def _usd(iid: str | None, qty: Any) -> float:
    if not iid or not isinstance(qty, (int, float)):
        return 0.0
    inst = secmaster.instrument_by_id(iid)
    return float(qty) * secmaster.live(iid)["lastPx"] * rules.FX_USD.get(inst.ccy if inst else "USD", 1.0)


async def notional(s: Any, a: Action) -> float:
    """What a card puts at stake, in dollars: shares times price. Kinds without a size count zero (and only
    against the day's count)."""
    p = a.payload or {}
    if a.kind == "margin":  # collateral moved, not shares
        return float(p.get("amount") or 0) * rules.FX_USD.get(p.get("ccy") or "USD", 1.0)
    if a.kind == "basket":
        total = 0.0
        for ln in p.get("lines") or []:
            res = await secmaster.resolve(s, str(ln.get("instrument") or ""))
            total += _usd(res.instrument.id if res.instrument else None, ln.get("quantity"))
        return total
    return _usd(await _instrument(s, a), p.get("quantity"))


async def used_today(s: Any) -> tuple[int, float]:
    """Auto-approvals so far today (New York) and the notional they counted, from the desk's own records."""
    day = clock.local_date()
    rows = (await s.execute(select(Event.ts, Event.after).where(Event.entity == ENTITY, Event.type == "approved")))
    n, usd = 0, 0.0
    for ts, after in rows:
        if clock.local_date(ts) == day:
            n += 1
            usd += float((after or {}).get("notionalUsd") or 0)
    return n, usd


async def verdict(s: Any, a: Action, m: Mode) -> tuple[str | None, float]:
    """Why this card can't approve itself in mode ``m`` (None: it can), and its notional."""
    env = envelope(m)
    if env is None:
        return "autopilot is off", 0.0
    if a.status != "PENDING":
        return f"card is {a.status}", 0.0
    if a.proposed_by.startswith("trader:"):
        return "a trader's own card", 0.0
    if env.kinds is not None and a.kind not in env.kinds:
        return f"{a.kind} cards wait for a trader in {m}", 0.0
    flags = a.risk_flags or []
    if any(f.startswith("BLOCKED") for f in flags):
        return "a hard check failed (BLOCKED)", 0.0
    if margin.OVER in flags:  # the desk's collateral budget holds in every mode
        return "over today's collateral budget", 0.0
    if flags and not env.allow_warnings:
        return f"risk flag: {flags[0]}", 0.0
    conf = (a.payload or {}).get("confidence")
    if isinstance(conf, (int, float)) and conf < env.min_confidence:
        return f"agent confidence {conf:.2f} under {env.min_confidence:g}", 0.0
    iid = await _instrument(s, a)
    if iid and not env.allow_hot and secmaster.live(iid)["tier"] == "HOT":
        return "a Hot name", 0.0
    usd = await notional(s, a)
    if usd > env.max_card_usd:
        return f"${usd:,.0f} over the ${env.max_card_usd:,.0f} card limit", usd
    n, day = await used_today(s)
    if n >= env.day_count:
        return f"the day's {env.day_count} auto-approvals are used", usd
    if day + usd > env.day_usd:
        return f"${day + usd:,.0f} would pass the day's ${env.day_usd:,.0f}", usd
    return None, usd


async def consider(action_id: str) -> Action | None:
    """Approve one pending card if the current mode allows it. Returns the card if it was approved."""
    global _failures
    m = mode()
    if m == "off":
        return None
    async with db.session() as s:
        a = await s.get(Action, action_id)
        if a is None:
            return None
        why, usd = await verdict(s, a, m)
        if why is not None:
            if a.status == "PENDING":
                events.record(s, ENTITY, a.id, "passed_over", {"reason": why, "mode": m}, actor=ACTOR[m])
                await s.commit()
            return None
        try:
            await gate.approve(s, a, ACTOR[m])
        except gate.GateError as e:  # the kill switch: the card waits, and that's not a failure
            log.info("autopilot: %s not approved: %s", a.id, e)
            return None
        events.record(s, ENTITY, a.id, "approved", {"mode": m, "kind": a.kind, "notionalUsd": round(usd, 2),
                                                    "status": a.status}, actor=ACTOR[m])
        await s.commit()
    from . import agent

    if (r := agent.current()) is not None:  # the agent run that drafted it carries on
        await r.action_decided(a)
    _failures = _failures + 1 if a.status == "FAILED" else 0
    if _failures >= policy.trip_after():
        await set_mode("off", "policy:BREAKER", f"{_failures} auto-approved cards failed in a row "
                                                 f"(last: {(a.result or {}).get('error')})")
    return a


# -- danger: a lender's counter ------------------------------------------------------------------------
def counter_ok(o: Order) -> bool:
    """Take a lender's counter when it's within the tolerance of what we bid: a borrow pays at most that much
    more, a loan takes at most that much less."""
    if o.status != "COUNTERED" or o.counter_fee_bps is None:
        return False
    tol = policy.limits("danger").counterTolerancePct / 100
    if o.side == "BORROW":
        return o.counter_fee_bps <= o.fee_bps * (1 + tol)
    return o.counter_fee_bps >= o.fee_bps * (1 - tol)


async def take_counter(order_id: str) -> None:
    from .services import orders

    async with db.session() as s:
        o = await s.get(Order, order_id)
        if o is None or not counter_ok(o):
            return
        try:
            await orders.venue_command(s, o, "accept", by=ACTOR["danger"])
        except orders.OrderError as e:
            log.info("danger: counter on %s not taken: %s", order_id, e)
            return
        events.record(s, ENTITY, o.id, "counter_taken", {"feeBps": o.fee_bps, "counterFeeBps": o.counter_fee_bps},
                      actor=ACTOR["danger"])
        await s.commit()


# -- the worker: listens to the desk's own stream ------------------------------------------------------
class Worker:
    """Takes each new card, new locate and countered order as it's committed, one at a time."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._pump: asyncio.Task[None] | None = None
        self._reviews: deque[str] = deque()
        self._sweep = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="autopilot")
        self._pump = asyncio.create_task(self._start_reviews(), name="autopilot-reviews")
        if mode() != "off":
            self.sweep()

    async def stop(self) -> None:
        for t in (self._task, self._pump):
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    def sweep(self) -> None:
        """Look again at every pending card (the mode just went on)."""
        self._sweep.set()

    async def _run(self) -> None:
        q = events.hub.subscribe()
        try:
            while True:
                if self._sweep.is_set():
                    self._sweep.clear()
                    async with db.session() as s:
                        ids = list((await s.execute(select(Action.id).where(Action.status == "PENDING")
                                                    .order_by(Action.created_at))).scalars())
                    for i in ids:
                        await self._safe(consider(i))
                msg = await self._next(q)
                if msg is None:
                    continue
                await self._handle(msg)
        finally:
            events.hub.unsubscribe(q)

    async def _next(self, q: asyncio.Queue[dict[str, Any]]) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(q.get(), timeout=1.0)
        except TimeoutError:
            return None

    async def _handle(self, msg: dict[str, Any]) -> None:
        m, topic, d = mode(), msg.get("topic"), msg.get("data") or {}
        if m == "off" or not isinstance(d, dict):
            return
        body = d.get("data") or {}
        if topic == "action" and d.get("type") == "proposed":
            await self._safe(consider(d["id"]))
        elif topic == "locate" and d.get("type") == "received" and body.get("status") == "RECEIVED":
            if body.get("route") == "REVIEW" or (m == "danger" and body.get("route") == "HOLD"):
                self._reviews.append(d["id"])
        elif topic == "order" and m == "danger" and body.get("status") == "COUNTERED":
            await self._safe(take_counter(d["id"]))

    async def _start_reviews(self) -> None:
        """Hand queued locates to the agent, a few at a time, so a burst doesn't spend the budget at once."""
        from . import agent

        while True:
            await asyncio.sleep(0.5)
            r = agent.current()
            if r is None or mode() == "off" or get_settings().agent_mode == "off":
                self._reviews.clear()
                continue
            while self._reviews and len(r._tasks) < get_settings().autopilot_max_reviews:
                await self._safe(r.review_locate(self._reviews.popleft()))

    @staticmethod
    async def _safe(coro: Any) -> None:
        try:
            await coro
        except Exception:  # one bad card never stops the autopilot; it's logged and the card waits
            log.exception("autopilot step failed")


worker: Worker | None = None


def start() -> None:
    global worker
    worker = Worker()
    worker.start()


async def stop() -> None:
    if worker is not None:
        await worker.stop()
