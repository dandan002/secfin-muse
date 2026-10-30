"""mock-custodian: settles securities-lending legs from ISO 20022 sese.023 instructions.

Flow per instruction: schema validation -> sese.024 PACK (or RJCT with the failing field) ->
matching against the simulated counterparty's instruction (sese.024 MACH, or NMAT for a
seeded few) -> settlement on the settlement date at 10:00 New York: sese.025 confirmation,
or sese.024 pending/failing (LACK of securities, retried next business day, up to 3 tries).

Cash payments (``POST /payments``): a free payment to, or an expected receipt from, a counterparty (a buy-in's
cost, say), settled on its value date at the settlement time.

A sese.020 cancellation request is answered with a sese.027 (accepted / rejected) and, when
accepted, a sese.024 CAND. Outbound messages go to an outbox (``GET /iso/outbox``) and the
``/stream`` WebSocket. Full reference: ../API.md.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Date, DateTime, Identity, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from mockcore import clock, rng
from mockcore.app import ServiceContext, ServiceSpec, create_app
from mockcore.db import make_base
from mockcore.keys import AuthSpec, Grant, Principal
from refdata import calendar
from refdata.counterparties import OUR_FIRM
from refdata.universe import resolve

from . import iso

Base = make_base("custodian")


class Instr(Base):
    __tablename__ = "instructions"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instruction_id: Mapped[str] = mapped_column(String(32), unique=True)
    tx_id: Mapped[str] = mapped_column(String(35), unique=True)
    biz_msg_id: Mapped[str] = mapped_column(String(35))
    sender_bic: Mapped[str] = mapped_column(String(11))
    movement: Mapped[str] = mapped_column(String(4))
    payment: Mapped[str] = mapped_column(String(4))
    common_id: Mapped[str | None] = mapped_column(String(35))
    tx_type: Mapped[str] = mapped_column(String(4))
    isin: Mapped[str] = mapped_column(String(12))
    instrument_id: Mapped[str | None] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(BigInteger)
    account: Mapped[str] = mapped_column(String(35))
    trade_date: Mapped[date] = mapped_column(Date)
    settle_date: Mapped[date] = mapped_column(Date)
    delivering_bic: Mapped[str] = mapped_column(String(11))
    receiving_bic: Mapped[str] = mapped_column(String(11))
    amount: Mapped[float | None]
    ccy: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16))  # ACCEPTED|MATCHED|UNMATCHED|FAILING|SETTLED|CANCELLED
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[Any]] = mapped_column(default=list)


class Payment(Base):
    """A cash movement on our account, not against securities: we pay a counterparty, or expect it to pay us."""

    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(32), unique=True)
    tx_id: Mapped[str] = mapped_column(String(35), unique=True)
    direction: Mapped[str] = mapped_column(String(8))  # PAY | RECEIVE
    amount: Mapped[float]
    ccy: Mapped[str] = mapped_column(String(3))
    counterparty_bic: Mapped[str] = mapped_column(String(11))
    value_date: Mapped[date] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column(String(140))
    status: Mapped[str] = mapped_column(String(16))  # ACCEPTED | SETTLED | CANCELLED
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[Any]] = mapped_column(default=list)


class OutMsg(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instruction_id: Mapped[str | None] = mapped_column(String(32), index=True)
    msg_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(8))
    xml: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


SCOPES = {
    "instructions:write": "Send settlement instructions (sese.023 XML or JSON) and cancel them",
    "instructions:read": "Instruction status",
    "outbox:read": "Outbound sese.024/sese.025 messages (REST and stream)",
    "payments:write": "Send cash payments and expected receipts",
    "payments:read": "Payment status",
}
spec = ServiceSpec(
    name="mock-custodian",
    label="Custodian",
    schema="custodian",
    base=Base,
    description="Simulated custodian: ISO 20022 sese.023 in; sese.024 status and sese.025 confirmations out.",
    auth=AuthSpec(SCOPES, lambda: [Grant(OUR_FIRM["bic"], tuple(SCOPES), "partner", f"{OUR_FIRM['name']} (client)")]),
)
ctx = ServiceContext(spec)
app = create_app(ctx)
_cfg: dict[str, Any] = {"fail_rate": 0.05, "unmatch_rate": 0.02, "max_attempts": 3, "fail_next": False,
                        "match_latency_s": (2.0, 10.0), "settle_time_local": "10:00"}


def client(scope: str):  # noqa: ANN201 - a FastAPI dependency
    """The calling firm (the key's principal), entitled to ``scope`` (see mockcore.keys)."""
    gate = ctx.require(scope)

    async def dep(p: Principal = Depends(gate)) -> str:
        return p.principal

    return dep


def instr_json(i: Instr) -> dict[str, Any]:
    return {"instructionId": i.instruction_id, "txId": i.tx_id, "bizMsgId": i.biz_msg_id, "movement": i.movement,
            "payment": i.payment, "commonId": i.common_id, "txType": i.tx_type, "isin": i.isin,
            "instrumentId": i.instrument_id, "quantity": i.quantity, "account": i.account,
            "tradeDate": i.trade_date.isoformat(), "settlementDate": i.settle_date.isoformat(),
            "deliveringBic": i.delivering_bic, "receivingBic": i.receiving_bic, "amount": i.amount, "ccy": i.ccy,
            "status": i.status, "attempts": i.attempts, "receivedAt": clock.iso(i.received_at), "history": i.history}


def _hist(i: Instr, event: str, **data: Any) -> None:
    i.history = [*i.history, {"ts": clock.iso(clock.now()), "event": event, **data}]


async def _emit_out(s, i: Instr | None, msg_type: str, status: str, xml: bytes, to_bic: str) -> dict[str, Any]:
    m = OutMsg(instruction_id=i.instruction_id if i else None, msg_type=msg_type, status=status, xml=xml.decode(),
               ts=clock.now())
    s.add(m)
    await s.flush()
    payload = {"outId": m.id, "msgType": msg_type, "status": status, "instructionId": m.instruction_id,
               "txId": i.tx_id if i else None, "to": to_bic, "xml": m.xml,
               "instruction": instr_json(i) if i else None}
    await ctx.events.emit(s, f"{msg_type}_{status.lower()}", "message", str(m.id), payload, topic="outbox")
    return payload


def _settle_at(d: date) -> datetime:
    hh, mm = (int(x) for x in _cfg["settle_time_local"].split(":"))
    return datetime.combine(d, time(hh, mm), tzinfo=clock.NEW_YORK)


async def _accept(s, ins: iso.Instruction, raw: bytes) -> tuple[Instr, dict[str, Any]]:
    dup = (await s.execute(select(Instr).where(Instr.tx_id == ins.tx_id))).scalar()
    if dup:
        raise HTTPException(409, f"duplicate TxId {ins.tx_id} ({dup.instruction_id})")
    inst = resolve(ins.isin)
    i = Instr(instruction_id="pending", tx_id=ins.tx_id, biz_msg_id=ins.biz_msg_id, sender_bic=ins.sender_bic,
              movement=ins.movement, payment=ins.payment, common_id=ins.common_id, tx_type=ins.tx_type,
              isin=ins.isin, instrument_id=inst.id if inst else None, quantity=ins.quantity, account=ins.account,
              trade_date=ins.trade_date, settle_date=ins.settle_date, delivering_bic=ins.delivering_bic,
              receiving_bic=ins.receiving_bic, amount=ins.amount, ccy=ins.ccy, status="ACCEPTED",
              raw=raw.decode(), received_at=clock.now(), history=[])
    s.add(i)
    await s.flush()
    i.instruction_id = f"CUS-{i.id:08d}"
    if inst is None:
        i.status = "REJECTED"
        _hist(i, "REJECTED", reason="ISIN unknown to custodian")
        out = await _emit_out(s, i, "sese.024", "RJCT",
                              iso.build_sese024(ins.sender_bic, f"{i.instruction_id}-R", i.tx_id, i.instruction_id,
                                                "RJCT", "SAFE", "ISIN not held/serviced"), ins.sender_bic)
        return i, out
    _hist(i, "ACCEPTED")
    out = await _emit_out(s, i, "sese.024", "PACK",
                          iso.build_sese024(ins.sender_bic, f"{i.instruction_id}-A", i.tx_id, i.instruction_id,
                                            "PACK"), ins.sender_bic)
    ctx.scheduler.after(rng.stream("custodian", "latency").uniform(*_cfg["match_latency_s"]),
                        f"match:{i.instruction_id}", match_job, i.instruction_id)
    return i, out


async def _get(s, ref: str) -> Instr:
    i = (await s.execute(select(Instr).where((Instr.instruction_id == ref) | (Instr.tx_id == ref))
                         .with_for_update())).scalar()
    if i is None:
        raise HTTPException(404, f"no instruction {ref}")
    return i


async def match_job(instruction_id: str) -> None:
    async with ctx.session() as s:
        i = await _get(s, instruction_id)
        if i.status != "ACCEPTED":
            return
        r = rng.stream("custodian", "match")
        if r.random() < _cfg["unmatch_rate"]:
            i.status = "UNMATCHED"
            _hist(i, "UNMATCHED", reason="DDAT")
            await _emit_out(s, i, "sese.024", "NMAT", iso.build_sese024(
                i.sender_bic, f"{i.instruction_id}-M", i.tx_id, i.instruction_id, "NMAT", "DDAT",
                "counterparty instruction has a different settlement date"), i.sender_bic)
        else:
            i.status = "MATCHED"
            _hist(i, "MATCHED")
            await _emit_out(s, i, "sese.024", "MACH", iso.build_sese024(
                i.sender_bic, f"{i.instruction_id}-M", i.tx_id, i.instruction_id, "MACH"), i.sender_bic)
            due = max(_settle_at(i.settle_date), clock.now() + timedelta(seconds=30))
            ctx.scheduler.at(due, f"settle:{i.instruction_id}", settle_job, i.instruction_id)
        await s.commit()


async def settle_job(instruction_id: str) -> None:
    async with ctx.session() as s:
        i = await _get(s, instruction_id)
        if i.status not in ("MATCHED", "FAILING"):
            return
        i.attempts += 1
        r = rng.stream("custodian", "settle")
        fail = _cfg["fail_next"] or r.random() < _cfg["fail_rate"]
        _cfg["fail_next"] = False
        if fail and i.attempts < _cfg["max_attempts"]:
            i.status = "FAILING"
            reason = "LACK" if i.movement == "DELI" else "CLAC"
            detail = "lack of securities" if reason == "LACK" else "counterparty lack of securities"
            _hist(i, "FAILING", reason=reason, attempt=i.attempts)
            await _emit_out(s, i, "sese.024", "PENF", iso.build_sese024(
                i.sender_bic, f"{i.instruction_id}-F{i.attempts}", i.tx_id, i.instruction_id, "PENF", reason, detail),
                i.sender_bic)
            nxt = calendar.add_business_days(clock.now().astimezone(clock.NEW_YORK).date(), 1)
            ctx.scheduler.at(_settle_at(nxt), f"settle:{i.instruction_id}", settle_job, i.instruction_id)
        else:
            i.status = "SETTLED"
            _hist(i, "SETTLED", attempt=i.attempts)
            await _emit_out(s, i, "sese.025", "SETT", iso.build_sese025(
                i.sender_bic, f"{i.instruction_id}-S", instr_json(i), clock.now()), i.sender_bic)
        await s.commit()


# -- endpoints ------------------------------------------------------------------------------------
@app.post("/iso/sese.023", tags=["iso20022"], response_class=Response)
async def receive(request: Request, who: str = Depends(client("instructions:write"))) -> Response:
    raw = await request.body()
    try:
        ins = iso.parse_sese023(raw)
    except iso.ValidationFailure as e:
        xml = iso.build_sese024(OUR_FIRM["bic"], "RJCT-SCHEMA", "UNKNOWN", "NONE", "RJCT", "NARR", e.errors[0])
        async with ctx.session() as s:
            await _emit_out(s, None, "sese.024", "RJCT", xml, OUR_FIRM["bic"])
            await s.commit()
        return Response(xml, status_code=400, media_type="application/xml",
                        headers={"X-Validation-Errors": " | ".join(e.errors)[:1000]})
    async with ctx.session() as s:
        i, out = await _accept(s, ins, raw)
        await s.commit()
    return Response(out["xml"], status_code=202 if i.status != "REJECTED" else 422, media_type="application/xml",
                    headers={"X-Instruction-Id": i.instruction_id})


class InstructionIn(BaseModel):
    """JSON convenience form; the custodian builds the sese.023 itself and runs the same flow."""

    txId: str = Field(..., max_length=35)
    movement: Literal["RECE", "DELI"]
    payment: Literal["FREE", "APMT"] = "FREE"
    commonId: str | None = None
    txType: Literal["SECL", "SECB", "SLRE", "SBRE", "TRAD"]
    instrument: str
    quantity: int = Field(..., gt=0)
    account: str = "LOANDESK-SAFE-01"
    tradeDate: date | None = None
    settlementDate: date
    counterpartyBic: str
    amount: float | None = None
    ccy: str | None = None


@app.post("/instructions", tags=["json"], status_code=202)
async def create_json(body: InstructionIn, who: str = Depends(client("instructions:write"))) -> dict:
    inst = resolve(body.instrument)
    if inst is None:
        raise HTTPException(422, "unknown instrument")
    ours, theirs = OUR_FIRM["bic"], body.counterpartyBic
    ins = iso.Instruction(
        biz_msg_id=f"J-{body.txId}"[:35], sender_bic=ours, tx_id=body.txId, movement=body.movement,
        payment=body.payment, common_id=body.commonId, trade_date=body.tradeDate or calendar.trade_date(clock.now()),
        settle_date=body.settlementDate, isin=inst.isin, quantity=body.quantity, account=body.account,
        tx_type=body.txType, delivering_bic=ours if body.movement == "DELI" else theirs,
        receiving_bic=theirs if body.movement == "DELI" else ours, amount=body.amount, ccy=body.ccy,
        credit_debit=("CRDT" if body.movement == "DELI" else "DBIT") if body.amount else None)
    xml = iso.build_sese023(ins)
    iso.parse_sese023(xml)  # our own builder must produce valid messages
    async with ctx.session() as s:
        i, out = await _accept(s, ins, xml)
        await s.commit()
    return {"instruction": instr_json(i), "sese023": xml.decode(), "ack": out["xml"]}


@app.get("/instructions", tags=["json"])
async def list_instr(status: str | None = None, who: str = Depends(client("instructions:read"))) -> list[dict]:
    async with ctx.session() as s:
        q = select(Instr).order_by(Instr.id)
        if status:
            q = q.where(Instr.status == status.upper())
        return [instr_json(i) for i in (await s.execute(q)).scalars()]


@app.get("/instructions/{ref}", tags=["json"])
async def get_instr(ref: str, who: str = Depends(client("instructions:read"))) -> dict:
    async with ctx.session() as s:
        return instr_json(await _get(s, ref))


async def _cancel(s, i: Instr, request_id: str) -> tuple[bool, dict[str, Any]]:
    """Cancel an instruction: sese.027 (accepted / rejected), then sese.024 CAND if accepted."""
    if i.status in ("SETTLED", "CANCELLED", "REJECTED"):
        out = await _emit_out(s, i, "sese.027", "RJCT", iso.build_sese027(
            i.sender_bic, f"{i.instruction_id}-X", request_id, i.tx_id, i.instruction_id, False, "NARR",
            f"instruction is {i.status}"), i.sender_bic)
        return False, out
    i.status = "CANCELLED"
    _hist(i, "CANCELLED", request=request_id)
    ctx.scheduler.cancel(f"match:{i.instruction_id}")
    ctx.scheduler.cancel(f"settle:{i.instruction_id}")
    out = await _emit_out(s, i, "sese.027", "ACCP", iso.build_sese027(
        i.sender_bic, f"{i.instruction_id}-X", request_id, i.tx_id, i.instruction_id, True), i.sender_bic)
    await _emit_out(s, i, "sese.024", "CAND", iso.build_sese024(
        i.sender_bic, f"{i.instruction_id}-C", i.tx_id, i.instruction_id, "CAND"), i.sender_bic)
    return True, out


@app.post("/iso/sese.020", tags=["iso20022"], response_class=Response)
async def receive_cancellation(request: Request, who: str = Depends(client("instructions:write"))) -> Response:
    """ISO 20022 cancellation request; answered with a sese.027 (and a sese.024 CAND when accepted)."""
    raw = await request.body()
    try:
        req = iso.parse_sese020(raw)
    except iso.ValidationFailure as e:
        xml = iso.build_sese027(OUR_FIRM["bic"], "RJCT-SCHEMA", "UNKNOWN", "UNKNOWN", None, False, "NARR", e.errors[0])
        return Response(xml, status_code=400, media_type="application/xml",
                        headers={"X-Validation-Errors": " | ".join(e.errors)[:1000]})
    async with ctx.session() as s:
        i = (await s.execute(select(Instr).where(Instr.tx_id == req.tx_id).with_for_update())).scalar()
        if i is None:
            xml = iso.build_sese027(req.sender_bic, f"CXL-{req.biz_msg_id}"[:35], req.biz_msg_id, req.tx_id,
                                    req.acct_svcr_tx_id, False, "NARR", f"no instruction with TxId {req.tx_id}")
            return Response(xml, status_code=404, media_type="application/xml")
        ok, out = await _cancel(s, i, req.biz_msg_id)
        await s.commit()
    return Response(out["xml"], status_code=202 if ok else 409, media_type="application/xml",
                    headers={"X-Instruction-Id": i.instruction_id})


@app.post("/instructions/{ref}/cancel", tags=["json"])
async def cancel(ref: str, who: str = Depends(client("instructions:write"))) -> dict:
    """JSON convenience form of a sese.020: same flow and messages."""
    async with ctx.session() as s:
        i = await _get(s, ref)
        ok, out = await _cancel(s, i, f"J-CXL-{i.tx_id}"[:35])
        await s.commit()
        if not ok:
            raise HTTPException(409, f"instruction is {i.status}")
        return instr_json(i)


@app.get("/iso/outbox", tags=["iso20022"])
async def outbox(after: int = 0, limit: int = Query(200, le=5000),
                 who: str = Depends(client("outbox:read"))) -> list[dict]:
    async with ctx.session() as s:
        rows = (await s.execute(select(OutMsg).where(OutMsg.id > after).order_by(OutMsg.id).limit(limit))).scalars()
        return [{"outId": m.id, "msgType": m.msg_type, "status": m.status, "instructionId": m.instruction_id,
                 "ts": clock.iso(m.ts), "xml": m.xml} for m in rows]


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    """Pass the key as ``?key=``."""
    if await ctx.gate.websocket(ws, "outbox:read") is None:
        return
    await ctx.ws_stream(ws, ["outbox"])


# -- cash payments --------------------------------------------------------------------------------
class PaymentIn(BaseModel):
    txId: str = Field(..., max_length=35, description="Your reference; unique (409 on reuse)")
    direction: Literal["PAY", "RECEIVE"]
    amount: float = Field(..., gt=0)
    ccy: str = Field("USD", min_length=3, max_length=3)
    counterpartyBic: str
    valueDate: date | None = Field(None, description="Default the next business day")
    reference: str | None = Field(None, max_length=140)


def payment_json(p: Payment) -> dict[str, Any]:
    return {"paymentId": p.payment_id, "txId": p.tx_id, "direction": p.direction, "amount": p.amount, "ccy": p.ccy,
            "counterpartyBic": p.counterparty_bic, "valueDate": p.value_date.isoformat(), "reference": p.reference,
            "status": p.status, "receivedAt": clock.iso(p.received_at), "history": p.history}


async def pay_job(payment_id: str) -> None:
    async with ctx.session() as s:
        p = (await s.execute(select(Payment).where(Payment.payment_id == payment_id).with_for_update())).scalar()
        if p is None or p.status != "ACCEPTED":
            return
        p.status = "SETTLED"
        p.history = [*p.history, {"ts": clock.iso(clock.now()), "event": "SETTLED"}]
        await ctx.events.emit(s, "payment_settled", "payment", p.payment_id, payment_json(p), topic="outbox")
        await s.commit()


@app.post("/payments", tags=["payments"], status_code=202)
async def create_payment(body: PaymentIn, who: str = Depends(client("payments:write"))) -> dict:
    """Pay a counterparty, or expect it to pay us: settled on the value date at the settlement time."""
    async with ctx.session() as s:
        if (await s.execute(select(Payment).where(Payment.tx_id == body.txId))).scalar():
            raise HTTPException(409, f"duplicate TxId {body.txId}")
        vd = body.valueDate or calendar.add_business_days(calendar.trade_date(clock.now()), 1)
        p = Payment(payment_id="pending", tx_id=body.txId, direction=body.direction, amount=round(body.amount, 2),
                    ccy=body.ccy.upper(), counterparty_bic=body.counterpartyBic, value_date=vd,
                    reference=body.reference, status="ACCEPTED", received_at=clock.now(),
                    history=[{"ts": clock.iso(clock.now()), "event": "ACCEPTED"}])
        s.add(p)
        await s.flush()
        p.payment_id = f"CUS-P-{p.id:06d}"
        await ctx.events.emit(s, "payment_accepted", "payment", p.payment_id, payment_json(p), topic="outbox")
        await s.commit()
    ctx.scheduler.at(max(_settle_at(vd), clock.now() + timedelta(seconds=30)), f"pay:{p.payment_id}", pay_job,
                     p.payment_id)
    return payment_json(p)


@app.get("/payments", tags=["payments"])
async def list_payments(status: str | None = None, who: str = Depends(client("payments:read"))) -> list[dict]:
    async with ctx.session() as s:
        q = select(Payment).order_by(Payment.id)
        if status:
            q = q.where(Payment.status == status.upper())
        return [payment_json(p) for p in (await s.execute(q)).scalars()]


@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {"header": "X-Api-Key", "apiKey": ctx.gate.bootstrap_key(OUR_FIRM["bic"]), "ourBic": OUR_FIRM["bic"],
            "custodianBic": iso.CUSTODIAN_BIC}


@app.put("/_admin/config", tags=["admin"])
async def put_config(cfg: dict[str, Any]) -> dict:
    unknown = set(cfg) - set(_cfg)
    if unknown:
        raise HTTPException(422, f"unknown keys {sorted(unknown)}")
    _cfg.update(cfg)
    return dict(_cfg)


@app.get("/_admin/config", tags=["admin"])
async def get_config() -> dict:
    return dict(_cfg)


@ctx.verb("config")
async def verb_config(args: dict[str, Any]) -> dict:
    """Patch custodian config: {fail_rate, unmatch_rate, fail_next, max_attempts, settle_time_local}"""
    return await put_config(args)


async def on_reset(c: ServiceContext) -> None:
    _cfg.update(fail_rate=0.05, unmatch_rate=0.02, max_attempts=3, fail_next=False, match_latency_s=(2.0, 10.0),
                settle_time_local="10:00")


spec.on_reset = on_reset
