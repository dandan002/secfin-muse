"""Locates, orders, trades and the blotter: the trader's manual path."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, events
from ..db import get_session
from ..models import Locate, Order, Trade
from ..services import booking, locates, orders
from .deps import trader

router = APIRouter()


# -- locates --------------------------------------------------------------------------------------
class LocateLineIn(BaseModel):
    identifier: str
    quantity: int = Field(..., gt=0)


class LocatesIn(BaseModel):
    counterparty: str
    lines: list[LocateLineIn] = Field(..., min_length=1, max_length=5000)
    channel: Literal["MANUAL", "PASTE", "FILE", "INTERNAL", "API"] = "MANUAL"
    requestRef: str | None = None


class DecisionIn(BaseModel):
    decision: Literal["APPROVED", "PARTIAL", "REJECTED"]
    quantity: int | None = Field(None, ge=0)
    feeBps: float | None = None
    rationale: str | None = None


class BatchDecisionIn(DecisionIn):
    ids: list[str] = Field(..., min_length=1, max_length=5000)


@router.post("/locates", tags=["locates"], status_code=201)
async def create_locates(body: LocatesIn, who: str = Depends(trader), s: AsyncSession = Depends(get_session)) -> list:
    ref = body.requestRef or events.new_id("req")
    locs = await locates.intake(s, counterparty=body.counterparty, channel=body.channel, request_ref=ref, actor=who,
                                lines=[locates.LineIn(x.identifier, x.quantity) for x in body.lines])
    await s.commit()
    return [locates.locate_json(x) for x in locs]


@router.get("/locates", tags=["locates"])
async def list_locates(status: str | None = None, route: str | None = None, limit: int = Query(500, le=5000),
                       s: AsyncSession = Depends(get_session)) -> list[dict]:
    return await locates.listing(s, status=status.upper().split(",") if status else None, route=route, limit=limit)


async def _decide(s: AsyncSession, loc_id: str, body: DecisionIn, who: str) -> Locate:
    loc = await s.get(Locate, loc_id)
    if loc is None:
        raise HTTPException(404, "no such locate")
    try:
        return await locates.decide(s, loc, body.decision, body.quantity, by=who, rationale=body.rationale,
                                    fee_bps=body.feeBps)
    except locates.DecisionError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/locates/{loc_id}/decision", tags=["locates"])
async def decide(loc_id: str, body: DecisionIn, who: str = Depends(trader),
                 s: AsyncSession = Depends(get_session)) -> dict:
    loc = await _decide(s, loc_id, body, who)
    await s.commit()
    return locates.locate_json(loc)


@router.post("/locates/decisions", tags=["locates"])
async def decide_batch(body: BatchDecisionIn, who: str = Depends(trader),
                       s: AsyncSession = Depends(get_session)) -> dict:
    """Batch approve (``Shift+A``). Lines that can't take the decision are reported, not fatal."""
    done, failed = [], []
    for loc_id in body.ids:
        loc = await s.get(Locate, loc_id)
        if loc is None or loc.status != "RECEIVED":
            failed.append({"id": loc_id, "error": "not open"})
            continue
        try:
            await locates.decide(s, loc, body.decision, body.quantity, by=who, rationale=body.rationale, reply=False)
            done.append(loc)
        except locates.DecisionError as e:
            failed.append({"id": loc_id, "error": str(e)})
    await locates.send_reply(s, done)
    await s.commit()
    return {"decided": [x.id for x in done], "failed": failed}


@router.post("/locates/{loc_id}/release", tags=["locates"])
async def release(loc_id: str, who: str = Depends(trader), s: AsyncSession = Depends(get_session)) -> dict:
    loc = await s.get(Locate, loc_id)
    if loc is None:
        raise HTTPException(404, "no such locate")
    try:
        await locates.release(s, loc, who)
    except locates.DecisionError as e:
        raise HTTPException(409, str(e)) from e
    await s.commit()
    return locates.locate_json(loc)


# -- orders ---------------------------------------------------------------------------------------
class CounterIn(BaseModel):
    feeBps: float = Field(..., ge=0)
    quantity: int | None = Field(None, gt=0)


def _order_error(e: orders.OrderError) -> HTTPException:
    return HTTPException(422, {"error": str(e), "checks": e.checks})


@router.post("/orders/preview", tags=["orders"])
async def preview(body: orders.OrderIn, s: AsyncSession = Depends(get_session)) -> dict:
    try:
        return await orders.preview(s, body)
    except orders.OrderError as e:
        raise _order_error(e) from e


@router.post("/orders", tags=["orders"], status_code=201)
async def create_order(body: orders.OrderIn, who: str = Depends(trader),
                       s: AsyncSession = Depends(get_session)) -> dict:
    try:
        o = await orders.create(s, body, by=who)
    except orders.OrderError as e:
        raise _order_error(e) from e
    await s.commit()
    return orders.order_json(o)


@router.get("/orders", tags=["orders"])
async def list_orders(status: str | None = None, s: AsyncSession = Depends(get_session)) -> list[dict]:
    return await orders.listing(s, status=status.upper().split(",") if status else None)


@router.post("/orders/{order_id}/{cmd}", tags=["orders"])
async def order_command(order_id: str, cmd: Literal["accept", "cancel", "counter"], body: CounterIn | None = None,
                        who: str = Depends(trader), s: AsyncSession = Depends(get_session)) -> dict:
    o = await s.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "no such order")
    try:
        await orders.venue_command(s, o, cmd, by=who, fee_bps=body.feeBps if body else None,
                                   quantity=body.quantity if body else None)
    except orders.OrderError as e:
        raise _order_error(e) from e
    await s.commit()
    return orders.order_json(o)


# -- trades and booking ------------------------------------------------------------------------------
@router.get("/trades", tags=["booking"])
async def list_trades(s: AsyncSession = Depends(get_session)) -> list[dict]:
    return await booking.listing(s)


@router.post("/trades/{trade_id}/{cmd}", tags=["booking"])
async def trade_command(trade_id: str, cmd: Literal["book", "compare", "amend"], who: str = Depends(trader),
                        s: AsyncSession = Depends(get_session)) -> dict:
    t = await s.get(Trade, trade_id)
    if t is None:
        raise HTTPException(404, "no such trade")
    if cmd == "book":
        await booking.book(s, t, by=who)
    elif cmd == "compare":
        await booking.compare(s, t, by=who)
    else:
        await booking.amend_to_fill(s, t, by=who)
    await s.commit()
    return booking.trade_json(t)


@router.get("/blotter", tags=["orders"])
async def blotter(s: AsyncSession = Depends(get_session)) -> dict:
    o = await orders.listing(s)
    t = await booking.listing(s)
    open_loc = (await s.execute(select(Locate.id).where(Locate.status == "RECEIVED"))).scalars().all()
    return {"asOf": clock.iso(clock.now()), "orders": o, "trades": t,
            "counts": {"openOrders": sum(1 for x in o if x["status"] in orders.OPEN), "fills": len(t),
                       "breaks": sum(1 for x in t if x["compareStatus"] == "UNMATCHED"),
                       "unbooked": sum(1 for x in t if x["compareStatus"] in ("UNBOOKED", "FAILED")),
                       "openLocates": len(open_loc)}}
