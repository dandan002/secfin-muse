"""mock-ngt: "Venue A (NGT-style)", an electronic securities-lending venue simulator.

REST + WebSocket for availability, prints, orders and trades, and a FIX 4.4-style TCP
session (see ../API.md). Both front ends share ``engine.py``. API shapes are our own.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from fastapi import Depends, HTTPException, Query, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from mockcore import clock
from mockcore.app import ServiceContext, ServiceSpec, create_app
from mockcore.keys import AuthSpec, Grant, Principal
from refdata.counterparties import OUR_FIRM, counterparties, counterparty_by_code
from refdata.universe import resolve

from . import engine, fix
from .models import Base, Inventory, Order, Print, Trade

FIX_HOST = os.environ.get("MOCK_NGT_FIX_HOST", "0.0.0.0")
FIX_PORT = int(os.environ.get("MOCK_NGT_FIX_PORT", "9878"))

SCOPES = {
    "reference:read": "Participant code table",
    "market:read": "Availability feed and last prints (REST and stream)",
    "orders:read": "Your orders, and order events on the stream",
    "orders:write": "Place, counter, accept and cancel orders",
    "trades:read": "Executed trades, and trade events on the stream",
}
spec = ServiceSpec(
    name="mock-ngt",
    label="Venue A (NGT-style)",
    schema="ngt",
    base=Base,
    description=(
        "Simulated electronic sec-lending venue: lender availability feed, last prints, BID/OFFER/COUNTER/"
        "ACCEPT/CANCEL/FIRM_BID orders, execution ledger with UTIs, and a FIX 4.4 session (default port 9878)."
    ),
    auth=AuthSpec(SCOPES, lambda: [
        Grant(OUR_FIRM["ngt_code"], tuple(SCOPES), "partner", f"{OUR_FIRM['name']} (participant)"),
        Grant("MARKETDATA", ("reference:read", "market:read"), "standard", "Market data only"),
    ]),
)
ctx = ServiceContext(spec)
ctx.fix_sessions = {}  # type: ignore[attr-defined]
app = create_app(ctx)
_server: dict[str, Any] = {}


def participant(scope: str):  # noqa: ANN201 - a FastAPI dependency
    """The calling participant's venue code (a key's principal), entitled to ``scope``."""
    gate = ctx.require(scope)

    async def dep(p: Principal = Depends(gate)) -> str:
        return p.principal

    return dep


@app.exception_handler(engine.VenueError)
async def venue_error(_, exc: engine.VenueError) -> JSONResponse:
    status = 404 if exc.code in ("UNKNOWN_ORDER",) else 409 if exc.code in ("BAD_STATE", "DUPLICATE_CLORDID") else 422
    return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=status)


# -- lifecycle -----------------------------------------------------------------------------------
async def on_reset(c: ServiceContext) -> None:
    await engine.seed_inventory(c)


async def on_startup(c: ServiceContext) -> None:
    _server["fix"] = await fix.serve(c, FIX_HOST, FIX_PORT)
    _server["router"] = asyncio.create_task(fix.router(c))


async def on_shutdown(c: ServiceContext) -> None:
    if "router" in _server:
        _server["router"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _server["router"]
    if "fix" in _server:
        _server["fix"].close()
        for sess in list(c.fix_sessions.values()):  # type: ignore[attr-defined]
            sess.closed = True
            with contextlib.suppress(Exception):
                sess.writer.close()


spec.on_reset = on_reset
spec.on_startup = on_startup
spec.on_shutdown = on_shutdown
ctx.scheduler.every(engine.config.feed_every_s, "feed", lambda: engine.feed_tick(ctx))
ctx.scheduler.every(engine.config.prints_every_s, "prints", lambda: engine.prints_tick(ctx))


# -- reference ------------------------------------------------------------------------------------
@app.get("/v1/participants", tags=["reference"], dependencies=[Depends(ctx.require("reference:read"))])
async def participants() -> list[dict]:
    """Venue participant code table."""
    rows = [{"code": OUR_FIRM["ngt_code"], "name": OUR_FIRM["name"], "lei": OUR_FIRM["lei"], "role": "PARTICIPANT",
             "self": True}]
    for c in counterparties():
        rows.append({"code": c.ngt_code, "name": c.name, "lei": c.lei,
                     "role": "LENDER" if c.kind == "AGENT_LENDER" else "BORROWER", "agreement": c.agreement})
    return rows


@app.get("/v1/availability", tags=["market"], dependencies=[Depends(ctx.require("market:read"))])
async def get_availability(instrument: str | None = None, lender: str | None = None,
                           minQuantity: int = 0, limit: int = Query(500, le=5000), offset: int = 0) -> dict:
    iid = None
    if instrument:
        inst = resolve(instrument)
        if inst is None:
            raise HTTPException(404, "unknown instrument")
        iid = inst.id
    async with ctx.session() as s:
        rows = await engine.availability(s, iid)
    if lender:
        cp = counterparty_by_code(lender)
        rows = [r for r in rows if cp and r["lenderCode"] == cp.ngt_code]
    rows = [r for r in rows if r["quantity"] >= minQuantity]
    agg = list(engine.aggregate(rows).values())
    return {"asOf": clock.iso(clock.now()), "total": len(agg), "instruments": agg[offset : offset + limit],
            "byLender": rows if instrument else None}


@app.get("/v1/prints", tags=["market"], dependencies=[Depends(ctx.require("market:read"))])
async def prints(instrument: str | None = None, limit: int = Query(50, le=1000)) -> list[dict]:
    async with ctx.session() as s:
        q = select(Print).order_by(Print.ts.desc(), Print.id.desc()).limit(limit)
        if instrument:
            inst = resolve(instrument)
            if inst is None:
                raise HTTPException(404, "unknown instrument")
            q = q.where(Print.instrument_id == inst.id)
        return [{"instrumentId": p.instrument_id, "quantity": p.qty, "feeBps": p.fee_bps, "ts": clock.iso(p.ts)}
                for p in (await s.execute(q)).scalars()]


@app.websocket("/v1/stream")
async def stream(ws: WebSocket, topics: str = "availability,prints,orders,trades") -> None:
    """Pass the key as ``?key=``; each topic needs its scope (orders and trades are private)."""
    wanted = [t.strip() for t in topics.split(",") if t.strip()]
    need = {"availability": "market:read", "prints": "market:read", "orders": "orders:read", "trades": "trades:read"}
    if await ctx.gate.websocket(ws, *{need[t] for t in wanted if t in need}) is None:
        return

    async def snapshot() -> list[dict]:
        if "availability" not in wanted:
            return []
        async with ctx.session() as s:
            agg = engine.aggregate(await engine.availability(s))
        return [{"type": "availability_snapshot", "ts": clock.iso(clock.now()), "data": list(agg.values())}]

    await ctx.ws_stream(ws, wanted, snapshot=snapshot)


# -- orders ---------------------------------------------------------------------------------------
class CounterBody(BaseModel):
    feeBps: float = Field(..., ge=0)
    quantity: int | None = Field(None, gt=0)


@app.post("/v1/orders", tags=["orders"], status_code=201)
async def place_order(req: engine.OrderRequest, who: str = Depends(participant("orders:write"))) -> dict:
    async with ctx.session() as s:
        o = await engine.place(ctx, s, req, who, "REST")
        await s.commit()
        return engine.order_json(o)


@app.get("/v1/orders", tags=["orders"])
async def list_orders(status: str | None = None, limit: int = Query(200, le=5000),
                      who: str = Depends(participant("orders:read"))) -> list[dict]:
    async with ctx.session() as s:
        q = select(Order).where(Order.participant == who).order_by(Order.id.desc()).limit(limit)
        if status:
            q = q.where(Order.status == status.upper())
        return [engine.order_json(o) for o in (await s.execute(q)).scalars()]


@app.get("/v1/orders/{order_id}", tags=["orders"])
async def get_order(order_id: str, who: str = Depends(participant("orders:read"))) -> dict:
    async with ctx.session() as s:
        return engine.order_json(await engine.get_order(s, order_id))


@app.post("/v1/orders/{order_id}/counter", tags=["orders"])
async def counter_order(order_id: str, body: CounterBody, who: str = Depends(participant("orders:write"))) -> dict:
    async with ctx.session() as s:
        o = await engine.counter(ctx, s, await engine.get_order(s, order_id, lock=True), body.feeBps, body.quantity)
        await s.commit()
        return engine.order_json(o)


@app.post("/v1/orders/{order_id}/accept", tags=["orders"])
async def accept_order(order_id: str, who: str = Depends(participant("orders:write"))) -> dict:
    async with ctx.session() as s:
        t = await engine.accept(ctx, s, await engine.get_order(s, order_id, lock=True))
        await s.commit()
        return engine.trade_json(t)


@app.post("/v1/orders/{order_id}/cancel", tags=["orders"])
async def cancel_order(order_id: str, who: str = Depends(participant("orders:write"))) -> dict:
    async with ctx.session() as s:
        o = await engine.cancel(ctx, s, await engine.get_order(s, order_id, lock=True))
        await s.commit()
        return engine.order_json(o)


@app.get("/v1/trades", tags=["trades"])
async def trades(since: str | None = None, limit: int = Query(500, le=5000),
                 who: str = Depends(participant("trades:read"))) -> list[dict]:
    async with ctx.session() as s:
        q = select(Trade).order_by(Trade.id).limit(limit)
        if since:
            q = q.where(Trade.executed_at >= clock.parse_ts(since))
        return [engine.trade_json(t) for t in (await s.execute(q)).scalars()]


@app.get("/v1/trades/{trade_id}", tags=["trades"])
async def trade(trade_id: str, who: str = Depends(participant("trades:read"))) -> dict:
    async with ctx.session() as s:
        t = (await s.execute(select(Trade).where((Trade.trade_id == trade_id) | (Trade.uti == trade_id)))).scalar()
        if t is None:
            raise HTTPException(404, "no such trade")
        return engine.trade_json(t)


# -- admin ------------------------------------------------------------------------------------------
@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {
        "participantCode": OUR_FIRM["ngt_code"],
        "restHeader": "X-Participant-Key",
        "participantKey": ctx.gate.bootstrap_key(OUR_FIRM["ngt_code"]),
        "marketDataKey": ctx.gate.bootstrap_key("MARKETDATA"),
        "fix": {"host": FIX_HOST, "port": FIX_PORT, "senderCompID": "LOANDESK", "targetCompID": fix.VENUE_COMP_ID,
                "password": fix.participant_password(ctx, "LOANDESK")},
    }


@app.get("/_admin/config", tags=["admin"])
async def get_config() -> dict:
    return engine.config.model_dump()


@app.put("/_admin/config", tags=["admin"])
async def put_config(cfg: engine.VenueConfig) -> dict:
    engine.config = cfg
    return cfg.model_dump()


@ctx.verb("supply")
async def verb_supply(args: dict[str, Any]) -> dict:
    """Set a lender's supply: {instrument, lender (code), quantity}. quantity 0 drains it."""
    inst = resolve(args["instrument"])
    cp = counterparty_by_code(args["lender"]) if args.get("lender") else None
    if inst is None:
        raise HTTPException(404, "unknown instrument")
    async with ctx.session() as s:
        q = select(Inventory).where(Inventory.instrument_id == inst.id)
        if cp:
            q = q.where(Inventory.lender == cp.code)
        rows = (await s.execute(q)).scalars().all()
        if not rows and cp:
            rows = [Inventory(lender=cp.code, instrument_id=inst.id, qty=0, fee_mult=1.0, updated_at=clock.now())]
            s.add(rows[0])
        for inv in rows:
            inv.qty = int(args.get("quantity", 0))
            inv.updated_at = clock.now()
        await s.commit()
        await engine.publish_availability(ctx, s, [inst.id])
    return {"instrumentId": inst.id, "rows": len(rows)}


@ctx.verb("config")
async def verb_config(args: dict[str, Any]) -> dict:
    """Patch venue config, e.g. {latency_special_s: [1, 2]}"""
    engine.config = engine.VenueConfig(**{**engine.config.model_dump(), **args})
    return engine.config.model_dump()
