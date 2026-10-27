"""Venue A order engine, shared by the REST and FIX front ends.

States: PENDING -> (COUNTERED <-> PENDING)* -> EXECUTED | CANCELLED | EXPIRED | REJECTED.

Simulated lenders answer a BID after a latency: execute if the bid meets their ask, counter
if it's close, reject if it's far off or they have no supply. GC FIRM_BIDs fill immediately
against any lender with supply. OFFERs (we lend) are hit by simulated borrowers when cheap
enough, otherwise they rest until expiry.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mockcore import clock, rng
from mockcore.app import ServiceContext
from refdata import calendar, market
from refdata import identifiers as ids
from refdata.counterparties import OUR_FIRM, counterparties, counterparty_by_code
from refdata.universe import Instrument, instrument_by_id, resolve, universe

from .models import Inventory, Order, Print, Trade

VENUE_LEI = ids.make_lei("MOCK00VENUEANGT001")
TERMINAL = {"EXECUTED", "CANCELLED", "EXPIRED", "REJECTED"}


class VenueConfig(BaseModel):
    latency_gc_s: tuple[float, float] = (1.0, 3.0)
    latency_special_s: tuple[float, float] = (5.0, 20.0)
    warm_spread: tuple[float, float] = (1.00, 1.10)
    hot_spread: tuple[float, float] = (1.05, 1.25)
    counter_band: float = Field(0.80, description="Bids at or above ask*band get a counter instead of a reject")
    max_counters: int = 2
    default_ttl_s: int = 900
    feed_every_s: float = 5.0
    prints_every_s: float = 15.0


config = VenueConfig()


class OrderRequest(BaseModel):
    clientOrderId: str = Field(..., max_length=64)
    type: Literal["BID", "OFFER", "FIRM_BID"]
    instrument: str = Field(..., description="ISIN, SEDOL, CUSIP, FIGI or 'TICK XX'")
    quantity: int = Field(..., gt=0, le=500_000_000)
    feeBps: float = Field(..., ge=0, le=100_000)
    counterpartyCode: str | None = Field(None, description="Venue code of the contra; omit to hit the best lender")
    term: Literal["OPEN", "TERM"] = "OPEN"
    termEndDate: date | None = None
    collateralType: Literal["CASH", "NONCASH"]
    collateralCcy: str = Field(..., min_length=3, max_length=3)
    marginPct: float = Field(..., ge=100, le=150)
    settlementDate: date
    legalAgreement: Literal["MSLA", "GMSLA_2010", "GMSLA_2018"]
    bookingEntity: str = Field(..., max_length=64)
    ttlSeconds: int | None = Field(None, gt=0, le=86_400)

    @model_validator(mode="after")
    def _term(self) -> OrderRequest:
        if self.term == "TERM" and not self.termEndDate:
            raise ValueError("termEndDate is required for TERM loans")
        return self


class VenueError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def lenders() -> list[str]:
    return [c.code for c in counterparties() if c.kind == "AGENT_LENDER"]


def borrowers() -> list[str]:
    return [c.code for c in counterparties() if c.kind in ("PRIME_BROKER", "BROKER_DEALER", "HEDGE_FUND")]


def order_json(o: Order) -> dict[str, Any]:
    cp = counterparty_by_code(o.counterparty) if o.counterparty else None
    return {
        "orderId": o.order_id,
        "clientOrderId": o.client_order_id,
        "type": o.type,
        "side": o.side,
        "status": o.status,
        "instrumentId": o.instrument_id,
        "isin": o.isin,
        "composite": instrument_by_id(o.instrument_id).composite,
        "quantity": o.qty,
        "feeBps": o.fee_bps,
        "counterpartyCode": cp.ngt_code if cp else None,
        "counterpartyName": cp.name if cp else None,
        "counterFeeBps": o.counter_fee_bps,
        "counterQuantity": o.counter_qty,
        "counters": o.counters,
        "term": o.term,
        "termEndDate": o.term_end.isoformat() if o.term_end else None,
        "collateralType": o.collateral_type,
        "collateralCcy": o.collateral_ccy,
        "marginPct": o.margin_pct,
        "settlementDate": o.settle_date.isoformat(),
        "legalAgreement": o.legal_agreement,
        "bookingEntity": o.booking_entity,
        "rejectReason": o.reject_reason,
        "source": o.source,
        "expiresAt": clock.iso(o.expires_at),
        "createdAt": clock.iso(o.created_at),
        "updatedAt": clock.iso(o.updated_at),
        "history": o.history,
    }


def trade_json(t: Trade) -> dict[str, Any]:
    lender, borrower = counterparty_by_code(t.lender), counterparty_by_code(t.borrower)
    return {
        "tradeId": t.trade_id,
        "uti": t.uti,
        "orderId": t.order_id,
        "clientOrderId": t.client_order_id,
        "instrumentId": t.instrument_id,
        "isin": t.isin,
        "lender": {"code": lender.ngt_code, "name": lender.name, "lei": lender.lei} if lender else _ours(),
        "borrower": {"code": borrower.ngt_code, "name": borrower.name, "lei": borrower.lei} if borrower else _ours(),
        "quantity": t.qty,
        "feeBps": t.fee_bps,
        "price": t.price,
        "ccy": t.ccy,
        "loanValue": t.loan_value,
        "term": t.term,
        "termEndDate": t.term_end.isoformat() if t.term_end else None,
        "collateralType": t.collateral_type,
        "collateralCcy": t.collateral_ccy,
        "marginPct": t.margin_pct,
        "settlementDate": t.settle_date.isoformat(),
        "legalAgreement": t.legal_agreement,
        "bookingEntity": t.booking_entity,
        "executedAt": clock.iso(t.executed_at),
    }


def _ours() -> dict[str, str]:
    return {"code": OUR_FIRM["ngt_code"], "name": OUR_FIRM["name"], "lei": OUR_FIRM["lei"]}


def _end_of_day(ts: datetime) -> datetime:
    local = ts.astimezone(clock.NEW_YORK)
    return datetime.combine(local.date(), time(16, 30), tzinfo=clock.NEW_YORK)


def _hist(o: Order, event: str, **data: Any) -> None:
    o.history = [*o.history, {"ts": clock.iso(clock.now()), "event": event, **data}]
    o.updated_at = clock.now()


async def _emit_order(ctx: ServiceContext, s: AsyncSession, o: Order, type_: str) -> None:
    await ctx.events.emit(s, type_, "order", o.order_id, order_json(o), topic="orders")


# -- inventory -------------------------------------------------------------------------------
async def seed_inventory(ctx: ServiceContext) -> None:
    r = rng.fresh("ngt", "inventory")
    lender_codes = lenders()
    region_of = {c.code: c.region for c in counterparties()}
    country_region = {"US": "US", "GB": "UK", "DE": "EU", "FR": "EU", "NL": "EU", "JP": "JP"}
    now = clock.now()
    rows = []
    for inst in universe():
        local = [c for c in lender_codes if region_of[c] == country_region[inst.country]] or lender_codes
        k = r.choice((1, 2, 2, 3))
        chosen = sorted(set(r.choices(local, k=k) + ([r.choice(lender_codes)] if r.random() < 0.3 else [])))
        lendable = inst.shs_out * inst.free_float_pct / 100 * 0.25 * max(1 - inst.base_util_pct / 100, 0.02)
        for code in chosen:
            qty = int(lendable / len(chosen) * r.uniform(0.3, 1.0)) // 1000 * 1000
            rows.append({"lender": code, "instrument_id": inst.id, "qty": qty,
                         "fee_mult": round(r.uniform(0.97, 1.12), 4), "updated_at": now})
    async with ctx.session() as s:
        await s.execute(Inventory.__table__.insert(), rows)
        await s.commit()


async def availability(s: AsyncSession, instrument_id: str | None = None) -> list[dict[str, Any]]:
    q = select(Inventory).order_by(Inventory.instrument_id, Inventory.lender)
    if instrument_id:
        q = q.where(Inventory.instrument_id == instrument_id)
    now = clock.now()
    out = []
    for inv in (await s.execute(q)).scalars():
        inst = instrument_by_id(inv.instrument_id)
        cp = counterparty_by_code(inv.lender)
        out.append({
            "instrumentId": inv.instrument_id,
            "isin": inst.isin,
            "composite": inst.composite,
            "lenderCode": cp.ngt_code,
            "lenderName": cp.name,
            "quantity": inv.qty,
            "indicativeFeeBps": round(market.fee_bps(inst, now) * inv.fee_mult, 1),
            "asOf": clock.iso(inv.updated_at),
        })
    return out


def aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        a = agg.setdefault(r["instrumentId"], {"instrumentId": r["instrumentId"], "isin": r["isin"],
                                                "composite": r["composite"], "quantity": 0, "lenders": 0,
                                                "bestFeeBps": None, "asOf": r["asOf"]})
        a["quantity"] += r["quantity"]
        a["lenders"] += 1
        if r["quantity"] > 0 and (a["bestFeeBps"] is None or r["indicativeFeeBps"] < a["bestFeeBps"]):
            a["bestFeeBps"] = r["indicativeFeeBps"]
        a["asOf"] = max(a["asOf"], r["asOf"])
    return agg


async def publish_availability(ctx: ServiceContext, s: AsyncSession, instrument_ids: list[str]) -> None:
    for iid in sorted(set(instrument_ids)):
        rows = await availability(s, iid)
        agg = aggregate(rows).get(iid)
        if agg:
            ctx.hub.publish("availability", {"type": "availability", "ts": clock.iso(clock.now()),
                                             "data": {**agg, "byLender": rows}})


async def feed_tick(ctx: ServiceContext) -> None:
    """Random-walk a slice of inventory; publish deltas (not written to the event log)."""
    r = rng.stream("ngt", "feed")
    async with ctx.session() as s:
        total = (await s.execute(select(func.count()).select_from(Inventory))).scalar_one()
        if not total:
            return
        picks = sorted({r.randint(1, total) for _ in range(25)})
        rows = (await s.execute(select(Inventory).where(Inventory.id.in_(picks)))).scalars().all()
        for inv in rows:
            inv.qty = max(int(inv.qty * math.exp(r.gauss(0, 0.05))) // 1000 * 1000, 0)
            inv.updated_at = clock.now()
        await s.commit()
        await publish_availability(ctx, s, [i.instrument_id for i in rows])


async def prints_tick(ctx: ServiceContext) -> None:
    """Simulated street activity so every active name has a recent last-traded fee."""
    r = rng.stream("ngt", "prints")
    insts = universe()
    async with ctx.session() as s:
        for _ in range(4):
            inst = r.choice(insts)
            if inst.tier == "GC" and r.random() < 0.5:
                continue
            fee = round(market.fee_bps(inst, clock.now()) * (1 + r.gauss(0, 0.03)), 1)
            qty = max(int(r.lognormvariate(0, 0.8) * 200_000) // 1000 * 1000, 1000)
            p = Print(instrument_id=inst.id, qty=qty, fee_bps=fee, ts=clock.now())
            s.add(p)
            ctx.hub.publish("prints", {"type": "print", "ts": clock.iso(p.ts),
                                       "data": {"instrumentId": inst.id, "isin": inst.isin,
                                                "composite": inst.composite, "quantity": qty, "feeBps": fee}})
        await s.commit()


# -- orders ----------------------------------------------------------------------------------
async def place(ctx: ServiceContext, s: AsyncSession, req: OrderRequest, participant: str, source: str,
                fix_session: str | None = None) -> Order:
    inst = resolve(req.instrument)
    if inst is None:
        raise VenueError("UNKNOWN_INSTRUMENT", f"unknown instrument {req.instrument!r}")
    if inst.restricted:
        raise VenueError("RESTRICTED", f"{inst.composite} is not eligible on this venue")
    cp = None
    if req.counterpartyCode:
        cp = counterparty_by_code(req.counterpartyCode)
        if cp is None:
            raise VenueError("UNKNOWN_COUNTERPARTY", f"no participant with code {req.counterpartyCode}")
        if req.type in ("BID", "FIRM_BID") and cp.kind != "AGENT_LENDER":
            raise VenueError("NOT_A_LENDER", f"{cp.ngt_code} does not lend on this venue")
        if cp.agreement != req.legalAgreement:
            raise VenueError("AGREEMENT_MISMATCH",
                             f"{cp.ngt_code} trades under {cp.agreement}, order says {req.legalAgreement}")
    if req.settlementDate < calendar.trade_date(clock.now()):
        raise VenueError("BAD_SETTLE_DATE", "settlement date is in the past")
    dup = (await s.execute(select(Order).where(Order.client_order_id == req.clientOrderId,
                                               Order.participant == participant))).scalar()
    if dup:
        raise VenueError("DUPLICATE_CLORDID", f"clientOrderId {req.clientOrderId} already used by {dup.order_id}")
    now = clock.now()
    ttl = req.ttlSeconds or config.default_ttl_s
    o = Order(
        order_id="pending", client_order_id=req.clientOrderId, participant=participant, source=source,
        fix_session=fix_session, type=req.type, side="LEND" if req.type == "OFFER" else "BORROW",
        instrument_id=inst.id, isin=inst.isin, qty=req.quantity, fee_bps=req.feeBps,
        counterparty=cp.code if cp else None, status="PENDING", term=req.term, term_end=req.termEndDate,
        collateral_type=req.collateralType, collateral_ccy=req.collateralCcy.upper(), margin_pct=req.marginPct,
        settle_date=req.settlementDate, legal_agreement=req.legalAgreement, booking_entity=req.bookingEntity,
        expires_at=min(now + timedelta(seconds=ttl), _end_of_day(now)) if _end_of_day(now) > now
        else now + timedelta(seconds=ttl),
        created_at=now, updated_at=now, history=[],
    )
    s.add(o)
    await s.flush()
    o.order_id = f"NGT-O-{o.id:08d}"
    _hist(o, "NEW", feeBps=o.fee_bps, quantity=o.qty)
    await _emit_order(ctx, s, o, "order_new")
    if o.type == "FIRM_BID":
        await _firm_bid(ctx, s, o, inst)
    elif o.type == "BID":
        lo, hi = config.latency_gc_s if inst.tier == "GC" else config.latency_special_s
        ctx.scheduler.after(rng.stream("ngt", "latency").uniform(lo, hi), f"respond:{o.order_id}",
                            respond_job, ctx, o.order_id)
    else:
        ctx.scheduler.after(rng.stream("ngt", "latency").uniform(*config.latency_special_s),
                            f"respond:{o.order_id}", offer_job, ctx, o.order_id)
    if o.status not in TERMINAL:
        ctx.scheduler.at(o.expires_at, f"expire:{o.order_id}", expire_job, ctx, o.order_id)
    return o


async def _pick_lender(s: AsyncSession, o: Order) -> Inventory | None:
    q = select(Inventory).where(Inventory.instrument_id == o.instrument_id)
    if o.counterparty:
        q = q.where(Inventory.lender == o.counterparty)
    rows = (await s.execute(q.order_by(Inventory.qty.desc(), Inventory.lender))).scalars().all()
    return rows[0] if rows else None


def _ask(inst: Instrument, inv: Inventory) -> float:
    fair = market.fee_bps(inst, clock.now()) * inv.fee_mult
    r = rng.stream("ngt", "ask")
    tier = market.tier_at(inst, clock.now())
    if tier == "HOT":
        fair *= r.uniform(*config.hot_spread)
    elif tier == "WARM":
        fair *= r.uniform(*config.warm_spread)
    return round(fair, 1)


async def _firm_bid(ctx: ServiceContext, s: AsyncSession, o: Order, inst: Instrument) -> None:
    if market.tier_at(inst, clock.now()) != "GC":
        await _reject(ctx, s, o, "FIRM_BID_GC_ONLY", "firm bids are only accepted on GC names")
        return
    inv = await _pick_lender(s, o)
    if inv is None or inv.qty <= 0:
        await _reject(ctx, s, o, "NO_AVAILABILITY", "no lender supply")
        return
    floor = market.fee_bps(inst, clock.now()) * 0.9
    if o.fee_bps < floor:
        await _reject(ctx, s, o, "BELOW_FLOOR", f"firm bid below GC floor {floor:.1f}bps")
        return
    o.counterparty = inv.lender
    await execute(ctx, s, o, o.fee_bps, min(o.qty, inv.qty), inv)


async def _reject(ctx: ServiceContext, s: AsyncSession, o: Order, code: str, text: str) -> None:
    o.status = "REJECTED"
    o.reject_reason = f"{code}: {text}"
    _hist(o, "REJECTED", reason=code)
    ctx.scheduler.cancel(f"expire:{o.order_id}")
    await _emit_order(ctx, s, o, "order_rejected")


async def _lender_decision(ctx: ServiceContext, s: AsyncSession, o: Order) -> None:
    inst = instrument_by_id(o.instrument_id)
    inv = await _pick_lender(s, o)
    if inv is None or inv.qty <= 0:
        await _reject(ctx, s, o, "NO_AVAILABILITY", "lender has no supply")
        return
    o.counterparty = inv.lender
    ask = _ask(inst, inv)
    qty = min(o.qty, inv.qty)
    if o.fee_bps >= ask and qty == o.qty:
        await execute(ctx, s, o, o.fee_bps, qty, inv)
    elif o.fee_bps >= ask * config.counter_band and o.counters < config.max_counters:
        o.status = "COUNTERED"
        o.counter_fee_bps = max(ask, o.fee_bps)
        o.counter_qty = qty
        _hist(o, "COUNTERED", feeBps=o.counter_fee_bps, quantity=qty)
        await _emit_order(ctx, s, o, "order_countered")
    else:
        await _reject(ctx, s, o, "FEE_TOO_LOW", f"lender ask {ask:.0f}bps")


async def respond_job(ctx: ServiceContext, order_id: str) -> None:
    async with ctx.session() as s:
        o = await _get(s, order_id, lock=True)
        if o is None or o.status != "PENDING":
            return
        await _lender_decision(ctx, s, o)
        await s.commit()


async def offer_job(ctx: ServiceContext, order_id: str) -> None:
    async with ctx.session() as s:
        o = await _get(s, order_id, lock=True)
        if o is None or o.status != "PENDING":
            return
        inst = instrument_by_id(o.instrument_id)
        r = rng.stream("ngt", "borrowers")
        bid = market.fee_bps(inst, clock.now()) * r.uniform(0.85, 1.0)
        if o.fee_bps <= bid:
            o.counterparty = r.choice(borrowers())
            await execute(ctx, s, o, o.fee_bps, o.qty, None)
        else:
            _hist(o, "RESTING", streetBidBps=round(bid, 1))
            await _emit_order(ctx, s, o, "order_resting")
        await s.commit()


async def expire_job(ctx: ServiceContext, order_id: str) -> None:
    async with ctx.session() as s:
        o = await _get(s, order_id, lock=True)
        if o is None or o.status in TERMINAL:
            return
        o.status = "EXPIRED"
        _hist(o, "EXPIRED")
        await _emit_order(ctx, s, o, "order_expired")
        await s.commit()


async def execute(ctx: ServiceContext, s: AsyncSession, o: Order, fee: float, qty: int,
                  inv: Inventory | None) -> Trade:
    inst = instrument_by_id(o.instrument_id)
    now = clock.now()
    px = market.price(inst, now)
    t = Trade(
        trade_id="pending", uti="pending:" + o.order_id, order_id=o.order_id, client_order_id=o.client_order_id,
        instrument_id=inst.id, isin=inst.isin,
        lender=o.counterparty if o.side == "BORROW" else OUR_FIRM["code"],
        borrower=OUR_FIRM["code"] if o.side == "BORROW" else o.counterparty,
        qty=qty, fee_bps=fee, price=px, ccy=inst.ccy, loan_value=round(qty * px, 2), term=o.term,
        term_end=o.term_end, collateral_type=o.collateral_type, collateral_ccy=o.collateral_ccy,
        margin_pct=o.margin_pct, settle_date=o.settle_date, legal_agreement=o.legal_agreement,
        booking_entity=o.booking_entity, executed_at=now,
    )
    s.add(t)
    await s.flush()
    t.trade_id = f"NGT-T-{t.id:08d}"
    t.uti = f"{VENUE_LEI}NGT{now:%Y%m%d}{t.id:010d}"
    if inv is not None:
        inv.qty = max(inv.qty - qty, 0)
        inv.updated_at = now
    o.status = "EXECUTED"
    o.qty, o.fee_bps = qty, fee
    _hist(o, "EXECUTED", tradeId=t.trade_id, feeBps=fee, quantity=qty)
    ctx.scheduler.cancel(f"expire:{o.order_id}")
    s.add(Print(instrument_id=inst.id, qty=qty, fee_bps=fee, ts=now, ours=True))
    await _emit_order(ctx, s, o, "order_executed")
    await ctx.events.emit(s, "trade_executed", "trade", t.trade_id, trade_json(t), topic="trades")
    ctx.hub.publish("prints", {"type": "print", "ts": clock.iso(now),
                               "data": {"instrumentId": inst.id, "isin": inst.isin, "composite": inst.composite,
                                        "quantity": qty, "feeBps": fee}})
    if inv is not None:
        await s.flush()
        await publish_availability(ctx, s, [inst.id])
    return t


async def _get(s: AsyncSession, order_id: str, lock: bool = False) -> Order | None:
    q = select(Order).where((Order.order_id == order_id) | (Order.client_order_id == order_id))
    if lock:
        q = q.with_for_update()
    return (await s.execute(q.order_by(Order.id.desc()).limit(1))).scalar()


async def counter(ctx: ServiceContext, s: AsyncSession, o: Order, fee: float, qty: int | None = None) -> Order:
    if o.status not in ("COUNTERED", "PENDING"):
        raise VenueError("BAD_STATE", f"cannot counter an order that is {o.status}")
    o.fee_bps = fee
    if qty:
        o.qty = qty
    o.counters += 1
    o.status = "PENDING"
    _hist(o, "COUNTER_FROM_PARTICIPANT", feeBps=fee, quantity=o.qty)
    await _emit_order(ctx, s, o, "order_replaced")
    ctx.scheduler.cancel(f"respond:{o.order_id}")
    ctx.scheduler.after(rng.stream("ngt", "latency").uniform(*config.latency_special_s), f"respond:{o.order_id}",
                        respond_job, ctx, o.order_id)
    return o


async def accept(ctx: ServiceContext, s: AsyncSession, o: Order) -> Trade:
    if o.status != "COUNTERED":
        raise VenueError("BAD_STATE", f"nothing to accept: order is {o.status}")
    inv = await _pick_lender(s, o)
    return await execute(ctx, s, o, o.counter_fee_bps, o.counter_qty or o.qty, inv)


async def cancel(ctx: ServiceContext, s: AsyncSession, o: Order) -> Order:
    if o.status in TERMINAL:
        raise VenueError("BAD_STATE", f"order already {o.status}")
    o.status = "CANCELLED"
    _hist(o, "CANCELLED")
    ctx.scheduler.cancel(f"respond:{o.order_id}")
    ctx.scheduler.cancel(f"expire:{o.order_id}")
    await _emit_order(ctx, s, o, "order_cancelled")
    return o


async def get_order(s: AsyncSession, order_id: str, lock: bool = False) -> Order:
    o = await _get(s, order_id, lock)
    if o is None:
        raise VenueError("UNKNOWN_ORDER", f"no order {order_id}")
    return o
