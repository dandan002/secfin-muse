"""mock-wematch: "Venue B (Wematch-style)", a list-based matching venue simulator.

Upload interest lists (JSON or CSV, up to 10k lines), run the optimiser against simulated
dealer lists, negotiate each proposed match (propose / counter / accept / reject), then manage
the resulting loans (re-rate, return/recall). API shapes are our own.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from mockcore import clock, rng
from mockcore.app import ServiceContext, ServiceSpec, create_app
from mockcore.auth import derive_secret
from refdata.counterparties import OUR_FIRM, counterparties

from . import engine
from .models import Base, InterestList, ListLine, Loan, Match

spec = ServiceSpec(
    name="mock-wematch",
    label="Venue B (Wematch-style)",
    schema="wematch",
    base=Base,
    description="Simulated list-matching venue: interest lists, optimiser matching, negotiation and loan lifecycle.",
)
ctx = ServiceContext(spec)
app = create_app(ctx)


def api_key() -> str:
    return derive_secret(rng.get_seed(), "wematch-api", OUR_FIRM["wematch_entity"])[:32]


async def member(x_api_key: str | None = Header(default=None)) -> str:
    if x_api_key != api_key():
        raise HTTPException(401, "missing or bad X-Api-Key")
    return OUR_FIRM["wematch_entity"]


@app.exception_handler(engine.EngineError)
async def engine_error(_, exc: engine.EngineError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status)


async def on_reset(c: ServiceContext) -> None:
    await engine.seed_dealers(c)


async def _optimiser_tick() -> None:
    async with ctx.session() as s:
        result = await engine.run_optimiser(ctx, s)
        await s.commit()
    if result["matchesProposed"]:
        ctx.hub.publish("optimiser", {"type": "optimiser_run", "data": result})


spec.on_reset = on_reset
ctx.scheduler.every(engine.config.optimiser_every_s, "optimiser", _optimiser_tick)


@app.get("/v1/entities", tags=["reference"])
async def entities() -> list[dict]:
    rows = [{"entity": OUR_FIRM["wematch_entity"], "name": OUR_FIRM["name"], "lei": OUR_FIRM["lei"], "self": True}]
    rows += [{"entity": c.wematch_entity, "name": c.name, "lei": c.lei, "kind": c.kind} for c in counterparties()
             if c.kind != "INTERNAL"]
    return rows


@app.post("/v1/interest-lists", tags=["lists"], status_code=201)
async def upload_list(request: Request, who: str = Depends(member)) -> dict:
    """Upload a list as JSON (``ListIn``), as ``text/csv``, or as a multipart ``file`` field."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        body = engine.ListIn.model_validate(await request.json())
        raw = [ln.model_dump() for ln in body.lines]
        name, product, ref, auto = body.name, body.product, body.clientRef, body.autoAccept
    else:
        q = request.query_params
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            f = form.get("file")
            if not isinstance(f, UploadFile) and not hasattr(f, "read"):
                raise HTTPException(422, "multipart upload needs a 'file' field")
            text = (await f.read()).decode("utf-8-sig")
            q = {**q, **{k: v for k, v in form.items() if k != "file"}}
        else:
            text = (await request.body()).decode("utf-8-sig")
        raw = engine.parse_csv(text)
        name, product = q.get("name", "csv upload"), q.get("product", "SBL")
        ref, auto = q.get("clientRef"), str(q.get("autoAccept", "false")).lower() == "true"
    async with ctx.session() as s:
        out = await engine.upload(ctx, s, raw, name, product, ref, auto)
        await s.commit()
    return out


@app.get("/v1/interest-lists", tags=["lists"])
async def lists(who: str = Depends(member)) -> list[dict]:
    async with ctx.session() as s:
        return [engine.list_json(x) for x in (await s.execute(select(InterestList).order_by(InterestList.id))).scalars()]


@app.get("/v1/interest-lists/{list_id}", tags=["lists"])
async def get_list(list_id: str, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        lst = (await s.execute(select(InterestList).where(InterestList.list_id == list_id))).scalar()
        if lst is None:
            raise HTTPException(404, "no such list")
        lines = (await s.execute(select(ListLine).where(ListLine.list_id == list_id).order_by(ListLine.line_no))).scalars()
        return {**engine.list_json(lst), "lineItems": [engine.line_json(ln) for ln in lines]}


@app.delete("/v1/interest-lists/{list_id}", tags=["lists"])
async def withdraw_list(list_id: str, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        lst = (await s.execute(select(InterestList).where(InterestList.list_id == list_id))).scalar()
        if lst is None:
            raise HTTPException(404, "no such list")
        lst.status = "WITHDRAWN"
        await ctx.events.emit(s, "list_withdrawn", "list", list_id, engine.list_json(lst), topic="lists")
        await s.commit()
        return engine.list_json(lst)


class OptimiserRun(BaseModel):
    listId: str | None = None


@app.post("/v1/optimiser/run", tags=["matching"])
async def optimiser(body: OptimiserRun | None = None, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        out = await engine.run_optimiser(ctx, s, body.listId if body else None)
        await s.commit()
    return out


@app.get("/v1/matches", tags=["matching"])
async def matches(status: str | None = None, listId: str | None = None, limit: int = Query(500, le=10_000),
                  who: str = Depends(member)) -> list[dict]:
    async with ctx.session() as s:
        q = select(Match).order_by(Match.id).limit(limit)
        if status:
            q = q.where(Match.status == status.upper())
        if listId:
            q = q.where(Match.list_id == listId)
        return [engine.match_json(m) for m in (await s.execute(q)).scalars()]


@app.get("/v1/matches/{match_id}", tags=["matching"])
async def match(match_id: str, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        m = (await s.execute(select(Match).where(Match.match_id == match_id))).scalar()
        if m is None:
            raise HTTPException(404, "no such match")
        return engine.match_json(m)


class ProposeBody(BaseModel):
    feeBps: float = Field(..., ge=0)
    quantity: int | None = Field(None, gt=0)


@app.post("/v1/matches/{match_id}/propose", tags=["negotiation"])
async def propose(match_id: str, body: ProposeBody, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        m = await engine.propose(ctx, s, match_id, body.feeBps, body.quantity)
        await s.commit()
        return engine.match_json(m)


@app.post("/v1/matches/{match_id}/counter", tags=["negotiation"])
async def counter(match_id: str, body: ProposeBody, who: str = Depends(member)) -> dict:
    return await propose(match_id, body, who)


@app.post("/v1/matches/{match_id}/accept", tags=["negotiation"])
async def accept(match_id: str, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        m = await engine.accept(ctx, s, match_id)
        await s.commit()
        return engine.match_json(m)


@app.post("/v1/matches/{match_id}/reject", tags=["negotiation"])
async def reject(match_id: str, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        m = await engine.reject(ctx, s, match_id)
        await s.commit()
        return engine.match_json(m)


@app.get("/v1/loans", tags=["lifecycle"])
async def loans(status: str | None = None, who: str = Depends(member)) -> list[dict]:
    async with ctx.session() as s:
        q = select(Loan).order_by(Loan.id)
        if status:
            q = q.where(Loan.status == status.upper())
        return [engine.loan_json(x) for x in (await s.execute(q)).scalars()]


class RerateBody(BaseModel):
    feeBps: float = Field(..., ge=0)


class ReturnBody(BaseModel):
    quantity: int | None = Field(None, gt=0)


@app.post("/v1/loans/{loan_id}/rerate", tags=["lifecycle"])
async def rerate(loan_id: str, body: RerateBody, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        ln = await engine.rerate(ctx, s, loan_id, body.feeBps)
        await s.commit()
        return engine.loan_json(ln)


@app.post("/v1/loans/{loan_id}/return", tags=["lifecycle"])
async def return_loan(loan_id: str, body: ReturnBody | None = None, who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        ln = await engine.return_qty(ctx, s, loan_id, body.quantity if body else None)
        await s.commit()
        return engine.loan_json(ln)


@app.post("/v1/loans/{loan_id}/recall", tags=["lifecycle"])
async def recall_loan(loan_id: str, body: ReturnBody | None = None, who: str = Depends(member)) -> dict:
    return await return_loan(loan_id, body, who)


@app.get("/v1/stats", tags=["lifecycle"])
async def stats(who: str = Depends(member)) -> dict:
    async with ctx.session() as s:
        return await engine.stats(s)


@app.websocket("/v1/stream")
async def stream(ws: WebSocket, key: str = Query(""), topics: str = "lists,matches,loans,optimiser") -> None:
    if key != api_key():
        await ws.close(code=4401)
        return
    await ctx.ws_stream(ws, [t.strip() for t in topics.split(",") if t.strip()])


@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {"entity": OUR_FIRM["wematch_entity"], "header": "X-Api-Key", "apiKey": api_key()}


@app.get("/_admin/dealer-lines", tags=["admin"])
async def dealer_lines(side: str | None = None, limit: int = Query(100, le=10_000)) -> list[dict]:
    """Simulated dealer interest (hidden from members on a real venue; exposed here for tests/demos)."""
    from .models import DealerLine

    async with ctx.session() as s:
        q = select(DealerLine).where(DealerLine.qty > 0).order_by(DealerLine.id).limit(limit)
        if side:
            q = q.where(DealerLine.side == side.upper())
        return [{"id": d.id, "dealer": d.dealer, "instrumentId": d.instrument_id, "side": d.side, "quantity": d.qty,
                 "feeMin": d.fee_min, "feeMax": d.fee_max} for d in (await s.execute(q)).scalars()]


@app.get("/_admin/config", tags=["admin"])
async def get_config() -> dict:
    return engine.config.model_dump()


@app.put("/_admin/config", tags=["admin"])
async def put_config(cfg: engine.EngineConfig) -> dict:
    engine.config = cfg
    return cfg.model_dump()


@ctx.verb("optimise")
async def verb_optimise(args: dict[str, Any]) -> dict:
    """Run the optimiser now: {listId?}"""
    async with ctx.session() as s:
        out = await engine.run_optimiser(ctx, s, args.get("listId"))
        await s.commit()
    return out


@ctx.verb("config")
async def verb_config(args: dict[str, Any]) -> dict:
    """Patch engine config, e.g. {dealer_accept_p: 1.0}"""
    engine.config = engine.EngineConfig(**{**engine.config.model_dump(), **args})
    return engine.config.model_dump()
