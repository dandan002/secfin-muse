"""mock-gateway: the "smart API" counterparties use to send us locates and orders.

Counterparty side (bearer API key per counterparty, see mockcore.keys):
  ``POST /v1/locates`` single or batch (<= 5,000 lines) as CDM-shaped JSON, flat JSON or CSV,
  with ``Idempotency-Key`` and per-key rate limits and quotas; ``GET /v1/locates/{id}``;
  ``POST /v1/orders``.
Our platform's side (the platform's bearer key):
  ``GET /v1/inbox`` / WS ``/v1/inbox/stream`` to pull new requests (or set a downstream URL to
  have them pushed), and ``POST /v1/decisions`` to answer; decisions go back to the
  counterparty's callback URL as HMAC-signed webhooks with retries.
Simulated counterparties: ``/cp/{code}/callback`` receivers record what they get, and a load
generator produces Poisson arrivals with a 09:00-09:45 New York burst.
The clients' trading: at each close, every locate expiring then is used by its client or not (a short
sale against it), clients buy back some of their shorts, and ``GET /v1/stock-record`` publishes the
day's client short positions as the prime broker's stock record would. Some of the shorts settling at a close
fail to deliver at CNS: ``GET /v1/fails`` lists them, and ``POST /v1/fails/{id}/deliver`` delivers against one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Literal

from fastapi import Depends, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select

from mockcore import auth, clock, rng
from mockcore.app import ServiceContext, ServiceSpec, create_app
from mockcore.keys import DEFAULT_PLANS, AuthSpec, Grant, Plan, Principal
from refdata import calendar, market
from refdata import identifiers as ids
from refdata.counterparties import Counterparty, counterparties, counterparty_by_code
from refdata.universe import resolve, universe

from .models import (
    Base,
    ClientCharge,
    CnsBuyIn,
    CnsFail,
    CpOrder,
    CpReceived,
    DayClose,
    Delivery,
    LocateLine,
    LocateRequest,
    ShortPosition,
    ShortSale,
)

MAX_LINES = 5000
DOWNSTREAM = os.environ.get("MOCK_GATEWAY_DOWNSTREAM_URL", "")

CP_SCOPES = {
    "locates:write": "Send locate requests",
    "locates:read": "Your locate requests and their decisions",
    "orders:write": "Send orders",
}
PLATFORM_SCOPES = {
    "inbox:read": "Pull new requests (REST and stream) and the stock record",
    "decisions:write": "Answer locate requests",
    "deliveries:read": "Webhook delivery log",
}
PLATFORM = "PLATFORM"


def _grants() -> list[Grant]:
    cps = [Grant(c.code, tuple(CP_SCOPES), "standard", c.name) for c in counterparties() if c.kind != "INTERNAL"]
    return [Grant(PLATFORM, tuple(PLATFORM_SCOPES), "partner", "LoanDesk platform"), *cps]


spec = ServiceSpec(
    name="mock-gateway",
    label="Counterparty Gateway",
    schema="gateway",
    base=Base,
    description=(
        "Counterparty-facing locate/order API with idempotency, rate limits, signed webhooks and a load generator."
    ),
    auth=AuthSpec({**CP_SCOPES, **PLATFORM_SCOPES}, _grants),
)
ctx = ServiceContext(spec)
app = create_app(ctx)


def webhook_secret(code: str) -> str:
    """Signs webhooks to a counterparty; derived from the key ring's master secret, like its API key."""
    return ctx.gate.derive("webhook", code)


def downstream_token() -> str:
    """Sent as ``X-Gateway-Token`` on pushes to the platform, so it can tell them from anyone else's."""
    return ctx.gate.derive("downstream")


# -- auth -------------------------------------------------------------------------------------------
def counterparty(scope: str):  # noqa: ANN201 - a FastAPI dependency
    """The calling counterparty (its key's principal), entitled to ``scope``."""
    gate = ctx.require(scope)

    async def dep(p: Principal = Depends(gate)) -> Counterparty:
        cp = counterparty_by_code(p.principal)
        if cp is None:
            raise HTTPException(403, {"error": "forbidden", "detail": f"{p.principal} is not a counterparty"})
        return cp

    return dep


# -- normalisation ---------------------------------------------------------------------------------
def _norm_lines_cdm(doc: dict[str, Any]) -> tuple[str | None, date | None, str | None, list[dict[str, Any]]]:
    b = doc["borrowerLocate"]
    lines = [{"identifier": ln["security"]["identifier"], "idType": ln["security"].get("identifierType"),
              "quantity": ln["quantity"]["amount"], "maxFeeBps": (ln.get("maxFee") or {}).get("bps")}
             for ln in b.get("locateLines", [])]
    sd = b.get("settlementDate")
    return b.get("locateRequestId"), date.fromisoformat(sd) if sd else None, b.get("callbackUrl"), lines


def _norm_lines_flat(doc: dict[str, Any]) -> tuple[str | None, date | None, str | None, list[dict[str, Any]]]:
    if "lines" not in doc and "identifier" in doc:  # single-line shorthand
        doc = {**doc, "lines": [{k: doc[k] for k in ("identifier", "idType", "quantity", "maxFeeBps") if k in doc}]}
    sd = doc.get("settlementDate")
    return doc.get("requestId"), date.fromisoformat(sd) if sd else None, doc.get("callbackUrl"), doc.get("lines", [])


def _norm_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        raise HTTPException(422, "CSV has no header row")
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    id_col = next((cols[k] for k in ("identifier", "isin", "sedol", "cusip", "ticker", "security") if k in cols), None)
    qty_col = next((cols[k] for k in ("quantity", "qty", "shares") if k in cols), None)
    if not id_col or not qty_col:
        raise HTTPException(422, f"CSV needs identifier and quantity columns; got {reader.fieldnames}")
    type_col = cols.get("idtype") or cols.get("id_type")
    fee_col = cols.get("maxfeebps") or cols.get("max_fee_bps")
    out = []
    for row in reader:
        idt = (row.get(type_col) if type_col else None) or (id_col.upper() if id_col.lower() != "identifier" else None)
        out.append({"identifier": (row[id_col] or "").strip(), "idType": idt,
                    "quantity": (row[qty_col] or "0").replace(",", "").strip(),
                    "maxFeeBps": (row.get(fee_col) or "").strip() or None if fee_col else None})
    return out


def _resolve_line(raw: dict[str, Any]) -> dict[str, Any]:
    ident = str(raw.get("identifier", "")).strip()
    try:
        qty = int(float(raw.get("quantity", 0)))
    except (TypeError, ValueError):
        qty = 0
    try:
        max_fee = float(raw["maxFeeBps"]) if raw.get("maxFeeBps") not in (None, "") else None
    except (TypeError, ValueError):
        max_fee = None
    out: dict[str, Any] = {"identifier": ident, "idType": raw.get("idType"), "quantity": qty, "maxFeeBps": max_fee}
    if qty <= 0:
        return {**out, "status": "INVALID", "error": "quantity must be a positive number of shares"}
    inst = resolve(ident)
    if inst is None:
        kind = ids.detect(ident)
        hints = ids.suggest_fix(ident) if kind is None else []
        hints = [h for h in hints if resolve(h)]
        msg = "unknown security" if kind else "identifier fails check-digit validation or is unknown"
        return {**out, "status": "INVALID", "error": msg + (f"; did you mean {hints[0]}?" if hints else "")}
    return {**out, "status": "RECEIVED", "instrumentId": inst.id, "isin": inst.isin, "tier": inst.tier,
            "idType": raw.get("idType") or ids.detect(ident) or "TICKER"}


def line_json(ln: LocateLine) -> dict[str, Any]:
    return {"lineId": ln.id, "lineNo": ln.line_no, "identifier": ln.identifier, "idType": ln.id_type,
            "instrumentId": ln.instrument_id, "isin": ln.isin, "quantity": ln.quantity,
            "maxFeeBps": ln.max_fee_bps, "status": ln.status, "approvedQuantity": ln.approved_qty,
            "feeBps": ln.fee_bps, "rationale": ln.rationale, "regShoBasis": ln.reg_sho_basis,
            "enteredAt": clock.iso(ln.decided_at) if ln.decided_at else None,
            "expiresAt": clock.iso(ln.expires_at) if ln.expires_at else None,
            "usedQuantity": ln.used_qty, "error": ln.error}


def request_json(r: LocateRequest, lines: list[LocateLine]) -> dict[str, Any]:
    return {"locateId": r.locate_id, "requestId": r.request_id, "counterparty": r.cp_code, "format": r.format,
            "source": r.source, "settlementDate": r.settle_date.isoformat() if r.settle_date else None,
            "callbackUrl": r.callback_url, "status": r.status, "forwardStatus": r.forward_status,
            "receivedAt": clock.iso(r.received_at), "lines": [line_json(x) for x in lines]}


async def create_locate(cp: Counterparty, fmt: str, request_id: str | None, settle: date | None,
                        callback: str | None, raw_lines: list[dict[str, Any]], idem: str | None, body_hash: str,
                        source: str = "API") -> tuple[int, dict[str, Any]]:
    if not raw_lines:
        raise HTTPException(422, "no lines")
    if len(raw_lines) > MAX_LINES:
        raise HTTPException(413, f"max {MAX_LINES} lines per request")
    async with ctx.session() as s:
        if idem:
            prior = (await s.execute(select(LocateRequest).where(LocateRequest.cp_code == cp.code,
                                                                 LocateRequest.idempotency_key == idem))).scalar()
            if prior:
                if prior.body_hash != body_hash:
                    raise HTTPException(409, "Idempotency-Key reused with a different body")
                return 200, {**prior.response, "idempotentReplay": True}
        now = clock.now()
        req = LocateRequest(
            locate_id="pending", request_id=request_id, cp_code=cp.code, idempotency_key=idem, body_hash=body_hash,
            format=fmt, source=source, settle_date=settle or calendar.settle_date(now),
            callback_url=callback or f"{ctx.cfg.gateway_url}/cp/{cp.code}/callback", status="RECEIVED",
            forward_status="PENDING" if DOWNSTREAM else "SINK", received_at=now, response={},
        )
        s.add(req)
        await s.flush()
        req.locate_id = f"LOC-{now:%Y%m%d}-{req.id:06d}"
        rows = []
        for n, raw in enumerate(raw_lines, start=1):
            r = _resolve_line(raw)
            rows.append(LocateLine(locate_id=req.locate_id, line_no=n, identifier=r["identifier"][:64],
                                   id_type=r.get("idType"), instrument_id=r.get("instrumentId"), isin=r.get("isin"),
                                   tier=r.get("tier"), quantity=r["quantity"], max_fee_bps=r.get("maxFeeBps"),
                                   status=r["status"], error=r.get("error")))
        s.add_all(rows)
        await s.flush()
        body = {"locateId": req.locate_id, "requestId": request_id, "status": "RECEIVED",
                "receivedAt": clock.iso(now), "lines": len(rows),
                "invalidLines": [{"lineNo": x.line_no, "identifier": x.identifier, "error": x.error}
                                 for x in rows if x.status == "INVALID"]}
        req.response = body
        await ctx.events.emit(s, "locate_received", "locate", req.locate_id, request_json(req, rows), topic="inbox")
        await s.commit()
    if DOWNSTREAM:
        ctx.scheduler.after(0, f"forward:{req.locate_id}", forward_job, req.locate_id, 1)
    return 202, body


async def forward_job(locate_id: str, attempt: int) -> None:
    async with ctx.session() as s:
        req = (await s.execute(select(LocateRequest).where(LocateRequest.locate_id == locate_id))).scalar_one()
        lines = (await s.execute(select(LocateLine).where(LocateLine.locate_id == locate_id)
                                 .order_by(LocateLine.line_no))).scalars().all()
        payload = request_json(req, list(lines))
        try:
            r = await ctx.http.post(f"{DOWNSTREAM.rstrip('/')}/locates", json=payload,
                                    headers={"X-Gateway-Token": downstream_token()})
            ok = r.status_code < 300
        except Exception:
            ok = False
        req.forward_status = "FORWARDED" if ok else ("FAILED" if attempt >= 5 else "PENDING")
        await s.commit()
    if not ok and attempt < 5:
        ctx.scheduler.after(2**attempt, f"forward:{locate_id}", forward_job, locate_id, attempt + 1)


# -- counterparty API -------------------------------------------------------------------------------
@app.post("/v1/locates", tags=["counterparty"], status_code=202)
async def post_locates(request: Request, cp: Counterparty = Depends(counterparty("locates:write")),
                       idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    raw = await request.body()
    ctype = request.headers.get("content-type", "")
    body_hash = hashlib.sha256(raw).hexdigest()
    if ctype.startswith("text/csv"):
        q = request.query_params
        fmt, rid, cb = "CSV", q.get("requestId"), q.get("callbackUrl")
        sd = date.fromisoformat(q["settlementDate"]) if q.get("settlementDate") else None
        lines = _norm_csv(raw.decode("utf-8-sig"))
    else:
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"invalid JSON: {e}") from e
        if "borrowerLocate" in doc:
            fmt, (rid, sd, cb, lines) = "CDM", _norm_lines_cdm(doc)
        else:
            fmt, (rid, sd, cb, lines) = "FLAT", _norm_lines_flat(doc)
        if doc.get("counterparty") and counterparty_by_code(doc["counterparty"]) not in (cp, None):
            raise HTTPException(403, "counterparty in body does not match API key")
    status, body = await create_locate(cp, fmt, rid, sd, cb, lines, idempotency_key, body_hash)
    return JSONResponse(body, status_code=status)


@app.get("/v1/locates/{locate_id}", tags=["counterparty"])
async def get_locate(locate_id: str, cp: Counterparty = Depends(counterparty("locates:read"))) -> dict:
    async with ctx.session() as s:
        req = (await s.execute(select(LocateRequest).where(LocateRequest.locate_id == locate_id,
                                                           LocateRequest.cp_code == cp.code))).scalar()
        if req is None:
            raise HTTPException(404, "no such locate")
        lines = (await s.execute(select(LocateLine).where(LocateLine.locate_id == locate_id)
                                 .order_by(LocateLine.line_no))).scalars().all()
        return request_json(req, list(lines))


@app.get("/v1/locates", tags=["counterparty"])
async def my_locates(limit: int = Query(100, le=1000),
                     cp: Counterparty = Depends(counterparty("locates:read"))) -> list[dict]:
    async with ctx.session() as s:
        reqs = (await s.execute(select(LocateRequest).where(LocateRequest.cp_code == cp.code)
                                .order_by(LocateRequest.id.desc()).limit(limit))).scalars().all()
        return [r.response | {"status": r.status} for r in reqs]


class OrderIn(BaseModel):
    clientOrderId: str
    side: Literal["BORROW", "LOAN", "RETURN", "RECALL"]
    identifier: str
    quantity: int = Field(..., gt=0)
    feeBps: float | None = None
    term: Literal["OPEN", "TERM"] = "OPEN"
    settlementDate: date | None = None


@app.post("/v1/orders", tags=["counterparty"], status_code=202)
async def post_order(body: OrderIn, cp: Counterparty = Depends(counterparty("orders:write")),
                     idempotency_key: str | None = Header(default=None)) -> dict:
    inst = resolve(body.identifier)
    if inst is None:
        raise HTTPException(422, f"unknown security {body.identifier!r}")
    async with ctx.session() as s:
        if idempotency_key:
            prior = (await s.execute(select(CpOrder).where(CpOrder.cp_code == cp.code,
                                                           CpOrder.idempotency_key == idempotency_key))).scalar()
            if prior:
                return {"orderId": prior.order_id, "status": prior.status, "idempotentReplay": True}
        o = CpOrder(order_id="pending", cp_code=cp.code, idempotency_key=idempotency_key,
                    body={**body.model_dump(mode="json"), "instrumentId": inst.id, "isin": inst.isin},
                    status="RECEIVED", received_at=clock.now())
        s.add(o)
        await s.flush()
        o.order_id = f"CPO-{o.id:06d}"
        await ctx.events.emit(s, "order_received", "order", o.order_id, {"orderId": o.order_id, "counterparty": cp.code,
                              **o.body}, topic="inbox")
        await s.commit()
        return {"orderId": o.order_id, "status": "RECEIVED"}


# -- platform API ---------------------------------------------------------------------------------
@app.get("/v1/inbox", tags=["platform"], dependencies=[Depends(ctx.require("inbox:read"))])
async def inbox(status: str = "RECEIVED", after: int = 0, limit: int = Query(200, le=5000)) -> list[dict]:
    async with ctx.session() as s:
        reqs = (await s.execute(select(LocateRequest).where(LocateRequest.status == status.upper(),
                                                            LocateRequest.id > after)
                                .order_by(LocateRequest.id).limit(limit))).scalars().all()
        out = []
        for r in reqs:
            lines = (await s.execute(select(LocateLine).where(LocateLine.locate_id == r.locate_id)
                                     .order_by(LocateLine.line_no))).scalars().all()
            out.append({"seq": r.id, **request_json(r, list(lines))})
        return out


@app.websocket("/v1/inbox/stream")
async def inbox_stream(ws: WebSocket) -> None:
    """Pass the platform key as ``?token=`` (or ``?key=``)."""
    if await ctx.gate.websocket(ws, "inbox:read") is None:
        return
    await ctx.ws_stream(ws, ["inbox", "decisions"])


class LineDecision(BaseModel):
    lineNo: int
    decision: Literal["APPROVED", "PARTIAL", "REJECTED"]
    approvedQuantity: int = Field(0, ge=0)
    feeBps: float | None = Field(None, validation_alias=AliasChoices("feeBps", "indicativeFeeBps"),
                                 description="The agreed fee the client pays on what it shorts against this locate")
    rationale: str | None = None
    regShoBasis: str | None = None
    expiresAt: datetime | None = Field(None, description="Default: today's close (16:00 New York), or the next "
                                                         "business day's if decided after it")


class DecisionIn(BaseModel):
    locateId: str
    lines: list[LineDecision] = Field(..., min_length=1)


@app.post("/v1/decisions", tags=["platform"], dependencies=[Depends(ctx.require("decisions:write"))])
async def decide(body: DecisionIn) -> dict:
    async with ctx.session() as s:
        req = (await s.execute(select(LocateRequest).where(LocateRequest.locate_id == body.locateId)
                               .with_for_update())).scalar()
        if req is None:
            raise HTTPException(404, "no such locate")
        lines = {x.line_no: x for x in (await s.execute(select(LocateLine).where(
            LocateLine.locate_id == body.locateId))).scalars()}
        now = clock.now()
        expiry = calendar.locate_expiry(now)
        decided = []
        for d in body.lines:
            ln = lines.get(d.lineNo)
            if ln is None:
                raise HTTPException(422, f"no line {d.lineNo}")
            if ln.status == "INVALID":
                raise HTTPException(409, f"line {d.lineNo} is INVALID and cannot be decided")
            if d.approvedQuantity > ln.quantity:
                raise HTTPException(422, f"line {d.lineNo}: approved quantity exceeds requested")
            ln.status = d.decision
            ln.approved_qty = 0 if d.decision == "REJECTED" else d.approvedQuantity or ln.quantity
            ln.fee_bps, ln.rationale, ln.reg_sho_basis = d.feeBps, d.rationale, d.regShoBasis
            ln.expires_at, ln.decided_at = d.expiresAt or expiry, now
            decided.append(ln)
        open_lines = [x for x in lines.values() if x.status == "RECEIVED"]
        req.status = "PARTIALLY_DECIDED" if open_lines else "DECIDED"
        payload = {"locateId": req.locate_id, "requestId": req.request_id, "counterparty": req.cp_code,
                   "status": req.status, "decidedAt": clock.iso(now), "lines": [line_json(x) for x in decided]}
        await ctx.events.emit(s, "locate_decided", "locate", req.locate_id, payload, topic="decisions")
        await s.commit()
    if req.callback_url:
        ctx.scheduler.after(0, f"deliver:{req.locate_id}:{clock.iso(now)}", deliver_job, req.locate_id,
                            req.callback_url, payload, 1)
    return payload


async def deliver_job(locate_id: str, url: str, payload: dict[str, Any], attempt: int) -> None:
    cp = counterparty_by_code(payload["counterparty"])
    body = json.dumps(payload, sort_keys=True).encode()
    sends = ctx.faults.outbound(body)  # chaos drills: dropped / duplicated webhooks
    status_code, err = None, None
    for b in sends:
        try:
            headers = {"content-type": "application/json", "X-Mock-Delivery-Attempt": str(attempt),
                       auth.SIGNATURE_HEADER: auth.sign(webhook_secret(cp.code), b)}
            r = await ctx.http.post(url, content=b, headers=headers)
            status_code = r.status_code
        except Exception as e:
            err = type(e).__name__
    ok = status_code is not None and status_code < 300
    async with ctx.session() as s:
        s.add(Delivery(locate_id=locate_id, url=url, attempt=attempt, status_code=status_code, ok=ok,
                       error=err or (None if sends else "dropped (fault injection)"), ts=clock.now()))
        await s.commit()
    if not ok and attempt < 5 and sends:
        ctx.scheduler.after(2**attempt, f"deliver:{locate_id}:{attempt + 1}", deliver_job, locate_id, url, payload,
                            attempt + 1)


@app.get("/v1/deliveries", tags=["platform"], dependencies=[Depends(ctx.require("deliveries:read"))])
async def deliveries(locateId: str | None = None) -> list[dict]:
    async with ctx.session() as s:
        q = select(Delivery).order_by(Delivery.id)
        if locateId:
            q = q.where(Delivery.locate_id == locateId)
        return [{"locateId": d.locate_id, "url": d.url, "attempt": d.attempt, "statusCode": d.status_code,
                 "ok": d.ok, "error": d.error, "ts": clock.iso(d.ts)} for d in (await s.execute(q)).scalars()]


# -- the clients' trading: locates used or not, shorts, the stock record ----------------------------------
# How likely a client is to short against a locate it was granted. Funds trade what they locate; other
# requesters shop several desks and use fewer.
USE_RATE = {"HEDGE_FUND": 0.7, "PRIME_BROKER": 0.4, "BROKER_DEALER": 0.3}
COVER_RATE = 0.25  # chance a client buys back some of a short at a close
FAIL_RATE = 0.03  # chance a new US short settling at a close fails to deliver at CNS (verb ``fail_rate``)
BUY_IN_AFTER_DAYS = 3  # business days a fail stays open before the buyer buys in (verb ``cns_buy_in_after``)
_fails = {"rate": FAIL_RATE, "buyInAfterDays": BUY_IN_AFTER_DAYS}


def _lot(q: float) -> int:
    return int(q // 100 * 100)


async def close_due() -> int:
    """Run every business-day close that has passed and isn't recorded yet (in order). Returns how many ran."""
    now = clock.now()
    today = calendar.trade_date(now)
    async with ctx.session() as s:
        last = (await s.execute(select(DayClose.trade_date).order_by(DayClose.trade_date.desc()).limit(1))).scalar()
        if last is not None:
            d = last + timedelta(days=1)
        else:  # first close: start from the oldest locate still waiting on one
            first = (await s.execute(select(LocateLine.expires_at).where(LocateLine.used_qty.is_(None),
                                                                         LocateLine.expires_at.is_not(None))
                                     .order_by(LocateLine.expires_at).limit(1))).scalar()
            d = min(calendar.trade_date(first), today) if first else today
        ran = 0
        while d <= today and ran < 30:
            if calendar.is_business_day(d) and calendar.close_at(d) <= now:
                await close_day(s, d)
                ran += 1
            d += timedelta(days=1)
        await s.commit()
    return ran


async def close_day(s, d: date) -> dict[str, Any]:
    """One close: each locate expiring by it is used by its client or not; some shorts are bought back; the
    day's client short positions are written as the stock record, settling the next business day."""
    if await s.get(DayClose, d) is not None:
        raise HTTPException(409, f"{d} is already closed")
    r = rng.stream("gateway", f"close:{d.isoformat()}")
    close = calendar.close_at(d)
    lines = (await s.execute(select(LocateLine, LocateRequest.cp_code)
                             .join(LocateRequest, LocateRequest.locate_id == LocateLine.locate_id)
                             .where(LocateLine.status.in_(("APPROVED", "PARTIAL")), LocateLine.used_qty.is_(None),
                                    LocateLine.expires_at <= close)
                             .order_by(LocateLine.id))).all()
    sold: dict[tuple[str, str], int] = {}
    for ln, cp_code in lines:
        cp = counterparty_by_code(cp_code)
        over_limit = ln.max_fee_bps is not None and (ln.fee_bps or 0) > ln.max_fee_bps
        used = 0
        if not over_limit and r.random() < USE_RATE.get(cp.kind if cp else "", 0.3):
            used = max(_lot((ln.approved_qty or 0) * r.uniform(0.25, 1.0)), min(ln.approved_qty or 0, 100))
        ln.used_qty, ln.used_on = used, d
        if used and ln.instrument_id:
            start = ln.decided_at or close
            end = min(ln.expires_at or close, close)
            at = start + (end - start) * r.random() if end > start else start
            s.add(ShortSale(trade_date=d, cp_code=cp_code, instrument_id=ln.instrument_id, quantity=used,
                            locate_id=ln.locate_id, line_no=ln.line_no, executed_at=at))
            sold[(cp_code, ln.instrument_id)] = sold.get((cp_code, ln.instrument_id), 0) + used
    prev_date = (await s.execute(select(ShortPosition.trade_date).where(ShortPosition.trade_date < d)
                                 .order_by(ShortPosition.trade_date.desc()).limit(1))).scalar()
    prev = {} if prev_date is None else {
        (p.cp_code, p.instrument_id): p.short_qty for p in (await s.execute(
            select(ShortPosition).where(ShortPosition.trade_date == prev_date))).scalars()}
    bought: dict[tuple[str, str], int] = {}  # CNS buy-ins since the last close: they cover the client's short
    for b in (await s.execute(select(CnsBuyIn).where(CnsBuyIn.applied_on.is_(None), CnsBuyIn.ts <= close))
              ).scalars():
        bought[(b.cp_code, b.instrument_id)] = bought.get((b.cp_code, b.instrument_id), 0) + b.qty
        b.applied_on = d
    for key in sorted(set(prev) | set(sold)):
        held = prev.get(key, 0)
        covered = _lot(held * r.uniform(0.2, 1.0)) if held and r.random() < COVER_RATE else 0
        covered = min(max(covered, bought.get(key, 0)), held)
        new = sold.get(key, 0)
        if held - covered + new or new or covered:
            s.add(ShortPosition(trade_date=d, cp_code=key[0], instrument_id=key[1], short_qty=held - covered + new,
                                sold_qty=new, covered_qty=covered))
    s.add(DayClose(trade_date=d, closed_at=clock.now()))
    failed = await _settle_fails(s, d, prev_date)
    await s.flush()
    summary = {"tradeDate": d.isoformat(), "settlementDate": calendar.add_business_days(d, 1).isoformat(),
               "locates": len(lines), "used": sum(1 for ln, _ in lines if ln.used_qty),
               "shortSales": sum(sold.values()), "failsToDeliver": failed}
    await ctx.events.emit(s, "stock_record", "stock_record", d.isoformat(), summary, topic="inbox")
    return summary


async def _settle_fails(s, d: date, prev_date: date | None) -> int:
    """The shorts sold at the last close settle today: some of the US ones fail to deliver at CNS (a seeded
    share of each, for the desk to close out under Reg SHO Rule 204)."""
    if prev_date is None or calendar.add_business_days(prev_date, 1) != d or _fails["rate"] <= 0:
        return 0
    r = rng.stream("gateway", f"fails:{d.isoformat()}")
    by_id = {i.id: i for i in universe()}
    n = 0
    for p in (await s.execute(select(ShortPosition).where(ShortPosition.trade_date == prev_date,
                                                          ShortPosition.sold_qty > 0)
                              .order_by(ShortPosition.cp_code, ShortPosition.instrument_id))).scalars().all():
        inst = by_id.get(p.instrument_id)
        if inst is None or inst.country != "US" or r.random() >= _fails["rate"]:
            continue
        await _add_fail(s, d, p.cp_code, p.instrument_id, max(_lot(p.sold_qty * r.uniform(0.25, 1.0)), 100),
                        at=calendar.close_at(d))
        n += 1
    return n


async def _add_fail(s, d: date, cp_code: str, instrument_id: str, qty: int, *, at: datetime) -> CnsFail:
    f = CnsFail(fail_id="pending", settle_date=d, cp_code=cp_code, instrument_id=instrument_id, qty=qty,
                outstanding=qty, status="OPEN", ts=at, deliveries=[])
    s.add(f)
    await s.flush()
    f.fail_id = f"CNS-F-{f.id:06d}"
    await ctx.events.emit(s, "fail_to_deliver", "fail", f.fail_id, fail_json(f), topic="inbox")
    return f


def fail_json(f: CnsFail) -> dict[str, Any]:
    inst = next((i for i in universe() if i.id == f.instrument_id), None)
    return {"failId": f.fail_id, "settlementDate": f.settle_date.isoformat(), "counterparty": f.cp_code,
            "instrumentId": f.instrument_id, "isin": inst.isin if inst else None, "quantity": f.qty,
            "outstanding": f.outstanding, "status": f.status, "failedAt": clock.iso(f.ts),
            "closedAt": clock.iso(f.closed_at) if f.closed_at else None, "deliveries": f.deliveries or [],
            "boughtIn": f.bought_in_qty or 0}


def buy_in_json(b: CnsBuyIn) -> dict[str, Any]:
    return {"buyInId": b.buy_in_id, "failId": b.fail_id, "counterparty": b.cp_code, "instrumentId": b.instrument_id,
            "quantity": b.qty, "price": b.price, "salePrice": b.sale_price, "amountDue": b.amount_due,
            "boughtInAt": clock.iso(b.ts)}


async def _buy_in(s, f: CnsFail, qty: int | None = None, price: float | None = None) -> CnsBuyIn:
    """The buyer buys in what a fail still owes, at the market plus a premium; we owe the difference."""
    qty = min(qty or f.outstanding, f.outstanding)
    if f.status != "OPEN" or qty <= 0:
        raise HTTPException(409, f"{f.fail_id} has nothing to buy in ({f.status})")
    inst = next(i for i in universe() if i.id == f.instrument_id)
    r = rng.stream("gateway", f"buy-in:{f.fail_id}:{f.bought_in_qty or 0}")
    px = round(price or market.price(inst, clock.now()) * (1 + r.uniform(0.005, 0.03)), 4)
    sale = round(market.price(inst, calendar.close_at(f.settle_date)), 4)
    f.outstanding -= qty
    f.bought_in_qty = (f.bought_in_qty or 0) + qty
    if f.outstanding == 0:
        f.status, f.closed_at = "BOUGHT_IN", clock.now()
    b = CnsBuyIn(buy_in_id="pending", fail_id=f.fail_id, cp_code=f.cp_code, instrument_id=f.instrument_id, qty=qty,
                 price=px, sale_price=sale, amount_due=round(qty * (px - sale), 2), ts=clock.now())
    s.add(b)
    await s.flush()
    b.buy_in_id = f"CNS-B-{b.id:06d}"
    await ctx.events.emit(s, "fail_bought_in", "fail", f.fail_id, {**fail_json(f), "buyIn": buy_in_json(b)},
                          topic="inbox")
    return b


async def _auto_buy_ins() -> None:
    """Fails still open ``buyInAfterDays`` business days after they failed are bought in, in market hours."""
    now = clock.now()
    today = calendar.trade_date(now)
    if _fails["buyInAfterDays"] is None or not calendar.is_business_day(today) or not (
            dtime(9, 30) <= now.astimezone(clock.NEW_YORK).time() < calendar.MARKET_CLOSE):
        return
    async with ctx.session() as s:
        for f in (await s.execute(select(CnsFail).where(CnsFail.status == "OPEN").order_by(CnsFail.id))
                  ).scalars().all():
            if calendar.add_business_days(f.settle_date, _fails["buyInAfterDays"]) <= today:
                await _buy_in(s, f)
        await s.commit()


ctx.scheduler.every(60, "cns-buy-ins", _auto_buy_ins)


ctx.scheduler.every(60, "close", close_due)


@app.get("/v1/stock-record", tags=["platform"], dependencies=[Depends(ctx.require("inbox:read"))])
async def stock_record(date_: date | None = Query(None, alias="date",
                                                  description="Trade date; default the latest close")) -> dict:
    """Client short positions at a close (settling the next business day), and the short sales done that day
    against each locate. A real prime broker reads this from its own books and records."""
    async with ctx.session() as s:
        q = select(DayClose).order_by(DayClose.trade_date.desc()).limit(1) if date_ is None else \
            select(DayClose).where(DayClose.trade_date == date_)
        dc = (await s.execute(q)).scalar()
        if dc is None:
            raise HTTPException(404, "no close yet" if date_ is None else f"{date_} has not closed")
        d = dc.trade_date
        positions = (await s.execute(select(ShortPosition).where(ShortPosition.trade_date == d)
                                     .order_by(ShortPosition.cp_code, ShortPosition.instrument_id))).scalars().all()
        sales = (await s.execute(select(ShortSale).where(ShortSale.trade_date == d)
                                 .order_by(ShortSale.executed_at))).scalars().all()
        usage = (await s.execute(select(LocateLine).where(LocateLine.used_on == d)
                                 .order_by(LocateLine.id))).scalars().all()
    by_id = {i.id: i for i in universe()}
    return {
        "tradeDate": d.isoformat(), "settlementDate": calendar.add_business_days(d, 1).isoformat(),
        "closedAt": clock.iso(dc.closed_at),
        "positions": [{"counterparty": p.cp_code, "instrumentId": p.instrument_id,
                       "isin": by_id[p.instrument_id].isin if p.instrument_id in by_id else None,
                       "shortQuantity": p.short_qty, "soldQuantity": p.sold_qty, "coveredQuantity": p.covered_qty}
                      for p in positions],
        "shortSales": [{"counterparty": x.cp_code, "instrumentId": x.instrument_id, "quantity": x.quantity,
                        "locateId": x.locate_id, "lineNo": x.line_no, "executedAt": clock.iso(x.executed_at)}
                       for x in sales],
        "locateUsage": [{"locateId": x.locate_id, "lineNo": x.line_no, "approvedQuantity": x.approved_qty,
                         "usedQuantity": x.used_qty, "feeBps": x.fee_bps, "maxFeeBps": x.max_fee_bps}
                        for x in usage],
    }


@ctx.verb("close_day")
async def verb_close_day(args: dict[str, Any]) -> dict:
    """Run a close now, as if it had come: {date?} (default today). Locates expiring by it are used or not."""
    d = date.fromisoformat(args["date"]) if args.get("date") else calendar.trade_date(clock.now())
    async with ctx.session() as s:
        summary = await close_day(s, d)
        await s.commit()
    return summary


# -- fails to deliver at CNS ------------------------------------------------------------------------
class DeliverIn(BaseModel):
    quantity: int = Field(..., gt=0)
    ref: str | None = None  # the desk's own reference for the delivery


@app.get("/v1/fails", tags=["platform"], dependencies=[Depends(ctx.require("inbox:read"))])
async def fails(status: str | None = None, after: int = 0, limit: int = Query(500, le=5000)) -> list[dict]:
    """Fails to deliver at CNS on client short sales, oldest first (``after``: the last ``seq`` you've seen). One
    stays OPEN until we deliver the shares against it."""
    async with ctx.session() as s:
        q = select(CnsFail).where(CnsFail.id > after).order_by(CnsFail.id).limit(limit)
        if status:
            q = q.where(CnsFail.status == status.upper())
        return [{**fail_json(f), "seq": f.id} for f in (await s.execute(q)).scalars()]


@app.post("/v1/fails/{fail_id}/deliver", tags=["platform"], dependencies=[Depends(ctx.require("decisions:write"))])
async def deliver_fail(fail_id: str, body: DeliverIn) -> dict:
    """Deliver shares against a fail (in CNS, the night cycle takes them from our account). Closed when none is
    left outstanding."""
    async with ctx.session() as s:
        f = (await s.execute(select(CnsFail).where(CnsFail.fail_id == fail_id))).scalar()
        if f is None:
            raise HTTPException(404, f"no fail {fail_id}")
        if f.status != "OPEN" or body.quantity > f.outstanding:
            raise HTTPException(409, f"{fail_id} has {f.outstanding:,} outstanding ({f.status})")
        f.outstanding -= body.quantity
        f.deliveries = [*(f.deliveries or []), {"ts": clock.iso(clock.now()), "quantity": body.quantity,
                                                "ref": body.ref}]
        if f.outstanding == 0:
            f.status, f.closed_at = "CLOSED", clock.now()
        await ctx.events.emit(s, "fail_delivered", "fail", f.fail_id, fail_json(f), topic="inbox")
        await s.commit()
        return fail_json(f)


@ctx.verb("fail")
async def verb_fail(args: dict[str, Any]) -> dict:
    """A client's short fails to deliver at CNS: {counterparty?, instrument?, quantity?, date? (today)}. Without a
    name, the largest short in the latest stock record."""
    async with ctx.session() as s:
        q = select(ShortPosition).where(ShortPosition.short_qty > 0).order_by(
            ShortPosition.trade_date.desc(), ShortPosition.short_qty.desc(), ShortPosition.id)
        if args.get("counterparty"):
            q = q.where(ShortPosition.cp_code == str(args["counterparty"]).upper())
        if args.get("instrument"):
            inst = resolve(str(args["instrument"]))
            q = q.where(ShortPosition.instrument_id == (inst.id if inst else "?"))
        p = (await s.execute(q.limit(1))).scalar()
        if p is None:
            raise HTTPException(404, "no client short matches")
        d = date.fromisoformat(args["date"]) if args.get("date") else calendar.trade_date(clock.now())
        f = await _add_fail(s, d, p.cp_code, p.instrument_id, min(int(args.get("quantity") or p.short_qty),
                                                                     p.short_qty), at=clock.now())
        await s.commit()
        return fail_json(f)


@app.get("/v1/buy-ins", tags=["platform"], dependencies=[Depends(ctx.require("inbox:read"))])
async def buy_ins(after: int = 0, limit: int = Query(500, le=5000)) -> list[dict]:
    """CNS buy-ins on fails we didn't deliver, oldest first (``after``: the last ``seq`` you've seen)."""
    async with ctx.session() as s:
        q = select(CnsBuyIn).where(CnsBuyIn.id > after).order_by(CnsBuyIn.id).limit(limit)
        return [{**buy_in_json(b), "seq": b.id} for b in (await s.execute(q)).scalars()]


class ChargeIn(BaseModel):
    counterparty: str
    amount: float = Field(..., gt=0)
    ccy: str = Field("USD", min_length=3, max_length=3)
    reason: str = Field(..., max_length=500)
    ref: str = Field(..., max_length=64, description="Your reference; the same ref returns the same charge")


def charge_json(c: ClientCharge) -> dict[str, Any]:
    return {"chargeId": c.charge_id, "ref": c.ref, "counterparty": c.cp_code, "amount": c.amount, "ccy": c.ccy,
            "reason": c.reason, "postedAt": clock.iso(c.ts)}


@app.post("/v1/charges", tags=["platform"], dependencies=[Depends(ctx.require("decisions:write"))])
async def post_charge(body: ChargeIn) -> dict:
    """Post a charge to a client's account. Idempotent on ``ref``."""
    if counterparty_by_code(body.counterparty.upper()) is None:
        raise HTTPException(404, f"no counterparty {body.counterparty}")
    async with ctx.session() as s:
        have = (await s.execute(select(ClientCharge).where(ClientCharge.ref == body.ref))).scalar()
        if have is not None:
            return {**charge_json(have), "duplicate": True}
        c = ClientCharge(charge_id="pending", ref=body.ref, cp_code=body.counterparty.upper(),
                         amount=round(body.amount, 2), ccy=body.ccy.upper(), reason=body.reason, ts=clock.now())
        s.add(c)
        await s.flush()
        c.charge_id = f"GW-C-{c.id:06d}"
        await ctx.events.emit(s, "client_charged", "charge", c.charge_id, charge_json(c), topic="inbox")
        await s.commit()
        return charge_json(c)


@app.get("/v1/charges", tags=["platform"], dependencies=[Depends(ctx.require("inbox:read"))])
async def charges(counterparty: str | None = None) -> list[dict]:
    async with ctx.session() as s:
        q = select(ClientCharge).order_by(ClientCharge.id)
        if counterparty:
            q = q.where(ClientCharge.cp_code == counterparty.upper())
        return [charge_json(c) for c in (await s.execute(q)).scalars()]


@app.get("/v1/my/charges", tags=["counterparty"])
async def my_charges(cp: Counterparty = Depends(counterparty("locates:read"))) -> list[dict]:
    """What the prime broker has charged your account."""
    async with ctx.session() as s:
        return [charge_json(c) for c in (await s.execute(
            select(ClientCharge).where(ClientCharge.cp_code == cp.code).order_by(ClientCharge.id))).scalars()]


@ctx.verb("cns_buy_in")
async def verb_cns_buy_in(args: dict[str, Any]) -> dict:
    """The buyer buys in a fail now: {failId?, quantity?, price?}. Default the oldest open fail."""
    async with ctx.session() as s:
        q = select(CnsFail).where(CnsFail.status == "OPEN").order_by(CnsFail.id)
        if args.get("failId"):
            q = q.where(CnsFail.fail_id == args["failId"])
        f = (await s.execute(q.limit(1))).scalar()
        if f is None:
            raise HTTPException(404, "no open fail matches")
        b = await _buy_in(s, f, int(args["quantity"]) if args.get("quantity") else None,
                          float(args["price"]) if args.get("price") else None)
        await s.commit()
        return buy_in_json(b)


@ctx.verb("cns_buy_in_after")
async def verb_cns_buy_in_after(args: dict[str, Any]) -> dict:
    """How many business days a fail stays open before the buyer buys in: {days} (default 3; null never)."""
    days = args.get("days", BUY_IN_AFTER_DAYS)
    _fails["buyInAfterDays"] = None if days is None else max(int(days), 0)
    return {"buyInAfterDays": _fails["buyInAfterDays"]}


@ctx.verb("fail_rate")
async def verb_fail_rate(args: dict[str, Any]) -> dict:
    """How often a new US short settling at a close fails at CNS: {rate} (0 to 1; default 0.03)."""
    _fails["rate"] = max(0.0, min(float(args.get("rate", FAIL_RATE)), 1.0))
    return {"rate": _fails["rate"]}


# -- simulated counterparty receivers -----------------------------------------------------------------
@app.post("/cp/{code}/callback", tags=["simulated counterparties"])
async def cp_callback(code: str, request: Request) -> dict:
    cp = counterparty_by_code(code)
    if cp is None:
        raise HTTPException(404, "unknown counterparty")
    raw = await request.body()
    ok = auth.verify(webhook_secret(cp.code), raw, request.headers.get(auth.SIGNATURE_HEADER, ""), tolerance_s=0)
    async with ctx.session() as s:
        s.add(CpReceived(cp_code=cp.code, signature_ok=ok, body=json.loads(raw or b"{}"), ts=clock.now()))
        await s.commit()
    if not ok:
        raise HTTPException(401, "bad signature")
    return {"ok": True}


@app.get("/cp/{code}/received", tags=["simulated counterparties"])
async def cp_received(code: str) -> list[dict]:
    async with ctx.session() as s:
        rows = (await s.execute(select(CpReceived).where(CpReceived.cp_code == code.upper())
                                .order_by(CpReceived.id))).scalars()
        return [{"signatureOk": r.signature_ok, "body": r.body, "ts": clock.iso(r.ts)} for r in rows]


# -- load generator -----------------------------------------------------------------------------------
_load: dict[str, Any] = {}


def _load_defaults() -> dict[str, Any]:
    return {"on": False, "base_per_min": 2.0, "burst_mult": 8.0, "burst_start": "09:00", "burst_end": "09:45",
            "median_qty": 50_000, "tier_mix": {"GC": 0.7, "WARM": 0.2, "HOT": 0.1}, "generated": 0,
            # We're a prime broker: our clients, the funds, are who ask us for locates.
            "counterparties": [c.code for c in counterparties() if c.kind == "HEDGE_FUND"]}


def _in_burst(ts: datetime) -> bool:
    local = ts.astimezone(clock.NEW_YORK).time()
    a, b = (dtime(*map(int, _load[k].split(":"))) for k in ("burst_start", "burst_end"))
    return a <= local < b


_FX = {"USD": 1.0, "GBP": 1.27, "EUR": 1.08, "JPY": 0.0067}  # rough USD rates, as the desk uses


def _gen_lines(r, n: int, tier_counts: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """``n`` lines drawn by the load's tier mix, or exactly ``tier_counts`` per tier (shuffled)."""
    mix = _load["tier_mix"]
    pools: dict[str, list] = {}
    for i in universe():
        # Exact counts are a scripted "34 ETB names": no threshold-list names, and no penny stocks a desk's
        # under-$1 filter would decline.
        scripted_out = tier_counts and (i.tier == "GC" and i.threshold_flag or i.base_px * _FX.get(i.ccy, 1.0) < 1)
        if not i.restricted and not scripted_out:
            pools.setdefault(i.tier, []).append(i)
    tiers = [t for t, k in (tier_counts or {}).items() for _ in range(k)]
    r.shuffle(tiers)
    out = []
    for k in range(len(tiers) or n):
        tier = tiers[k] if tiers else r.choices(list(mix), list(mix.values()))[0]
        inst = r.choice(pools[tier])
        style = r.choice(("isin", "isin", "sedol", "cusip", "ticker"))
        ident = {"isin": inst.isin, "sedol": inst.sedol, "cusip": inst.cusip or inst.isin,
                 "ticker": inst.composite}[style]
        qty = r.lognormvariate(math.log(_load["median_qty"]), 1.0)
        qty = max(int(round(qty / (5000 if qty > 50_000 else 1000))) * (5000 if qty > 50_000 else 1000), 1000)
        line = {"identifier": ident, "idType": ids.detect(ident) or "TICKER", "quantity": qty}
        if not tier_counts and inst.tier != "GC" and r.random() < 0.4:  # a fund's limit on a harder name
            line["maxFeeBps"] = round(market.fee_bps(inst, clock.now()) * r.uniform(0.85, 1.4), 1)
        out.append(line)
    return out


async def _arrival() -> None:
    if not _load.get("on"):
        return
    r = rng.stream("gateway", "loadgen")
    # Re-arm first so one failed arrival doesn't end the stream.
    rate = _load["base_per_min"] * (_load["burst_mult"] if _in_burst(clock.now()) else 1.0)
    ctx.scheduler.after(r.expovariate(rate / 60.0), "loadgen", _arrival)
    cp = counterparty_by_code(r.choice(_load["counterparties"]))
    n = r.choices((1, r.randint(2, 5), r.randint(10, 50)), (0.4, 0.4, 0.2))[0]
    lines = _gen_lines(r, n)
    _load["generated"] += 1
    await create_locate(cp, "FLAT", f"{cp.code}-{clock.now():%H%M%S}-{_load['generated']}", None, None, lines, None,
                        hashlib.sha256(json.dumps(lines).encode()).hexdigest(), source="LOADGEN")


class LoadgenIn(BaseModel):
    on: bool = True
    base_per_min: float | None = Field(None, gt=0)
    burst_mult: float | None = Field(None, ge=1)
    burst_start: str | None = None
    burst_end: str | None = None
    median_qty: int | None = Field(None, gt=0)
    tier_mix: dict[str, float] | None = None
    counterparties: list[str] | None = None


@app.put("/_admin/loadgen", tags=["admin"])
async def loadgen(cfg: LoadgenIn) -> dict:
    _load.update({k: v for k, v in cfg.model_dump().items() if v is not None})
    ctx.scheduler.cancel("loadgen")
    if _load["on"]:
        r = rng.stream("gateway", "loadgen")
        ctx.scheduler.after(r.expovariate(_load["base_per_min"] / 60.0), "loadgen", _arrival)
    return dict(_load)


@app.get("/_admin/loadgen", tags=["admin"])
async def get_loadgen() -> dict:
    return dict(_load)


@ctx.verb("loadgen")
async def verb_loadgen(args: dict[str, Any]) -> dict:
    """Start/stop Poisson locate traffic: {on, base_per_min, burst_mult, burst_start, burst_end, ...}"""
    return await loadgen(LoadgenIn(**args))


@ctx.verb("burst")
async def verb_burst(args: dict[str, Any]) -> dict:
    """One API batch from a counterparty right now: {counterparty, lines, tierMix?, tierCounts?}"""
    cp = counterparty_by_code(args.get("counterparty", "KESTRELHF"))
    if cp is None:
        raise HTTPException(404, "unknown counterparty")
    if args.get("tierMix"):
        _load["tier_mix"] = args["tierMix"]
    r = rng.stream("gateway", "burst")
    lines = _gen_lines(r, int(args.get("lines", 25)), args.get("tierCounts"))
    _, body = await create_locate(cp, "FLAT", args.get("requestId") or f"{cp.code}-BATCH-{clock.now():%H%M%S}",
                                  None, None, lines, None, hashlib.sha256(json.dumps(lines).encode()).hexdigest())
    return body


@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {
        "platformToken": ctx.gate.bootstrap_key(PLATFORM),
        "downstreamUrl": DOWNSTREAM or None,
        "downstreamToken": downstream_token(),
        "counterparties": [{"code": c.code, "apiKey": ctx.gate.bootstrap_key(c.code),
                            "webhookSecret": webhook_secret(c.code)} for c in counterparties()
                           if c.kind != "INTERNAL"],
    }


@app.put("/_admin/limits", tags=["admin"])
async def set_limits(rate_per_s: float = Query(..., gt=0), burst: float = Query(..., ge=1)) -> dict:
    """Shorthand for ``PUT /_admin/plans/standard``: the plan every counterparty key starts on."""
    std = DEFAULT_PLANS["standard"]
    ctx.gate.set_plan(Plan("standard", rate_per_s, burst, std.daily_quota, std.description))
    return {"rate_per_s": rate_per_s, "burst": burst}


async def on_reset(c: ServiceContext) -> None:
    _fails.update(rate=FAIL_RATE, buyInAfterDays=BUY_IN_AFTER_DAYS)
    _load.clear()
    _load.update(_load_defaults())
    ctx.gate.reset_plans()


spec.on_reset = on_reset
_load.update(_load_defaults())
