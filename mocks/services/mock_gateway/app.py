"""mock-gateway: the "smart API" counterparties use to send us locates and orders.

Counterparty side (bearer API key per counterparty):
  ``POST /v1/locates`` single or batch (<= 5,000 lines) as CDM-shaped JSON, flat JSON or CSV,
  with ``Idempotency-Key`` and per-counterparty rate limits; ``GET /v1/locates/{id}``;
  ``POST /v1/orders``.
Our platform's side (platform token):
  ``GET /v1/inbox`` / WS ``/v1/inbox/stream`` to pull new requests (or set a downstream URL to
  have them pushed), and ``POST /v1/decisions`` to answer; decisions go back to the
  counterparty's callback URL as HMAC-signed webhooks with retries.
Simulated counterparties: ``/cp/{code}/callback`` receivers record what they get, and a load
generator produces Poisson arrivals with a 09:00-09:45 New York burst.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import time
from datetime import date, datetime
from datetime import time as dtime
from typing import Any, Literal

from fastapi import Depends, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from mockcore import auth, clock, rng
from mockcore.app import ServiceContext, ServiceSpec, create_app
from refdata import calendar
from refdata import identifiers as ids
from refdata.counterparties import Counterparty, counterparties, counterparty_by_api_key, counterparty_by_code
from refdata.universe import resolve, universe

from .models import Base, CpOrder, CpReceived, Delivery, LocateLine, LocateRequest

MAX_LINES = 5000
DOWNSTREAM = os.environ.get("MOCK_GATEWAY_DOWNSTREAM_URL", "")

spec = ServiceSpec(
    name="mock-gateway",
    label="Counterparty Gateway",
    schema="gateway",
    base=Base,
    description="Counterparty-facing locate/order API with idempotency, rate limits, signed webhooks and a load generator.",
)
ctx = ServiceContext(spec)
app = create_app(ctx)


def platform_token() -> str:
    return auth.derive_secret(rng.get_seed(), "gateway-platform")[:32]


# -- auth & rate limits ------------------------------------------------------------------------------
class _Bucket:
    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst, self.tokens, self.ts = rate, burst, burst, time.monotonic()

    def take(self) -> float:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
        self.ts = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


_limits: dict[str, Any] = {"rate_per_s": 20.0, "burst": 40.0}
_buckets: dict[str, _Bucket] = {}


async def counterparty(authorization: str | None = Header(default=None)) -> Counterparty:
    key = (authorization or "").removeprefix("Bearer ").strip()
    cp = counterparty_by_api_key(key) if key else None
    if cp is None:
        raise HTTPException(401, "missing or bad API key", headers={"WWW-Authenticate": "Bearer"})
    b = _buckets.setdefault(cp.code, _Bucket(_limits["rate_per_s"], _limits["burst"]))
    wait = b.take()
    if wait:
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": f"{math.ceil(wait)}"})
    return cp


async def platform(authorization: str | None = Header(default=None)) -> None:
    if authorization != f"Bearer {platform_token()}":
        raise HTTPException(401, "missing or bad platform token")


# -- normalisation ---------------------------------------------------------------------------------
def _norm_lines_cdm(doc: dict[str, Any]) -> tuple[str | None, date | None, str | None, list[dict[str, Any]]]:
    b = doc["borrowerLocate"]
    lines = [{"identifier": ln["security"]["identifier"], "idType": ln["security"].get("identifierType"),
              "quantity": ln["quantity"]["amount"]} for ln in b.get("locateLines", [])]
    sd = b.get("settlementDate")
    return b.get("locateRequestId"), date.fromisoformat(sd) if sd else None, b.get("callbackUrl"), lines


def _norm_lines_flat(doc: dict[str, Any]) -> tuple[str | None, date | None, str | None, list[dict[str, Any]]]:
    if "lines" not in doc and "identifier" in doc:  # single-line shorthand
        doc = {**doc, "lines": [{k: doc[k] for k in ("identifier", "idType", "quantity") if k in doc}]}
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
    out = []
    for row in reader:
        idt = (row.get(type_col) if type_col else None) or (id_col.upper() if id_col.lower() != "identifier" else None)
        out.append({"identifier": (row[id_col] or "").strip(), "idType": idt,
                    "quantity": (row[qty_col] or "0").replace(",", "").strip()})
    return out


def _resolve_line(raw: dict[str, Any]) -> dict[str, Any]:
    ident = str(raw.get("identifier", "")).strip()
    try:
        qty = int(float(raw.get("quantity", 0)))
    except (TypeError, ValueError):
        qty = 0
    out: dict[str, Any] = {"identifier": ident, "idType": raw.get("idType"), "quantity": qty}
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
            "instrumentId": ln.instrument_id, "isin": ln.isin, "quantity": ln.quantity, "status": ln.status,
            "approvedQuantity": ln.approved_qty, "indicativeFeeBps": ln.fee_bps, "rationale": ln.rationale,
            "regShoBasis": ln.reg_sho_basis, "expiresAt": clock.iso(ln.expires_at) if ln.expires_at else None,
            "decidedAt": clock.iso(ln.decided_at) if ln.decided_at else None, "error": ln.error}


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
                                   tier=r.get("tier"), quantity=r["quantity"], status=r["status"],
                                   error=r.get("error")))
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
                                    headers={"X-Gateway-Token": platform_token()})
            ok = r.status_code < 300
        except Exception:
            ok = False
        req.forward_status = "FORWARDED" if ok else ("FAILED" if attempt >= 5 else "PENDING")
        await s.commit()
    if not ok and attempt < 5:
        ctx.scheduler.after(2**attempt, f"forward:{locate_id}", forward_job, locate_id, attempt + 1)


# -- counterparty API -------------------------------------------------------------------------------
@app.post("/v1/locates", tags=["counterparty"], status_code=202)
async def post_locates(request: Request, cp: Counterparty = Depends(counterparty),
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
async def get_locate(locate_id: str, cp: Counterparty = Depends(counterparty)) -> dict:
    async with ctx.session() as s:
        req = (await s.execute(select(LocateRequest).where(LocateRequest.locate_id == locate_id,
                                                           LocateRequest.cp_code == cp.code))).scalar()
        if req is None:
            raise HTTPException(404, "no such locate")
        lines = (await s.execute(select(LocateLine).where(LocateLine.locate_id == locate_id)
                                 .order_by(LocateLine.line_no))).scalars().all()
        return request_json(req, list(lines))


@app.get("/v1/locates", tags=["counterparty"])
async def my_locates(limit: int = Query(100, le=1000), cp: Counterparty = Depends(counterparty)) -> list[dict]:
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
async def post_order(body: OrderIn, cp: Counterparty = Depends(counterparty),
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
@app.get("/v1/inbox", tags=["platform"], dependencies=[Depends(platform)])
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
async def inbox_stream(ws: WebSocket, token: str = Query("")) -> None:
    if token != platform_token():
        await ws.close(code=4401)
        return
    await ctx.ws_stream(ws, ["inbox", "decisions"])


class LineDecision(BaseModel):
    lineNo: int
    decision: Literal["APPROVED", "PARTIAL", "REJECTED"]
    approvedQuantity: int = Field(0, ge=0)
    indicativeFeeBps: float | None = None
    rationale: str | None = None
    regShoBasis: str | None = None
    expiresAt: datetime | None = None


class DecisionIn(BaseModel):
    locateId: str
    lines: list[LineDecision] = Field(..., min_length=1)


@app.post("/v1/decisions", tags=["platform"], dependencies=[Depends(platform)])
async def decide(body: DecisionIn) -> dict:
    async with ctx.session() as s:
        req = (await s.execute(select(LocateRequest).where(LocateRequest.locate_id == body.locateId)
                               .with_for_update())).scalar()
        if req is None:
            raise HTTPException(404, "no such locate")
        lines = {x.line_no: x for x in (await s.execute(select(LocateLine).where(
            LocateLine.locate_id == body.locateId))).scalars()}
        now = clock.now()
        eod = datetime.combine(now.astimezone(clock.NEW_YORK).date(), dtime(23, 59), tzinfo=clock.NEW_YORK)
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
            ln.fee_bps, ln.rationale, ln.reg_sho_basis = d.indicativeFeeBps, d.rationale, d.regShoBasis
            ln.expires_at, ln.decided_at = d.expiresAt or eod, now
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
            r = await ctx.http.post(url, content=b, headers={"content-type": "application/json",
                                                             auth.SIGNATURE_HEADER: auth.sign(cp.webhook_secret, b),
                                                             "X-Mock-Delivery-Attempt": str(attempt)})
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


@app.get("/v1/deliveries", tags=["platform"], dependencies=[Depends(platform)])
async def deliveries(locateId: str | None = None) -> list[dict]:
    async with ctx.session() as s:
        q = select(Delivery).order_by(Delivery.id)
        if locateId:
            q = q.where(Delivery.locate_id == locateId)
        return [{"locateId": d.locate_id, "url": d.url, "attempt": d.attempt, "statusCode": d.status_code,
                 "ok": d.ok, "error": d.error, "ts": clock.iso(d.ts)} for d in (await s.execute(q)).scalars()]


# -- simulated counterparty receivers -----------------------------------------------------------------
@app.post("/cp/{code}/callback", tags=["simulated counterparties"])
async def cp_callback(code: str, request: Request) -> dict:
    cp = counterparty_by_code(code)
    if cp is None:
        raise HTTPException(404, "unknown counterparty")
    raw = await request.body()
    ok = auth.verify(cp.webhook_secret, raw, request.headers.get(auth.SIGNATURE_HEADER, ""), tolerance_s=0)
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
            "counterparties": [c.code for c in counterparties() if c.kind in ("PRIME_BROKER", "HEDGE_FUND")]}


def _in_burst(ts: datetime) -> bool:
    local = ts.astimezone(clock.NEW_YORK).time()
    a, b = (dtime(*map(int, _load[k].split(":"))) for k in ("burst_start", "burst_end"))
    return a <= local < b


def _gen_lines(r, n: int) -> list[dict[str, Any]]:
    mix = _load["tier_mix"]
    pools: dict[str, list] = {}
    for i in universe():
        if not i.restricted:
            pools.setdefault(i.tier, []).append(i)
    out = []
    for _ in range(n):
        tier = r.choices(list(mix), list(mix.values()))[0]
        inst = r.choice(pools[tier])
        style = r.choice(("isin", "isin", "sedol", "cusip", "ticker"))
        ident = {"isin": inst.isin, "sedol": inst.sedol, "cusip": inst.cusip or inst.isin,
                 "ticker": inst.composite}[style]
        qty = r.lognormvariate(math.log(_load["median_qty"]), 1.0)
        qty = max(int(round(qty / (5000 if qty > 50_000 else 1000))) * (5000 if qty > 50_000 else 1000), 1000)
        out.append({"identifier": ident, "idType": ids.detect(ident) or "TICKER", "quantity": qty})
    return out


async def _arrival() -> None:
    if not _load.get("on"):
        return
    r = rng.stream("gateway", "loadgen")
    cp = counterparty_by_code(r.choice(_load["counterparties"]))
    n = r.choices((1, r.randint(2, 5), r.randint(10, 50)), (0.4, 0.4, 0.2))[0]
    lines = _gen_lines(r, n)
    _load["generated"] += 1
    await create_locate(cp, "FLAT", f"{cp.code}-{clock.now():%H%M%S}-{_load['generated']}", None, None, lines, None,
                        hashlib.sha256(json.dumps(lines).encode()).hexdigest(), source="LOADGEN")
    rate = _load["base_per_min"] * (_load["burst_mult"] if _in_burst(clock.now()) else 1.0)
    ctx.scheduler.after(r.expovariate(rate / 60.0), "loadgen", _arrival)


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
    """One API batch from a counterparty right now: {counterparty, lines, tierMix?}"""
    cp = counterparty_by_code(args.get("counterparty", "KESTRELHF"))
    if cp is None:
        raise HTTPException(404, "unknown counterparty")
    if args.get("tierMix"):
        _load["tier_mix"] = args["tierMix"]
    r = rng.stream("gateway", "burst")
    lines = _gen_lines(r, int(args.get("lines", 25)))
    _, body = await create_locate(cp, "FLAT", args.get("requestId") or f"{cp.code}-BATCH-{clock.now():%H%M%S}",
                                  None, None, lines, None, hashlib.sha256(json.dumps(lines).encode()).hexdigest())
    return body


@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {
        "platformToken": platform_token(),
        "downstreamUrl": DOWNSTREAM or None,
        "counterparties": [{"code": c.code, "apiKey": c.api_key, "webhookSecret": c.webhook_secret}
                           for c in counterparties()],
    }


@app.put("/_admin/limits", tags=["admin"])
async def set_limits(rate_per_s: float = Query(..., gt=0), burst: float = Query(..., ge=1)) -> dict:
    _limits.update(rate_per_s=rate_per_s, burst=burst)
    _buckets.clear()
    return dict(_limits)


async def on_reset(c: ServiceContext) -> None:
    _load.clear()
    _load.update(_load_defaults())
    _buckets.clear()
    _limits.update(rate_per_s=20.0, burst=40.0)


spec.on_reset = on_reset
_load.update(_load_defaults())
