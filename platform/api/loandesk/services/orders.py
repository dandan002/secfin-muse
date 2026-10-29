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
from ..settings import get_settings
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
    venue: Literal["AUTO", "VENUE_A", "VENUE_B"] = "AUTO"
    type: Literal["AUTO", "BID", "OFFER", "FIRM_BID", "INTEREST_LIST"] = "AUTO"
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
    if req.venue == "VENUE_B" or req.type == "INTEREST_LIST":
        venue, vtype, reason = "VENUE_B", "INTEREST_LIST", \
            "trader chose Venue B: an interest list the optimiser matches against dealer lists"
    elif req.type != "AUTO":
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
    if cp and venue == "VENUE_B":
        checks.append(rules.Check("venue B is anonymous", False, "interest lists match against any dealer: "
                                  f"drop {cp.code} or route to Venue A"))
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
        cfg = get_settings()
        await send_to_venue(s, o, failover=cfg.failover_to_venue_b and req.venue == "AUTO" and req.type == "AUTO"
                            and not o.cp_code)
    return o


def _down(e: AdapterError) -> bool:
    return e.status is None or e.status >= 500


async def send_to_venue(s: AsyncSession, o: Order, *, failover: bool = False) -> Order:
    """Place the order on its venue. ``failover``: if Venue A is down, place it on Venue B and say so."""
    inst = await s.get(Instrument, o.instrument_id)
    before = order_json(o)
    if o.venue == "VENUE_B":
        await _place_b(o, inst)
    else:
        await _place_a(s, o, inst)
        if failover and o.status == "FAILED":
            a_error = o.reject_reason
            o.venue, o.type, o.reject_reason = "VENUE_B", "INTEREST_LIST", None
            o.route_reason = f"failover: Venue A down ({a_error}); sent to Venue B as an interest list"
            await _place_b(o, inst)
            if o.status == "FAILED":
                o.reject_reason = f"{a_error}; {o.reject_reason}"
    o.updated_at = clock.now()
    events.record(s, "order", o.id, "sent" if o.venue_order_id else "send_failed", order_json(o, inst),
                  before=before, actor="system")
    return o


async def _place_b(o: Order, inst: Instrument) -> None:
    """A one-line interest list at our limit, auto-accepted on our side: a dealer agreeing at or inside the limit
    fills it; a dealer counter outside it comes back to the trader as COUNTERED."""
    vb = systems().venue_b
    line = {"instrument": inst.isin, "side": o.side, "quantity": o.qty, "feeBps": o.fee_bps, "term": o.term,
            "collateralType": o.collateral_type, "collateralCcy": o.collateral_ccy}
    try:
        lst = await vb.upload_list(f"order {o.id}", [line], auto_accept=True, client_ref=o.id)
        o.venue_order_id, o.status = lst["listId"], "PENDING"
    except AdapterError as e:
        o.status, o.reject_reason = ("REJECTED" if not _down(e) else "FAILED"), str(e)
        return
    try:
        await vb.run_optimiser(lst["listId"])  # match now rather than on the venue's next pass
    except AdapterError:
        pass


async def _place_a(s: AsyncSession, o: Order, inst: Instrument) -> None:
    cp = await s.get(Counterparty, o.cp_code) if o.cp_code else None
    body = {"clientOrderId": o.id, "type": o.type, "instrument": inst.isin, "quantity": o.qty, "feeBps": o.fee_bps,
            "counterpartyCode": cp.ngt_code if cp else None, "term": o.term,
            "termEndDate": o.term_end.isoformat() if o.term_end else None, "collateralType": o.collateral_type,
            "collateralCcy": o.collateral_ccy, "marginPct": o.margin_pct,
            "settlementDate": o.settle_date.isoformat(), "legalAgreement": o.legal_agreement,
            "bookingEntity": o.booking_entity}
    try:
        v = await systems().venue_a.place(body)
        o.venue_order_id, o.status = v["orderId"], VENUE_STATUS.get(v["status"], "SENT")
    except AdapterError as e:
        o.status, o.reject_reason = ("FAILED" if _down(e) else "REJECTED"), str(e)


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
              venue_json=v, executed_at=clock.parse_ts(v["executedAt"]), open_qty=int(v["quantity"]),
              loan_status="OPEN", lifecycle=[])
    s.add(t)
    if o is not None:
        o.filled_qty += t.qty
        o.cp_code = o.cp_code or t.cp_code
    await s.flush()
    from .booking import trade_json

    events.record(s, "trade", t.id, "executed", trade_json(t), actor="venue_a")
    await _auto_book(s, t)
    return t


async def _auto_book(s: AsyncSession, t: Trade) -> None:
    if get_settings().auto_book:
        from .booking import book

        await book(s, t, by="policy:AUTO-BOOK")


# -- Venue B: interest lists, matches and loans --------------------------------------------------------
async def apply_venue_b(s: AsyncSession, o: Order, lst: dict[str, Any] | None, matches: list[dict[str, Any]]
                        ) -> bool:
    """Fold our list and its matches into the order. A match waiting on us (a dealer counter outside our limit)
    shows as COUNTERED; fills come from the loans (``add_trade_b``)."""
    waiting = [m for m in matches if m["status"] == "PROPOSED" and not m["ourAccept"]]
    if o.filled_qty >= o.qty:
        status = "EXECUTED"
    elif lst and lst["status"] == "WITHDRAWN":
        status, waiting = "CANCELLED", []
    elif waiting:
        status = "COUNTERED"
    else:
        status = "PENDING"
    counter_fee = waiting[0]["feeBps"] if waiting else None
    counter_qty = sum(m["quantity"] for m in waiting) if waiting else None
    if (status, counter_fee, counter_qty) == (o.status, o.counter_fee_bps, o.counter_qty):
        return False
    before = order_json(o)
    o.status, o.counter_fee_bps, o.counter_qty = status, counter_fee, counter_qty
    if waiting and not o.cp_code:
        cp = (await s.execute(select(Counterparty).where(
            Counterparty.wematch_entity == waiting[0]["counterparty"]["entity"]))).scalar()
        o.cp_code = cp.code if cp else None
    o.updated_at = clock.now()
    events.record(s, "order", o.id, status.lower(), order_json(o), before=before, actor="venue_b")
    return True


def within_limit(o: Order, fee_bps: float) -> bool:
    """A dealer's fee the order already allows: no more than our limit to borrow, no less to lend."""
    return fee_bps <= o.fee_bps if o.side == "BORROW" else fee_bps >= o.fee_bps


async def accept_within_limit(s: AsyncSession, o: Order, matches: list[dict[str, Any]]) -> bool:
    """A dealer counter inside the order's limit is already covered by the order: accept it on the venue.
    (A counter resets our side's acceptance there, so the list's auto-accept no longer applies.)"""
    vb, done = systems().venue_b, False
    for m in matches:
        if m["status"] == "PROPOSED" and not m["ourAccept"] and within_limit(o, float(m["feeBps"])):
            await vb.accept(m["matchId"])
            events.record(s, "order", o.id, "accepted_within_limit", {"matchId": m["matchId"], "feeBps": m["feeBps"],
                                                                     "limitBps": o.fee_bps}, actor="policy:LIMIT")
            done = True
    return done


async def add_trade_b(s: AsyncSession, loan: dict[str, Any], list_id: str | None) -> Trade | None:
    """Record a Venue B loan once. Venue B gives no UTI, so we generate ours (ISO 23897: our LEI + a unique
    suffix); terms the venue doesn't carry come from the order, or the desk's defaults for the counterparty."""
    if (await s.execute(select(Trade.id).where(Trade.venue_trade_id == loan["loanId"]))).scalar():
        return None
    o = (await s.execute(select(Order).where(Order.venue == "VENUE_B", Order.venue_order_id == list_id))).scalar() \
        if list_id else None
    cp = (await s.execute(select(Counterparty).where(
        Counterparty.wematch_entity == loan["counterparty"]["entity"]))).scalar()
    inst = await s.get(Instrument, loan["instrumentId"])
    terms = rules.default_terms(inst, cp)
    if o is not None:
        terms.update(marginPct=o.margin_pct, legalAgreement=o.legal_agreement, bookingEntity=o.booking_entity,
                     termEndDate=o.term_end.isoformat() if o.term_end else None)
    terms.update(collateralType=loan["collateralType"], collateralCcy=loan["collateralCcy"], term=loan["term"])
    booked = next((h["ts"] for h in loan.get("history", []) if h["event"] == "BOOKED"), None)
    t = Trade(id=events.new_id("trd"), order_id=o.id if o else None, venue="VENUE_B", venue_trade_id=loan["loanId"],
              uti=f"{OUR_FIRM['lei']}{loan['loanId'].replace('-', '')}", side=loan["side"],
              instrument_id=inst.id, cp_code=cp.code if cp else loan["counterparty"]["entity"],
              qty=int(loan["quantityOriginal"]), open_qty=int(loan["quantityOpen"]), fee_bps=float(loan["feeBps"]),
              price=float(loan["priceAtTrade"]), ccy=loan["ccy"],
              settle_date=date.fromisoformat(loan["settlementDate"]), venue_json={**loan, **terms},
              executed_at=clock.parse_ts(booked) if booked else clock.now(), loan_status="OPEN", lifecycle=[])
    s.add(t)
    if o is not None:
        o.filled_qty += t.qty
        o.cp_code = o.cp_code or t.cp_code
    await s.flush()
    from .booking import trade_json

    events.record(s, "trade", t.id, "executed", trade_json(t, inst), actor="venue_b")
    if o is not None and o.filled_qty >= o.qty:
        await apply_venue_b(s, o, None, [])
    await _auto_book(s, t)
    return t


async def _venue_b_command(s: AsyncSession, o: Order, cmd: str, fee_bps: float | None,
                           quantity: int | None) -> None:
    vb = systems().venue_b
    matches = await vb.matches(o.venue_order_id)
    waiting = [m for m in matches if m["status"] == "PROPOSED" and not m["ourAccept"]]
    if cmd == "accept":
        if not waiting:
            raise OrderError("nothing from a dealer to accept")
        for m in waiting:
            await vb.accept(m["matchId"])
    elif cmd == "counter":
        if fee_bps is None:
            raise OrderError("counter needs a fee")
        if not waiting:
            raise OrderError("nothing from a dealer to counter")
        for m in waiting:
            await vb.propose(m["matchId"], fee_bps, quantity if len(waiting) == 1 else None)
        o.fee_bps = fee_bps
    else:
        for m in matches:
            if m["status"] == "PROPOSED":
                await vb.reject(m["matchId"])
        await vb.withdraw(o.venue_order_id)
    lst = await vb.interest_list(o.venue_order_id)
    await apply_venue_b(s, o, lst, await vb.matches(o.venue_order_id))


# -- chat: a trade agreed in Desk Chat becomes an order card ----------------------------------------------
async def propose_from_chat(s: AsyncSession, m: Any) -> list[str]:
    """Draft one order per agreed line, to the counterparty we agreed it with, for the gate.

    Nothing is sent: the trader approves the card. Pre-trade checks run now so the card shows them; approving
    a card with warnings on it is the override. A GC line with no fee is priced at the name's market fee."""
    from .. import gate

    if not m.our_side or not m.counterparty:
        return ["agreed, but who's on which side isn't clear: the trader books it"]
    notes: list[str] = []
    for ln in m.lines or []:
        if not ln.get("instrumentId") or not ln.get("quantity"):
            notes.append(f"{ln.get('identifier')}: no name or size to trade")
            continue
        fee, why = ln.get("feeBps"), "the agreed fee"
        if fee is None:
            fee, why = round(secmaster.live(ln["instrumentId"])["feeBps"], 1), "no fee in chat (GC): market fee"
        req = OrderIn(side=m.our_side, instrument=ln["instrumentId"], quantity=int(ln["quantity"]), feeBps=fee,
                      counterparty=m.counterparty)
        try:
            p = await preview(s, req)
        except OrderError as e:
            notes.append(f"{ln.get('identifier')}: {e}")
            continue
        failed = [c for c in p["checks"] if not c["ok"]]
        req.override = any(not c["blocking"] for c in failed)
        name = p["instrument"]["composite"]
        try:
            a = await gate.propose(
                s, kind="order", payload=req.model_dump(mode="json"), proposed_by=f"chat:{m.parser}",
                title=f"{m.our_side.lower()} {int(ln['quantity']):,} {name} @ {fee:g} bps with {m.counterparty} "
                      f"(agreed in chat)",
                rationale=f"Agreed in Desk Chat with {m.counterparty}: \"{m.text[:120]}\". Priced at {why}; "
                          f"{p['routeReason']}.",
                risk_flags=[f"{'BLOCKED' if c['blocking'] else 'warning'}: {c['name']} ({c['detail']})"
                            for c in failed],
                original={"message": m.id, "line": ln}, source_ref=m.id)
            notes.append(f"{name}: order card {a.id}")
        except gate.GateError as e:
            notes.append(f"{name}: {e}")
    return notes


async def venue_command(s: AsyncSession, o: Order, cmd: Literal["accept", "cancel", "counter"], *, by: str,
                        fee_bps: float | None = None, quantity: int | None = None) -> Order:
    if o.status not in OPEN or not o.venue_order_id:
        raise OrderError(f"order {o.id} is {o.status}")
    if o.venue == "VENUE_B":
        try:
            await _venue_b_command(s, o, cmd, fee_bps, quantity)
        except AdapterError as e:
            raise OrderError(str(e)) from e
        events.record(s, "order", o.id, f"trader_{cmd}", order_json(o), actor=by)
        return o
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
