"""Approval queue, paste ingestion, agent controls, audit trail and the UI stream."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import agent, auth, autopilot, clock, events, gate, observe, policy, sync
from ..adapters import AdapterError, systems
from ..agent.models import gateway
from ..agent.tools import REGISTRY
from ..db import get_session
from ..ingest import paste
from ..models import Action, AgentRun, Counterparty, Event, PasteSession
from ..services import locates
from ..settings import get_settings
from .deps import head, require_head, trader

if TYPE_CHECKING:
    from ..agent.runner import Runner

router = APIRouter()
STREAM_GRACE_S = 30  # a socket outlives its token by this much while the browser renews it


# -- approval queue -------------------------------------------------------------------------------
class ApproveIn(BaseModel):
    payload: dict[str, Any] | None = Field(None, description="Edited payload (Edit then approve)")


class RejectIn(BaseModel):
    reason: str | None = None


@router.get("/actions", tags=["gate"])
async def list_actions(status: str | None = "PENDING", limit: int = 200,
                       s: AsyncSession = Depends(get_session)) -> list[dict]:
    """Newest first; ``limit`` up to 2,000 (the whole queue on a busy day)."""
    return await gate.listing(s, None if status in (None, "", "ALL") else status, limit=max(1, min(limit, 2000)))


async def _action(s: AsyncSession, action_id: str) -> Action:
    a = await s.get(Action, action_id)
    if a is None:
        raise HTTPException(404, "no such action")
    return a


@router.post("/actions/{action_id}/approve", tags=["gate"])
async def approve(action_id: str, body: ApproveIn | None = None, who: str = Depends(trader),
                  s: AsyncSession = Depends(get_session)) -> dict:
    try:
        a = await gate.approve(s, await _action(s, action_id), who, body.payload if body else None)
    except gate.GateError as e:
        raise HTTPException(409, str(e)) from e
    await s.commit()
    await _resume_agent(a)
    return gate.action_json(a)


@router.post("/actions/{action_id}/{verdict}", tags=["gate"])
async def reject(action_id: str, verdict: Literal["reject", "take-over"], body: RejectIn | None = None,
                 who: str = Depends(trader), s: AsyncSession = Depends(get_session)) -> dict:
    try:
        a = await gate.reject(s, await _action(s, action_id), who, body.reason if body else None,
                              status="REJECTED" if verdict == "reject" else "TAKEN_OVER")
    except gate.GateError as e:
        raise HTTPException(409, str(e)) from e
    await s.commit()
    await _resume_agent(a)
    return gate.action_json(a)


async def _resume_agent(a: Action) -> None:
    """A decided card may be what an agent run is waiting on."""
    if (r := agent.current()) is not None:
        await r.action_decided(a)


class KillIn(BaseModel):
    on: bool


@router.put("/kill-switch", tags=["gate"])
async def kill_switch(body: KillIn, request: Request, who: str = Depends(trader),
                      s: AsyncSession = Depends(get_session)) -> dict:
    """Anyone throws it; only a head of desk releases it."""
    if not body.on:
        require_head(request, "release the kill switch")
    out = await gate.save_kill_switch(s, body.on, who)
    await s.commit()
    return out


class AutopilotIn(BaseModel):
    mode: Literal["off", "autopilot", "danger"]
    confirm: str | None = Field(None, description='Danger mode only: "DANGER", to say it was meant')


@router.get("/me", tags=["gate"])
async def me(request: Request, who: str = Depends(trader)) -> dict:
    """Who the desk thinks you are, and whether you're a head of desk (the UI greys out what you can't do)."""
    return {"trader": who, "head": auth.is_head(getattr(request.state, "claims", None)), "signIn": auth.enabled()}


@router.get("/autopilot", tags=["gate"])
async def autopilot_state(s: AsyncSession = Depends(get_session)) -> dict:
    n, usd = await autopilot.used_today(s)
    return {**autopilot.state(), "today": {"count": n, "notionalUsd": round(usd, 2)}}


@router.put("/autopilot", tags=["gate"])
async def set_autopilot(body: AutopilotIn, request: Request, who: str = Depends(trader)) -> dict:
    """Off, autopilot (limited kinds and sizes) or danger (end to end). Danger asks for ``confirm: "DANGER"``.
    Anyone turns it off; only a head of desk turns it on."""
    if body.mode != "off":
        require_head(request, f"turn {body.mode} mode on")
    if body.mode == "danger" and body.confirm != "DANGER":
        raise HTTPException(422, 'danger mode lets the agent act end to end: send confirm "DANGER" to turn it on')
    return await autopilot.set_mode(body.mode, who)


# -- desk policy: autopilot limits and counterparty priority ---------------------------------------
class BreakerIn(BaseModel):
    tripAfter: int = Field(..., ge=1, le=100)


@router.get("/policy", tags=["gate"])
async def get_policy() -> dict:
    """What the desk set (``value``), what settings start it at (``default``), and who set it last."""
    return {**policy.snapshot(), "kinds": sorted(gate._executors)}


@router.put("/policy/{key}", tags=["gate"])
async def put_policy(key: Literal["autopilot", "danger", "breaker", "priority"], body: dict[str, Any],
                     who: str = Depends(head), s: AsyncSession = Depends(get_session)) -> dict:
    """Set one policy. ``autopilot`` / ``danger``: the mode's limits; ``breaker``: ``{tripAfter}``; ``priority``:
    ``{returns, recalls}`` (counterparty codes, first first)."""
    try:
        if key in ("autopilot", "danger"):
            v = policy.Limits(**body)
            if v.kinds is not None and (bad := sorted(set(v.kinds) - set(gate._executors))):
                raise HTTPException(422, f"no such card kind: {', '.join(bad)}")
            value = v.model_dump()
        elif key == "breaker":
            value = BreakerIn(**body).model_dump()
        else:
            v = policy.Priority(**body)
            codes = set((await s.execute(select(Counterparty.code))).scalars())
            for name in policy.LISTS:
                order = getattr(v, name)
                if bad := sorted(set(order) - codes):
                    raise HTTPException(422, f"{name}: no counterparty {', '.join(bad)}")
                if len(set(order)) != len(order):
                    raise HTTPException(422, f"{name}: a counterparty is listed twice")
            value = v.model_dump()
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    out = await policy.put(s, key, value, who)
    await s.commit()
    if key in ("autopilot", "danger") and autopilot.worker is not None and autopilot.mode() == key:
        autopilot.worker.sweep()  # cards it passed over may fit the new limits
    return out


@router.delete("/policy/{key}", tags=["gate"])
async def reset_policy(key: Literal["autopilot", "danger", "breaker", "priority"], who: str = Depends(head),
                       s: AsyncSession = Depends(get_session)) -> dict:
    """Back to where the settings start it."""
    out = await policy.put(s, key, None, who)
    await s.commit()
    return out


# -- paste ingestion ---------------------------------------------------------------------------------
class PasteIn(BaseModel):
    text: str = Field(..., max_length=2_000_000)


class ApplyIn(BaseModel):
    target: Literal["locates", "interest_list", "watchlist"]
    counterparty: str | None = None
    rows: list[int] | None = Field(None, description="Row numbers to apply; default every resolved row")
    listName: str | None = None


@router.post("/ingest/paste", tags=["ingest"])
async def ingest_paste(body: PasteIn, s: AsyncSession = Depends(get_session)) -> dict:
    p = paste.parse(body.text)
    ps = PasteSession(id=events.new_id("pst"), kind=p.kind, raw=body.text, rows=[r.to_json() for r in p.rows],
                      columns=p.columns, created_at=clock.now())
    s.add(ps)
    await s.commit()
    return {"id": ps.id, "kind": p.kind, "columns": p.columns, "rows": ps.rows, "unparsed": p.unparsed,
            "resolved": sum(1 for r in p.rows if r.instrument), "total": len(p.rows)}


@router.post("/ingest/paste/{paste_id}/apply", tags=["ingest"])
async def apply_paste(paste_id: str, body: ApplyIn, who: str = Depends(trader),
                      s: AsyncSession = Depends(get_session)) -> dict:
    """Nothing is created until the trader confirms here."""
    ps = await s.get(PasteSession, paste_id)
    if ps is None:
        raise HTTPException(404, "no such paste")
    wanted = set(body.rows) if body.rows else None
    rows = [r for r in ps.rows if r["instrument"] and (wanted is None or r["rowNo"] in wanted)]
    if not rows:
        raise HTTPException(422, "no resolved rows to apply")
    ps.applied_as = body.target
    if body.target == "watchlist":
        await s.commit()
        return {"target": "watchlist", "instrumentIds": [r["instrument"]["id"] for r in rows]}
    if body.target == "locates":
        cps = {(r.get("counterparty") or body.counterparty or "").upper() for r in rows}
        if "" in cps:
            raise HTTPException(422, "pick a counterparty for rows that don't name one")
        out = []
        for cp in sorted(cps):
            lines = [locates.LineIn(r["instrument"]["isin"], r["quantity"] or 0) for r in rows
                     if (r.get("counterparty") or body.counterparty or "").upper() == cp and r["quantity"]]
            if lines:
                out += await locates.intake(s, counterparty=cp, lines=lines, channel="PASTE",
                                            request_ref=f"{paste_id}:{cp}", source_ref=paste_id, actor=who)
        await s.commit()
        return {"target": "locates", "created": [locates.locate_json(x) for x in out]}
    from .. import secmaster

    lines = []
    for r in rows:
        if not r["quantity"]:
            continue
        fee = r["feeBps"] if r["feeBps"] is not None else round(secmaster.live(r["instrument"]["id"])["feeBps"] * 1.1)
        lines.append({"instrument": r["instrument"]["isin"], "side": "BORROW", "quantity": r["quantity"],
                      "feeBps": fee, "collateralCcy": r["instrument"]["ccy"]})
    try:
        lst = await systems().venue_b.upload_list(body.listName or f"paste {paste_id}", lines, client_ref=paste_id)
        opt = await systems().venue_b.run_optimiser(lst["listId"])
    except AdapterError as e:
        raise HTTPException(502, str(e)) from e
    events.record(s, "interest_list", lst["listId"], "uploaded", {"lines": len(lines), "optimiser": opt}, actor=who)
    await s.commit()
    return {"target": "interest_list", "list": lst, "optimiser": opt}


# -- agent ------------------------------------------------------------------------------------------
@router.get("/agent/status", tags=["agent"])
async def agent_status() -> dict:
    return {**gateway().status(), "tools": sorted(REGISTRY), "actionKinds": gate.kinds(),
            "killSwitch": gate.kill_switch(), "graphs": agent.missing or "ready", "tracing": observe.status(),
            "lanes": r.status() if (r := agent.current()) else None}


def _runner() -> Runner:
    if (r := agent.current()) is None:
        raise HTTPException(503, agent.missing or "agent runner not started")
    return r


@router.post("/agent/locates/{loc_id}/review", status_code=202, tags=["agent"])
async def agent_review(loc_id: str) -> dict:
    """Start a warm-locate review in the background. Its card reaches the Agent queue when it has one."""
    if gateway().mode == "off":
        raise HTTPException(503, "agent is off: set LOANDESK_AGENT_MODE=replay|record|live")
    from ..agent.runner import run_json

    return run_json(await _runner().review_locate(loc_id))


@router.post("/agent/plan", status_code=202, tags=["agent"])
async def agent_plan() -> dict:
    """Have the queue planner read the whole queue now: it starts the sub-agents, then ranks and batches."""
    from ..agent.runner import run_json

    r = _runner()
    if r.busy("plan_queue"):
        raise HTTPException(409, "the planner is already reading the queue")
    return run_json(await r.plan_queue())


@router.get("/agent/plan", tags=["agent"])
async def agent_plan_latest(s: AsyncSession = Depends(get_session)) -> dict | None:
    """The latest plan: order, batches, notes, conflicts, what it dispatched."""
    from ..agent import queue

    return await queue.latest(s)


@router.post("/actions/{action_id}/review", status_code=202, tags=["agent"])
async def agent_review_trade(action_id: str, s: AsyncSession = Depends(get_session)) -> dict:
    """Ask the trade reviewer to read (or read again) one card."""
    from ..agent.runner import run_json

    if gateway().mode == "off":
        raise HTTPException(503, "agent is off: set LOANDESK_AGENT_MODE=replay|record|live")
    a = await _action(s, action_id)
    if a.status != "PENDING":
        raise HTTPException(409, f"the card is {a.status}")
    a.review = None
    await s.commit()
    return run_json(await _runner().review_trade(action_id))


@router.get("/agent/runs", tags=["agent"])
async def agent_runs(status: str | None = None, subject: str | None = None, limit: int = 50,
                     s: AsyncSession = Depends(get_session)) -> list[dict]:
    from ..agent.runner import run_json

    q = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(min(limit, 500))
    if status:
        q = q.where(AgentRun.status == status.upper())
    if subject:
        q = q.where(AgentRun.subject == subject)
    return [run_json(r) for r in (await s.execute(q)).scalars()]


@router.get("/agent/runs/{run_id}", tags=["agent"])
async def agent_run(run_id: str, s: AsyncSession = Depends(get_session)) -> dict:
    """One run with its graph state: the decision, the checks, the escalation and the full transcript."""
    from ..agent.runner import run_json

    r = await s.get(AgentRun, run_id)
    if r is None:
        raise HTTPException(404, "no such run")
    return run_json(r, await _runner().state(run_id))


# -- audit and status -------------------------------------------------------------------------------
@router.get("/events", tags=["audit"])
async def list_events(entity: str | None = None, id: str | None = None, limit: int = 200,
                      s: AsyncSession = Depends(get_session)) -> list[dict]:
    q = select(Event).order_by(Event.id.desc()).limit(min(limit, 2000))
    if entity:
        q = q.where(Event.entity == entity)
    if id:
        q = q.where(Event.entity_id == id)
    return [{"id": e.id, "ts": clock.iso(e.ts), "entity": e.entity, "entityId": e.entity_id, "type": e.type,
             "actor": e.actor, "before": e.before, "after": e.after} for e in (await s.execute(q)).scalars()]


@router.get("/events/story", tags=["audit"])
async def event_story(id: str, s: AsyncSession = Depends(get_session)) -> dict:
    """One trade's (or order's, card's, locate's, chat message's) story: its events and those of everything it
    came from or led to, in time order."""
    from ..services import story

    return await story.story(s, id)


@router.get("/status", tags=["audit"])
async def status() -> dict:
    cfg = get_settings()
    return {"clock": clock.to_json(), "systems": await systems().status(), "sync": sync.status,
            "agent": gateway().status(), "killSwitch": gate.kill_switch(), "autopilot": autopilot.state(),
            "policy": {"autoApproveEtb": cfg.auto_approve_etb, "locateNameCap": cfg.locate_name_cap,
                       "bands": {"gcMaxFeeBps": cfg.gc_max_fee_bps, "gcMaxUtilPct": cfg.gc_max_util_pct,
                                 "hotMinFeeBps": cfg.hot_min_fee_bps, "hotMinUtilPct": cfg.hot_min_util_pct}}}


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    """Multiplexed pushes: locate, order, trade, break, action, market, clock. With sign-in on, ``?token=``."""
    expires: list[float] = [float("inf")]  # when this socket's sign-in runs out (with sign-in on)
    if auth.enabled():
        try:
            expires[0] = float((await auth.verify(ws.query_params.get("token") or ""))["exp"])
        except auth.AuthError:
            await ws.close(code=4401)
            return
    await ws.accept()
    q = events.hub.subscribe()

    async def ticker() -> None:
        while True:
            if time.time() > expires[0] + STREAM_GRACE_S:  # signed out, or stopped renewing: no more pushes
                await ws.close(code=4401)
                return
            await ws.send_json({"topic": "clock", "data": clock.to_json()})
            await asyncio.sleep(1)

    async def renewals() -> None:
        """The browser sends ``{"type": "auth", "token": …}`` as Clerk refreshes the session; a sign-out stops it."""
        while True:
            msg = await ws.receive_json()
            if auth.enabled() and isinstance(msg, dict) and msg.get("type") == "auth":
                try:
                    expires[0] = float((await auth.verify(str(msg.get("token") or "")))["exp"])
                except auth.AuthError:
                    await ws.close(code=4401)
                    return

    tick = asyncio.create_task(ticker())
    renew = asyncio.create_task(renewals())
    try:
        while not tick.done() and not renew.done():  # either ends the socket: closed, signed out, gone
            with contextlib.suppress(TimeoutError):
                await ws.send_json(await asyncio.wait_for(q.get(), 1.0))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        tick.cancel()
        renew.cancel()
        for t in (tick, renew):
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError, Exception):
                await t
        events.hub.unsubscribe(q)
