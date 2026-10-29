"""Trading and order routing (spec module 4): build, check, route and send; follow the venue to a fill."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from refdata import calendar
from refdata.counterparties import OUR_FIRM

from .. import clock, events, rules, secmaster
from ..adapters import AdapterError, systems
from ..models import Counterparty, Instrument, Order, Trade
from . import market

OPEN = ("NEW", "SENT", "PENDING", "COUNTERED")
VENUE_STATUS = {"PENDING": "PENDING", "COUNTERED": "COUNTERED", "EXECUTED": "EXECUTED", "CANCELLED": "CANCELLED",
                "EXPIRED": "EXPIRED", "REJECTED": "REJECTED"}


class OrderIn(BaseModel):
    side: Literal["BORROW", "LEND"] = "BORROW"
    instrument: str = Field(..., description="Any identifier the security master resolves")
    quantity: int = Field(..., gt=0)
    feeBps: float = Field(..., ge=0)
    counterparty: str | None = Field(None, description="Our code (or venue code) of the contra; omit for best")
    venue: Literal["AUTO", "VENUE_A"] = "AUTO"
    type: Literal["AUTO", "BID", "OFFER", "FIRM_BID"] = "AUTO"
    collateralType: Literal["CASH", "NONCASH"] | None = None
    collateralCcy: str | None = None
    marginPct: float | None = Field(None, ge=100, le=150)
    term: Literal["OPEN", "TERM"] = "OPEN"
    termEndDate: date | None = None
    settlementDate: date | None = None
    legalAgreement: Literal["MSLA", "GMSLA_2010", "GMSLA_2018"] | None = None
    override: bool = Field(False, description="Send despite non-blocking warnings")


class OrderError(ValueError):
    def __init__(self, message: str, checks: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.checks = checks or []


def order_json(o: Order, inst: Instrument | None = None) -> dict[str, Any]:
    return {"id": o.id, "side": o.side, "type": o.type, "instrumentId": o.instrument_id,
            "composite": inst.composite if inst else None, "quantity": o.qty, "feeBps": o.fee_bps,
            "counterparty": o.cp_code, "venue": o.venue, "venueOrderId": o.venue_order_id,
            "routeReason": o.route_reason, "collateralType": o.collateral_type, "collateralCcy": o.collateral_ccy,
            "marginPct": o.margin_pct, "term": o.term, "termEndDate": o.term_end.isoformat() if o.term_end else None,
            "settlementDate": o.settle_date.isoformat(), "legalAgreement": o.legal_agreement,
            "bookingEntity": o.booking_entity, "status": o.status, "filledQty": o.filled_qty,
            "counterFeeBps": o.counter_fee_bps, "counterQty": o.counter_qty, "rejectReason": o.reject_reason,
            "checks": o.checks, "createdBy": o.created_by, "actionId": o.action_id,
            "createdAt": clock.iso(o.created_at), "updatedAt": clock.iso(o.updated_at)}


async def exposure_usd(s: AsyncSession, cp_code: str) -> float:
    """Booked trades plus outstanding approved locates (see ``services/limits.py``)."""
    from .limits import books

    b = (await books(s, [cp_code])).get(cp_code)
    return b.trades_usd + b.locates_usd if b else 0.0


async def _cp(s: AsyncSession, code: str | None) -> Counterparty | None:
    if not code:
        return None
    code = code.upper()
    cp = await s.get(Counterparty, code)
    if cp is None:
        cp = (await s.execute(select(Counterparty).where((Counterparty.ngt_code == code) |
                                                         (Counterparty.lei == code)))).scalar()
    if cp is None:
        raise OrderError(f"unknown counterparty {code}")
    return cp


async def preview(s: AsyncSession, req: OrderIn) -> dict[str, Any]:
    """Everything the ticket shows before send: resolved name, route, terms and pre-trade checks."""
    res = await secmaster.resolve(s, req.instrument)
    if res.instrument is None:
        raise OrderError(res.error or "unknown instrument")
    inst = res.instrument
    cp = await _cp(s, req.counterparty)
    lv = secmaster.live(inst.id)
    venue, vtype, reason = rules.route_order(side=req.side, tier=lv["tier"], qty=req.quantity)
    if req.type != "AUTO":
        vtype, reason = req.type, f"trader chose {req.type}"
    terms = rules.default_terms(inst, cp)
    terms.update({k: v for k, v in {"collateralType": req.collateralType, "collateralCcy": req.collateralCcy,
                                    "marginPct": req.marginPct, "legalAgreement": req.legalAgreement}.items() if v})
    settle = req.settlementDate or calendar.settle_date(clock.now(), inst.country)
    last = await market.last_fee(s, inst.id) or lv["feeBps"]
    checks = rules.pretrade_checks(side=req.side, inst=inst, qty=req.quantity, fee_bps=req.feeBps,
                                   price=lv["lastPx"], last_fee_bps=last, cp=cp,
                                   cp_exposure_usd=await exposure_usd(s, cp.code) if cp else 0.0,
                                   fx_usd=rules.FX_USD.get(inst.ccy, 1.0))
    if cp and cp.kind != "AGENT_LENDER" and req.side == "BORROW":
        checks.append(rules.Check("contra is a lender", False, f"{cp.code} is a {cp.kind.lower().replace('_', ' ')}"))
    return {"instrument": secmaster.instrument_json(inst), "counterparty": cp.code if cp else None, "venue": venue,
            "type": vtype, "routeReason": reason, "terms": {**terms, "term": req.term,
                                                            "settlementDate": settle.isoformat()},
            "notional": round(req.quantity * lv["lastPx"], 2), "lastFeeBps": last,
            "checks": [c.to_json() for c in checks]}


async def create(s: AsyncSession, req: OrderIn, *, by: str, action_id: str | None = None,
                 send: bool = True) -> Order:
    p = await preview(s, req)
    blocking = [c for c in p["checks"] if not c["ok"] and c["blocking"]]
    if blocking:
        raise OrderError("pre-trade checks failed: " + "; ".join(c["name"] for c in blocking), p["checks"])
    warnings = [c for c in p["checks"] if not c["ok"] and not c["blocking"]]
    if warnings and not req.override:
        raise OrderError("warnings need an override: " + "; ".join(c["name"] for c in warnings), p["checks"])
    t, now = p["terms"], clock.now()
    o = Order(id=events.new_id("ord"), side=req.side, type=p["type"], instrument_id=p["instrument"]["id"],
              qty=req.quantity, fee_bps=req.feeBps, cp_code=p["counterparty"], venue=p["venue"],
              route_reason=p["routeReason"], collateral_type=t["collateralType"], collateral_ccy=t["collateralCcy"],
              margin_pct=t["marginPct"], term=req.term, term_end=req.termEndDate,
              settle_date=date.fromisoformat(t["settlementDate"]), legal_agreement=t["legalAgreement"],
              booking_entity=t["bookingEntity"], status="NEW", checks=p["checks"], created_by=by,
              action_id=action_id, created_at=now, updated_at=now)
    s.add(o)
    await s.flush()
    events.record(s, "order", o.id, "created", order_json(o), actor=by)
    if send:
        await send_to_venue(s, o)
    return o


async def send_to_venue(s: AsyncSession, o: Order) -> Order:
    inst = await s.get(Instrument, o.instrument_id)
    cp = await s.get(Counterparty, o.cp_code) if o.cp_code else None
    body = {"clientOrderId": o.id, "type": o.type, "instrument": inst.isin, "quantity": o.qty, "feeBps": o.fee_bps,
            "counterpartyCode": cp.ngt_code if cp else None, "term": o.term,
            "termEndDate": o.term_end.isoformat() if o.term_end else None, "collateralType": o.collateral_type,
            "collateralCcy": o.collateral_ccy, "marginPct": o.margin_pct,
            "settlementDate": o.settle_date.isoformat(), "legalAgreement": o.legal_agreement,
            "bookingEntity": o.booking_entity}
    before = order_json(o)
    try:
        v = await systems().venue_a.place(body)
        o.venue_order_id, o.status = v["orderId"], VENUE_STATUS.get(v["status"], "SENT")
    except AdapterError as e:
        o.status, o.reject_reason = ("REJECTED" if e.status and e.status < 500 else "FAILED"), str(e)
    o.updated_at = clock.now()
    events.record(s, "order", o.id, "sent" if o.venue_order_id else "send_failed", order_json(o, inst),
                  before=before, actor="system")
    return o


async def apply_venue(s: AsyncSession, o: Order, v: dict[str, Any]) -> bool:
    """Fold a venue order snapshot into ours. Returns True if anything changed."""
    status = VENUE_STATUS.get(v["status"], o.status)
    changed = (status, v.get("counterFeeBps"), v.get("counterQuantity")) != (o.status, o.counter_fee_bps,
                                                                              o.counter_qty)
    if not changed:
        return False
    before = order_json(o)
    o.status, o.counter_fee_bps, o.counter_qty = status, v.get("counterFeeBps"), v.get("counterQuantity")
    o.reject_reason = v.get("rejectReason") or o.reject_reason
    if v.get("counterpartyCode") and not o.cp_code:
        cp = (await s.execute(select(Counterparty).where(Counterparty.ngt_code == v["counterpartyCode"]))).scalar()
        o.cp_code = cp.code if cp else None
    o.updated_at = clock.now()
    events.record(s, "order", o.id, status.lower(), order_json(o), before=before, actor="venue_a")
    return True


async def add_trade(s: AsyncSession, v: dict[str, Any]) -> Trade | None:
    """Record a Venue A execution once."""
    if (await s.execute(select(Trade.id).where(Trade.venue_trade_id == v["tradeId"]))).scalar():
        return None
    o = await s.get(Order, v["clientOrderId"]) if v.get("clientOrderId") else None
    ours_borrow = v["borrower"]["code"] == OUR_FIRM["ngt_code"]
    contra = v["lender"] if ours_borrow else v["borrower"]
    cp = (await s.execute(select(Counterparty).where(Counterparty.ngt_code == contra["code"]))).scalar()
    t = Trade(id=events.new_id("trd"), order_id=o.id if o else None, venue="VENUE_A", venue_trade_id=v["tradeId"],
              uti=v.get("uti"), side="BORROW" if ours_borrow else "LEND", instrument_id=v["instrumentId"],
              cp_code=cp.code if cp else contra["code"], qty=int(v["quantity"]), fee_bps=float(v["feeBps"]),
              price=float(v["price"]), ccy=v["ccy"], settle_date=date.fromisoformat(v["settlementDate"]),
              venue_json=v, executed_at=clock.parse_ts(v["executedAt"]))
    s.add(t)
    if o is not None:
        o.filled_qty += t.qty
        o.cp_code = o.cp_code or t.cp_code
    await s.flush()
    from .booking import trade_json

    events.record(s, "trade", t.id, "executed", trade_json(t), actor="venue_a")
    return t


async def venue_command(s: AsyncSession, o: Order, cmd: Literal["accept", "cancel", "counter"], *, by: str,
                        fee_bps: float | None = None, quantity: int | None = None) -> Order:
    if o.status not in OPEN or not o.venue_order_id:
        raise OrderError(f"order {o.id} is {o.status}")
    va = systems().venue_a
    try:
        if cmd == "accept":
            v = await va.accept(o.venue_order_id)
            await add_trade(s, v)
            v = await va.order(o.venue_order_id)
        elif cmd == "cancel":
            v = await va.cancel(o.venue_order_id)
        else:
            if fee_bps is None:
                raise OrderError("counter needs a fee")
            v = await va.counter(o.venue_order_id, fee_bps, quantity)
            o.fee_bps = fee_bps
    except AdapterError as e:
        raise OrderError(str(e)) from e
    await apply_venue(s, o, v)
    events.record(s, "order", o.id, f"trader_{cmd}", order_json(o), actor=by)
    return o


async def listing(s: AsyncSession, *, status: list[str] | None = None, limit: int = 300) -> list[dict[str, Any]]:
    q = select(Order).order_by(Order.created_at.desc()).limit(limit)
    if status:
        q = q.where(Order.status.in_(status))
    rows = list((await s.execute(q)).scalars())
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(
        Instrument.id.in_({o.instrument_id for o in rows})))).scalars()}
    return [order_json(o, insts.get(o.instrument_id)) for o in rows]


async def open_count(s: AsyncSession) -> int:
    return int((await s.execute(select(func.count()).select_from(Order).where(Order.status.in_(OPEN)))).scalar() or 0)
