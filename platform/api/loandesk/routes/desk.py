"""Approval queue, paste ingestion, agent controls, audit trail and the UI stream."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, events, gate, sync
from ..adapters import AdapterError, systems
from ..agent import loop
from ..agent.models import gateway
from ..agent.tools import REGISTRY
from ..db import get_session
from ..ingest import paste
from ..models import Action, Event, PasteSession
from ..services import locates
from ..settings import get_settings
from .deps import trader

router = APIRouter()


# -- approval queue -------------------------------------------------------------------------------
class ApproveIn(BaseModel):
    payload: dict[str, Any] | None = Field(None, description="Edited payload (Edit then approve)")


class RejectIn(BaseModel):
    reason: str | None = None


@router.get("/actions", tags=["gate"])
async def list_actions(status: str | None = "PENDING", s: AsyncSession = Depends(get_session)) -> list[dict]:
    return await gate.listing(s, None if status in (None, "", "ALL") else status)


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
    return gate.action_json(a)


class KillIn(BaseModel):
    on: bool


@router.put("/kill-switch", tags=["gate"])
async def kill_switch(body: KillIn, who: str = Depends(trader)) -> dict:
    return gate.set_kill_switch(body.on, who)


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
            "killSwitch": gate.kill_switch()}


@router.post("/agent/locates/{loc_id}/review", tags=["agent"])
async def agent_review(loc_id: str, s: AsyncSession = Depends(get_session)) -> dict:
    if gateway().mode == "off":
        raise HTTPException(503, "agent is off: set LOANDESK_AGENT_MODE=replay|record|live")
    run = await loop.review_locate(s, loc_id)
    await s.commit()
    return run.to_json()


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


@router.get("/status", tags=["audit"])
async def status() -> dict:
    cfg = get_settings()
    return {"clock": clock.to_json(), "systems": await systems().status(), "sync": sync.status,
            "agent": gateway().status(), "killSwitch": gate.kill_switch(),
            "policy": {"autoApproveEtb": cfg.auto_approve_etb, "locateNameCap": cfg.locate_name_cap,
                       "bands": {"gcMaxFeeBps": cfg.gc_max_fee_bps, "gcMaxUtilPct": cfg.gc_max_util_pct,
                                 "hotMinFeeBps": cfg.hot_min_fee_bps, "hotMinUtilPct": cfg.hot_min_util_pct}}}


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    """Multiplexed pushes: locate, order, trade, break, action, market, clock."""
    await ws.accept()
    q = events.hub.subscribe()

    async def ticker() -> None:
        while True:
            await ws.send_json({"topic": "clock", "data": clock.to_json()})
            await asyncio.sleep(1)

    tick = asyncio.create_task(ticker())
    try:
        while True:
            await ws.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await tick
        events.hub.unsubscribe(q)
