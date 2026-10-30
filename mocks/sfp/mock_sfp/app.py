"""mock-sfp: "Processor (FIS-style)", a books-and-records processor for securities loans.

Booking (``POST /trades``, CDM-shaped or flat JSON), positions, lifecycle (return, recall,
re-rate, mark), contract compare with field-level diffs, amendments, a nightly mark that
recalculates collateral and emits margin calls (JSON, or ISO 20022 colr.003 XML), an end-of-day
contracts file, and a WebSocket stream of contract, break and margin-call events. Reference: ../API.md.

A seeded ~3% of bookings land with one deliberately wrong field (fee, quantity or settle
date) so reconciliation has real breaks to find. ``force_break_next`` makes the next one
break on demand for the demo. API shapes are our own.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from datetime import time as dtime
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query, WebSocket
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from mockcore import clock, rng
from mockcore.app import ServiceContext, ServiceSpec, create_app
from mockcore.keys import AuthSpec, Grant, Principal
from refdata import calendar, market
from refdata.counterparties import OUR_FIRM, counterparty_by_code
from refdata.universe import instrument_by_id, resolve

from .models import Base, Break, BuyIn, Contract, Mark, RecallNotice

SCOPES = {
    "trades:write": "Book trades",
    "contracts:read": "Contracts, positions and open breaks",
    "contracts:write": "Amend contracts and run lifecycle events (return, recall, re-rate)",
    "reconcile": "Compare your view of a contract with the processor's",
    "marks:read": "Marks and margin calls",
    "marks:run": "Run the mark on demand",
    "eod:read": "End-of-day contracts file",
}
spec = ServiceSpec(
    name="mock-sfp",
    label="Processor (FIS-style)",
    schema="sfp",
    base=Base,
    description="Simulated securities-finance processor: booking, positions, lifecycle, compare, marks, EOD file.",
    auth=AuthSpec(SCOPES, lambda: [
        Grant(OUR_FIRM["code"], tuple(SCOPES), "partner", f"{OUR_FIRM['name']} (client)"),
        Grant(OUR_FIRM["code"], ("contracts:read", "marks:read", "eod:read"), "standard", "Operations (read-only)",
              name="ops-readonly"),
    ]),
)
ctx = ServiceContext(spec)
app = create_app(ctx)

_flags: dict[str, Any] = {"break_rate": 0.03, "force_break_next": False, "eod_break": False}
COMPARE_FIELDS = ("isin", "side", "counterparty", "quantity", "feeBps", "collateralType", "collateralCcy",
                  "marginPct", "term", "termEndDate", "settlementDate", "legalAgreement", "bookingEntity")


def client(scope: str):  # noqa: ANN201 - a FastAPI dependency
    """The calling firm (the key's principal), entitled to ``scope`` (see mockcore.keys)."""
    gate = ctx.require(scope)

    async def dep(p: Principal = Depends(gate)) -> str:
        return p.principal

    return dep


# -- input models ------------------------------------------------------------------------------
class TradeIn(BaseModel):
    clientRef: str | None = Field(None, description="Idempotency key")
    venue: str | None = None
    venueRef: str | None = None
    uti: str | None = None
    side: Literal["BORROW", "LEND"]
    counterparty: str = Field(..., description="Our code, LEI, venue code or venue entity")
    instrument: str
    quantity: int = Field(..., gt=0)
    feeBps: float = Field(..., ge=0)
    price: float | None = Field(None, gt=0)
    collateralType: Literal["CASH", "NONCASH"]
    collateralCcy: str = Field(..., min_length=3, max_length=3)
    marginPct: float = Field(..., ge=100, le=150)
    term: Literal["OPEN", "TERM"] = "OPEN"
    termEndDate: date | None = None
    tradeDate: date | None = None
    settlementDate: date
    legalAgreement: Literal["MSLA", "GMSLA_2010", "GMSLA_2018"]
    bookingEntity: str


def cdm_to_flat(doc: dict[str, Any]) -> dict[str, Any]:
    """Map our simplified CDM-shaped securities-lending trade to the flat booking form."""
    ids = {i.get("identifierType") or i.get("issuer"): i.get("assignedIdentifier")
           for i in doc.get("tradeIdentifier", [])}
    sl = doc.get("economicTerms", {}).get("securityLending", {})
    parties = {p.get("role", "").upper(): p for p in doc.get("party", [])}
    lender, borrower = parties.get("LENDER", {}), parties.get("BORROWER", {})
    ours = OUR_FIRM["lei"]
    side = "BORROW" if borrower.get("partyId") == ours else "LEND"
    other = lender if side == "BORROW" else borrower
    coll = sl.get("collateral", {})
    term = sl.get("termination", {"type": "OPEN"})
    venue = doc.get("venue")
    return {
        "clientRef": doc.get("clientRef"),
        "venue": venue,
        "venueRef": ids.get(venue) or ids.get("VENUE"),
        "uti": ids.get("UTI"),
        "side": side,
        "counterparty": other.get("partyId", ""),
        "instrument": sl.get("security", {}).get("isin", ""),
        "quantity": sl.get("quantity"),
        "feeBps": (sl.get("feeRate") or {}).get("bps"),
        "price": sl.get("price"),
        "collateralType": coll.get("type"),
        "collateralCcy": coll.get("currency"),
        "marginPct": coll.get("marginPercentage"),
        "term": term.get("type", "OPEN"),
        "termEndDate": term.get("endDate"),
        "tradeDate": doc.get("tradeDate"),
        "settlementDate": sl.get("settlementDate"),
        "legalAgreement": doc.get("legalAgreement"),
        "bookingEntity": doc.get("bookingEntity"),
    }


def flat_of(c: Contract) -> dict[str, Any]:
    cp = counterparty_by_code(c.counterparty)
    return {"isin": c.isin, "side": c.side, "counterparty": cp.code if cp else c.counterparty, "quantity": c.qty,
            "feeBps": c.fee_bps, "collateralType": c.collateral_type, "collateralCcy": c.collateral_ccy,
            "marginPct": c.margin_pct, "term": c.term, "termEndDate": c.term_end.isoformat() if c.term_end else None,
            "settlementDate": c.settle_date.isoformat(), "legalAgreement": c.legal_agreement,
            "bookingEntity": c.booking_entity}


def contract_json(c: Contract) -> dict[str, Any]:
    inst = instrument_by_id(c.instrument_id)
    px = market.price(inst, clock.now())
    cp = counterparty_by_code(c.counterparty)
    return {
        "contractId": c.contract_id, "uti": c.uti, "venue": c.venue, "venueRef": c.venue_ref,
        "clientRef": c.client_ref, "status": c.status, "side": c.side,
        "counterparty": {"code": cp.code, "name": cp.name, "lei": cp.lei} if cp else {"code": c.counterparty},
        "instrumentId": c.instrument_id, "isin": c.isin, "composite": inst.composite,
        "quantity": c.qty, "openQuantity": c.open_qty, "feeBps": c.fee_bps, "priceAtTrade": c.price,
        "price": px, "ccy": c.ccy, "loanValue": round(c.open_qty * px, 2),
        "collateralType": c.collateral_type, "collateralCcy": c.collateral_ccy, "marginPct": c.margin_pct,
        "collateralValue": round(c.collateral_value, 2), "term": c.term,
        "termEndDate": c.term_end.isoformat() if c.term_end else None, "tradeDate": c.trade_date.isoformat(),
        "settlementDate": c.settle_date.isoformat(), "legalAgreement": c.legal_agreement,
        "bookingEntity": c.booking_entity, "bookedAt": clock.iso(c.booked_at), "history": c.history,
    }


def _hist(c: Contract, event: str, **data: Any) -> None:
    c.history = [*c.history, {"ts": clock.iso(clock.now()), "event": event, **data}]


async def _contract(s, ref: str, lock: bool = False) -> Contract:
    q = select(Contract).where((Contract.contract_id == ref) | (Contract.uti == ref) | (Contract.venue_ref == ref))
    if lock:
        q = q.with_for_update()
    c = (await s.execute(q.order_by(Contract.id).limit(1))).scalar()
    if c is None:
        raise HTTPException(404, f"no contract {ref}")
    return c


# -- booking -------------------------------------------------------------------------------------
@app.post("/trades", tags=["booking"], status_code=201)
async def book(body: dict[str, Any], who: str = Depends(client("trades:write"))) -> dict:
    """Book a trade. Accepts the flat ``TradeIn`` form or a CDM-shaped document (has ``economicTerms``)."""
    cdm = body if "economicTerms" in body else None
    t = TradeIn.model_validate(cdm_to_flat(body) if cdm else body)
    inst = resolve(t.instrument)
    if inst is None:
        raise HTTPException(422, f"unknown instrument {t.instrument!r}")
    cp = counterparty_by_code(t.counterparty)
    if cp is None:
        raise HTTPException(422, f"unknown counterparty {t.counterparty!r}")
    if t.term == "TERM" and not t.termEndDate:
        raise HTTPException(422, "termEndDate required for TERM")
    async with ctx.session() as s:
        if t.clientRef:
            dup = (await s.execute(select(Contract).where(Contract.client_ref == t.clientRef))).scalar()
            if dup:
                return {"contractId": dup.contract_id, "status": "DUPLICATE", "contract": contract_json(dup)}
        now = clock.now()
        px = t.price or market.price(inst, now)
        submitted = {**t.model_dump(mode="json"), "isin": inst.isin, "counterparty": cp.code}
        booked = dict(submitted)
        injected = _maybe_break(booked, inst.country)
        c = Contract(
            contract_id="pending", uti=t.uti, venue=t.venue, venue_ref=t.venueRef, client_ref=t.clientRef,
            side=t.side, counterparty=cp.code, instrument_id=inst.id, isin=inst.isin, qty=booked["quantity"],
            open_qty=booked["quantity"], fee_bps=booked["feeBps"], price=px, ccy=inst.ccy,
            collateral_type=t.collateralType, collateral_ccy=t.collateralCcy.upper(), margin_pct=t.marginPct,
            collateral_value=booked["quantity"] * px * t.marginPct / 100, term=t.term, term_end=t.termEndDate,
            trade_date=t.tradeDate or calendar.trade_date(now),
            settle_date=date.fromisoformat(booked["settlementDate"]), legal_agreement=t.legalAgreement,
            booking_entity=t.bookingEntity, status="OPEN", submitted=submitted, cdm=cdm, injected_break=injected,
            booked_at=now, history=[],
        )
        s.add(c)
        await s.flush()
        c.contract_id = f"SFP-C-{c.id:08d}"
        _hist(c, "BOOKED")
        await ctx.events.emit(s, "contract_booked", "contract", c.contract_id, contract_json(c), topic="contracts")
        await s.commit()
    return {"contractId": c.contract_id, "status": "BOOKED", "contract": contract_json(c)}


def _maybe_break(booked: dict[str, Any], country: str) -> dict[str, Any] | None:
    r = rng.stream("sfp", "breaks")
    roll = r.random()
    forced = _flags["force_break_next"]
    if not forced and roll >= _flags["break_rate"]:
        return None
    _flags["force_break_next"] = False
    field = r.choice(("feeBps", "quantity", "settlementDate"))
    before = booked[field]
    if field == "feeBps":
        booked[field] = round(before + r.choice((-5, -1, 1, 5)), 1)
    elif field == "quantity":
        booked[field] = max(before + r.choice((-1000, -100, 100, 1000)), 1)
    else:
        booked[field] = calendar.add_business_days(date.fromisoformat(before), 1, country).isoformat()
    return {"field": field, "submitted": before, "booked": booked[field]}


@app.get("/contracts", tags=["booking"])
async def contracts(status: str | None = None, instrument: str | None = None, limit: int = Query(500, le=10_000),
                    who: str = Depends(client("contracts:read"))) -> list[dict]:
    async with ctx.session() as s:
        q = select(Contract).order_by(Contract.id).limit(limit)
        if status:
            q = q.where(Contract.status == status.upper())
        if instrument:
            inst = resolve(instrument)
            q = q.where(Contract.instrument_id == (inst.id if inst else "?"))
        return [contract_json(c) for c in (await s.execute(q)).scalars()]


@app.get("/contracts/{ref}", tags=["booking"])
async def contract(ref: str, who: str = Depends(client("contracts:read"))) -> dict:
    async with ctx.session() as s:
        return contract_json(await _contract(s, ref))


@app.get("/positions", tags=["positions"])
async def positions(bookingEntity: str | None = None, who: str = Depends(client("contracts:read"))) -> list[dict]:
    async with ctx.session() as s:
        q = select(Contract).where(Contract.status == "OPEN")
        if bookingEntity:
            q = q.where(Contract.booking_entity == bookingEntity)
        agg: dict[tuple, dict[str, Any]] = {}
        for c in (await s.execute(q)).scalars():
            key = (c.instrument_id, c.side, c.booking_entity)
            a = agg.setdefault(key, {"instrumentId": c.instrument_id, "isin": c.isin, "side": c.side,
                                     "bookingEntity": c.booking_entity, "openQuantity": 0, "contracts": 0,
                                     "_fee_qty": 0.0, "ccy": c.ccy})
            a["openQuantity"] += c.open_qty
            a["contracts"] += 1
            a["_fee_qty"] += c.fee_bps * c.open_qty
        out = []
        for a in agg.values():
            inst = instrument_by_id(a["instrumentId"])
            fee_qty = a.pop("_fee_qty")
            px = market.price(inst, clock.now())
            out.append({**a, "composite": inst.composite, "avgFeeBps": round(fee_qty / a["openQuantity"], 2)
                        if a["openQuantity"] else None, "price": px, "loanValue": round(a["openQuantity"] * px, 2)})
        return sorted(out, key=lambda x: (x["bookingEntity"], x["instrumentId"], x["side"]))


# -- compare / breaks / amend -----------------------------------------------------------------------
class CompareIn(BaseModel):
    contractId: str | None = Field(None, description="Contract ID, UTI or venue ref; inferred from trade if omitted")
    trade: dict[str, Any] = Field(..., description="Our view of the trade: flat TradeIn fields or CDM-shaped")


def _norm(field: str, v: Any) -> Any:
    if v is None:
        return None
    if field in ("quantity",):
        return int(v)
    if field in ("feeBps", "marginPct"):
        return round(float(v), 4)
    if field in ("collateralCcy",):
        return str(v).upper()
    return str(v)


@app.post("/compare", tags=["reconciliation"])
async def compare(body: CompareIn, who: str = Depends(client("reconcile"))) -> dict:
    ours = cdm_to_flat(body.trade) if "economicTerms" in body.trade else dict(body.trade)
    inst = resolve(str(ours.get("instrument") or ours.get("isin") or ""))
    if inst:
        ours["isin"] = inst.isin
    cp = counterparty_by_code(str(ours.get("counterparty") or ""))
    if cp:
        ours["counterparty"] = cp.code
    ref = body.contractId or ours.get("uti") or ours.get("venueRef") or ours.get("clientRef")
    if not ref:
        raise HTTPException(422, "give contractId, or a trade with uti/venueRef/clientRef")
    async with ctx.session() as s:
        c = await _contract(s, str(ref))
        booked = flat_of(c)
        diffs = [{"field": f, "ours": ours.get(f), "processor": booked.get(f)}
                 for f in COMPARE_FIELDS if f in ours and _norm(f, ours.get(f)) != _norm(f, booked.get(f))]
        result: dict[str, Any] = {"contractId": c.contract_id, "status": "MATCHED" if not diffs else "UNMATCHED",
                                  "diffs": diffs, "comparedAt": clock.iso(clock.now())}
        if diffs:
            existing = (await s.execute(select(Break).where(Break.contract_id == c.contract_id,
                                                            Break.status == "OPEN"))).scalar()
            if existing is None:
                b = Break(break_id="pending", contract_id=c.contract_id, source="COMPARE", diffs=diffs, status="OPEN",
                          opened_at=clock.now())
                s.add(b)
                await s.flush()
                b.break_id = f"SFP-B-{b.id:06d}"
                await ctx.events.emit(s, "break_opened", "break", b.break_id, _break_json(b), topic="breaks")
                existing = b
            result["breakId"] = existing.break_id
        await s.commit()
    return result


def _break_json(b: Break) -> dict[str, Any]:
    return {"breakId": b.break_id, "contractId": b.contract_id, "source": b.source, "diffs": b.diffs,
            "status": b.status, "openedAt": clock.iso(b.opened_at),
            "resolvedAt": clock.iso(b.resolved_at) if b.resolved_at else None}


@app.get("/breaks", tags=["reconciliation"])
async def breaks(status: str | None = None, who: str = Depends(client("contracts:read"))) -> list[dict]:
    async with ctx.session() as s:
        q = select(Break).order_by(Break.id)
        if status:
            q = q.where(Break.status == status.upper())
        return [_break_json(b) for b in (await s.execute(q)).scalars()]


class AmendIn(BaseModel):
    fields: dict[str, Any] = Field(..., description="e.g. {'feeBps': 650} or {'settlementDate': '2026-10-28'}")
    reason: str = ""


_AMENDABLE = {"feeBps": "fee_bps", "quantity": "qty", "settlementDate": "settle_date", "marginPct": "margin_pct",
              "collateralType": "collateral_type", "collateralCcy": "collateral_ccy", "termEndDate": "term_end",
              "legalAgreement": "legal_agreement", "bookingEntity": "booking_entity"}


@app.post("/contracts/{ref}/amend", tags=["reconciliation"])
async def amend(ref: str, body: AmendIn, who: str = Depends(client("contracts:write"))) -> dict:
    async with ctx.session() as s:
        c = await _contract(s, ref, lock=True)
        changes = {}
        for k, v in body.fields.items():
            col = _AMENDABLE.get(k)
            if col is None:
                raise HTTPException(422, f"{k} is not amendable; allowed: {sorted(_AMENDABLE)}")
            if col in ("settle_date", "term_end"):
                v = date.fromisoformat(v) if v else None
            before = getattr(c, col)
            if col == "qty":
                c.open_qty += int(v) - c.qty
                v = int(v)
            setattr(c, col, v)
            changes[k] = {"from": before.isoformat() if isinstance(before, date) else before,
                          "to": v.isoformat() if isinstance(v, date) else v}
        _hist(c, "AMENDED", changes=changes, reason=body.reason)
        await ctx.events.emit(s, "contract_amended", "contract", c.contract_id, contract_json(c), topic="contracts")
        booked = flat_of(c)
        resolved = []
        for b in (await s.execute(select(Break).where(Break.contract_id == c.contract_id,
                                                      Break.status == "OPEN"))).scalars():
            if all(_norm(d["field"], d["ours"]) == _norm(d["field"], booked.get(d["field"])) for d in b.diffs):
                b.status, b.resolved_at = "RESOLVED", clock.now()
                resolved.append(b.break_id)
                await ctx.events.emit(s, "break_resolved", "break", b.break_id, _break_json(b), topic="breaks")
        await s.commit()
        return {"contract": contract_json(c), "changes": changes, "resolvedBreaks": resolved}


# -- lifecycle ------------------------------------------------------------------------------------
class LifecycleIn(BaseModel):
    event: Literal["RETURN", "RECALL", "RERATE", "MARK", "BUY_IN"]
    quantity: int | None = Field(None, gt=0)
    feeBps: float | None = Field(None, ge=0)
    price: float | None = Field(None, gt=0, description="BUY_IN: what the lender paid a share")
    effectiveDate: date | None = None


@app.post("/lifecycle/{ref}", tags=["lifecycle"])
async def lifecycle(ref: str, body: LifecycleIn, who: str = Depends(client("contracts:write"))) -> dict:
    async with ctx.session() as s:
        c = await _contract(s, ref, lock=True)
        if c.status != "OPEN":
            raise HTTPException(409, f"contract is {c.status}")
        extra: dict[str, Any] = {}
        if body.event in ("RETURN", "RECALL", "BUY_IN"):
            q = body.quantity or c.open_qty
            if q > c.open_qty:
                raise HTTPException(422, f"only {c.open_qty} open")
            c.open_qty -= q
            c.collateral_value = c.collateral_value * (c.open_qty / (c.open_qty + q))
            if c.open_qty == 0:
                c.status = "CLOSED"
            _hist(c, body.event, quantity=q, remaining=c.open_qty, effectiveDate=_iso(body.effectiveDate),
                  **({"price": body.price} if body.event == "BUY_IN" else {}))
            if body.event == "RETURN":
                extra["recallsAnswered"] = await _answer_recalls(s, c, q)
        elif body.event == "RERATE":
            if body.feeBps is None:
                raise HTTPException(422, "feeBps required for RERATE")
            _hist(c, "RERATE", fromBps=c.fee_bps, toBps=body.feeBps, effectiveDate=_iso(body.effectiveDate))
            c.fee_bps = body.feeBps
        else:
            extra["mark"] = await _mark(s, c)
        await ctx.events.emit(s, f"contract_{body.event.lower()}", "contract", c.contract_id, contract_json(c),
                              topic="contracts")
        await s.commit()
        return {"contract": contract_json(c), **extra}


# -- recalls from lenders ---------------------------------------------------------------------------
# Venue A fills and bilateral trades have no venue lifecycle: the processor contract is the loan, so a lender's
# recall on one arrives here. (Venue B loans are recalled on the venue.)
def recall_json(n: RecallNotice) -> dict[str, Any]:
    inst = instrument_by_id(n.instrument_id)
    return {"recallId": n.recall_id, "contractId": n.contract_id, "counterparty": n.counterparty,
            "instrumentId": n.instrument_id, "composite": inst.composite if inst else None, "quantity": n.qty,
            "outstanding": n.outstanding, "noticeDate": n.notice_date.isoformat(), "dueDate": n.due_date.isoformat(),
            "status": n.status, "overdue": n.status == "OPEN" and n.due_date < calendar.trade_date(clock.now()),
            "boughtIn": n.bought_in_qty or 0, "ts": clock.iso(n.ts)}


async def _answer_recalls(s, c: Contract, q: int) -> list[str]:
    """A return answers the contract's open recalls, oldest first."""
    done = []
    notices = (await s.execute(select(RecallNotice).where(RecallNotice.contract_id == c.contract_id,
                                                          RecallNotice.status == "OPEN")
                               .order_by(RecallNotice.id))).scalars().all()
    for n in notices:
        if q <= 0:
            break
        take = min(q, n.outstanding)
        n.outstanding -= take
        q -= take
        if n.outstanding == 0:
            n.status = "RETURNED"
            done.append(n.recall_id)
            await ctx.events.emit(s, "recall_returned", "recall", n.recall_id, recall_json(n), topic="contracts")
    return done


# -- buy-ins: a recall not returned in time ----------------------------------------------------------
BUY_IN_AFTER_DAYS = 1  # business days past the due date before a lender buys in (verb ``buy_in_after``)
_buy_ins = {"afterDays": BUY_IN_AFTER_DAYS}


def buy_in_json(b: BuyIn) -> dict[str, Any]:
    inst = instrument_by_id(b.instrument_id)
    return {"buyInId": b.buy_in_id, "recallId": b.recall_id, "contractId": b.contract_id,
            "counterparty": b.counterparty, "instrumentId": b.instrument_id,
            "composite": inst.composite if inst else None, "quantity": b.qty, "price": b.price, "ccy": b.ccy,
            "cost": b.cost, "collateralKept": b.collateral_kept, "amountDue": b.amount_due, "ts": clock.iso(b.ts)}


async def _buy_in(s, n: RecallNotice, qty: int | None = None, price: float | None = None) -> BuyIn:
    """The lender buys the recalled shares in the market at our cost: the contract closes for them."""
    c = (await s.execute(select(Contract).where(Contract.contract_id == n.contract_id).with_for_update())).scalar()
    qty = min(qty or n.outstanding, n.outstanding, c.open_qty)
    if qty <= 0:
        raise HTTPException(409, f"{n.recall_id} has nothing left to buy in")
    inst = instrument_by_id(c.instrument_id)
    r = rng.stream("sfp", f"buy-in:{n.recall_id}")
    px = round(price or market.price(inst, clock.now()) * (1 + r.uniform(0.005, 0.03)), 4)  # paid up to get them
    kept = round(c.collateral_value * qty / c.open_qty, 2)
    c.open_qty -= qty
    c.collateral_value = round(c.collateral_value - kept, 2)
    if c.open_qty == 0:
        c.status = "CLOSED"
    n.outstanding -= qty
    n.bought_in_qty = (n.bought_in_qty or 0) + qty
    if n.outstanding == 0:
        n.status = "BOUGHT_IN"
    b = BuyIn(buy_in_id="pending", recall_id=n.recall_id, contract_id=c.contract_id, counterparty=c.counterparty,
              instrument_id=c.instrument_id, qty=qty, price=px, ccy=c.ccy, cost=round(qty * px, 2),
              collateral_kept=kept, amount_due=round(qty * px - kept, 2), ts=clock.now())
    s.add(b)
    await s.flush()
    b.buy_in_id = f"SFP-B-{b.id:06d}"
    _hist(c, "BUY_IN", buyInId=b.buy_in_id, recallId=n.recall_id, quantity=qty, price=px, remaining=c.open_qty)
    await ctx.events.emit(s, "buy_in", "buy_in", b.buy_in_id, buy_in_json(b), topic="contracts")
    return b


async def _auto_buy_ins() -> None:
    """Lenders buy in recalls still open ``afterDays`` business days past their due date, in market hours."""
    now = clock.now()
    today = calendar.trade_date(now)
    if _buy_ins["afterDays"] is None or not calendar.is_business_day(today) or not (
            dtime(9, 30) <= now.astimezone(clock.NEW_YORK).time() < calendar.MARKET_CLOSE):
        return
    async with ctx.session() as s:
        for n in (await s.execute(select(RecallNotice).where(RecallNotice.status == "OPEN")
                                  .order_by(RecallNotice.id))).scalars().all():
            if calendar.add_business_days(n.due_date, _buy_ins["afterDays"]) <= today:
                await _buy_in(s, n)
        await s.commit()


ctx.scheduler.every(60, "buy-ins", _auto_buy_ins)


@app.get("/buy-ins", tags=["lifecycle"])
async def buy_ins(after: int = 0, contractId: str | None = None,
                  who: str = Depends(client("contracts:read"))) -> list[dict]:
    """Lenders' buy-ins on our overdue recalls, oldest first (``after``: the last ``seq`` you've seen)."""
    async with ctx.session() as s:
        q = select(BuyIn).where(BuyIn.id > after).order_by(BuyIn.id)
        if contractId:
            q = q.where(BuyIn.contract_id == contractId)
        return [{**buy_in_json(b), "seq": b.id} for b in (await s.execute(q)).scalars()]


@ctx.verb("buy_in")
async def verb_buy_in(args: dict[str, Any]) -> dict:
    """A lender buys in a recall now: {recallId?, quantity?, price?}. Default the oldest open recall."""
    async with ctx.session() as s:
        q = select(RecallNotice).where(RecallNotice.status == "OPEN").order_by(RecallNotice.id)
        if args.get("recallId"):
            q = q.where(RecallNotice.recall_id == args["recallId"])
        n = (await s.execute(q.limit(1))).scalar()
        if n is None:
            raise HTTPException(404, "no open recall matches")
        b = await _buy_in(s, n, int(args["quantity"]) if args.get("quantity") else None,
                          float(args["price"]) if args.get("price") else None)
        await s.commit()
        return buy_in_json(b)


@ctx.verb("buy_in_after")
async def verb_buy_in_after(args: dict[str, Any]) -> dict:
    """How many business days past due a lender waits to buy in: {days} (default 1; null never)."""
    days = args.get("days", BUY_IN_AFTER_DAYS)
    _buy_ins["afterDays"] = None if days is None else max(int(days), 0)
    return dict(_buy_ins)


@app.get("/recalls", tags=["lifecycle"])
async def recalls(status: str | None = None, contractId: str | None = None, after: int = 0,
                  who: str = Depends(client("contracts:read"))) -> list[dict]:
    """Lenders' recalls on contracts where we borrow (``after``: the last ``seq`` you've seen)."""
    async with ctx.session() as s:
        q = select(RecallNotice).where(RecallNotice.id > after).order_by(RecallNotice.id)
        if status:
            q = q.where(RecallNotice.status == status.upper())
        if contractId:
            q = q.where(RecallNotice.contract_id == contractId)
        return [{**recall_json(n), "seq": n.id} for n in (await s.execute(q)).scalars()]


@ctx.verb("recall")
async def verb_recall(args: dict[str, Any]) -> dict:
    """A lender recalls shares it lent us: {contractId? | instrument?, counterparty?, quantity?, dueDays? (1)}"""
    async with ctx.session() as s:
        q = select(Contract).where(Contract.status == "OPEN", Contract.side == "BORROW",
                                   Contract.venue != "VENUE_B").order_by(Contract.id)
        if args.get("contractId"):
            q = q.where(Contract.contract_id == args["contractId"])
        if args.get("instrument"):
            inst = resolve(str(args["instrument"]))
            q = q.where(Contract.instrument_id == (inst.id if inst else "?"))
        if args.get("counterparty"):
            q = q.where(Contract.counterparty == str(args["counterparty"]).upper())
        rows = (await s.execute(q)).scalars().all()
        if not rows:
            raise HTTPException(404, "no open borrow at the processor matches "
                                     "(Venue B loans are recalled on the venue)")
        c = rows[0] if (args.get("contractId") or args.get("instrument")) else rng.stream("sfp", "recalls").choice(rows)
        owed = sum((await s.execute(select(RecallNotice.outstanding).where(
            RecallNotice.contract_id == c.contract_id, RecallNotice.status == "OPEN"))).scalars())
        qty = min(int(args.get("quantity") or c.open_qty), c.open_qty - owed)
        if qty <= 0:
            raise HTTPException(409, f"{c.contract_id} is recalled in full already")
        today = calendar.trade_date(clock.now())
        n = RecallNotice(recall_id="pending", contract_id=c.contract_id, counterparty=c.counterparty,
                         instrument_id=c.instrument_id, qty=qty, outstanding=qty, notice_date=today,
                         due_date=calendar.add_business_days(today, int(args.get("dueDays", 1))), status="OPEN",
                         ts=clock.now())
        s.add(n)
        await s.flush()
        n.recall_id = f"SFP-R-{n.id:06d}"
        _hist(c, "RECALL_NOTICE", recallId=n.recall_id, quantity=qty, dueDate=n.due_date.isoformat())
        await ctx.events.emit(s, "recall_notice", "recall", n.recall_id, recall_json(n), topic="contracts")
        await s.commit()
        return recall_json(n)


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


async def _mark(s, c: Contract) -> dict[str, Any]:
    inst = instrument_by_id(c.instrument_id)
    now = clock.now()
    px = market.price(inst, now)
    loan_value = c.open_qty * px
    required = loan_value * c.margin_pct / 100
    call = round(required - c.collateral_value, 2)
    m = Mark(contract_id=c.contract_id, mark_date=calendar.trade_date(now), price=px, loan_value=round(loan_value, 2),
             required_collateral=round(required, 2), posted_collateral=round(c.collateral_value, 2), call_amount=call,
             ts=now)
    s.add(m)
    c.collateral_value = required  # assume the call settles
    out = {"contractId": c.contract_id, "markDate": m.mark_date.isoformat(), "price": px,
           "loanValue": m.loan_value, "requiredCollateral": m.required_collateral,
           "postedCollateral": m.posted_collateral, "callAmount": call, "ccy": c.collateral_ccy}
    if abs(call) >= 1:
        # Lender calls for more collateral when the loan value rises; borrower when it falls.
        caller = ("LENDER" if call > 0 else "BORROWER")
        we_call = (caller == "LENDER") == (c.side == "LEND")
        out["marginCall"] = {
            "direction": "WE_CALL" if we_call else "WE_ARE_CALLED",
            "colr003": {  # ISO 20022 colr.003-shaped (MarginCallRequest), simplified
                "MrgnCallReqId": f"MC-{c.contract_id}-{m.mark_date:%Y%m%d}",
                "Oblgtn": {"PtyA": OUR_FIRM["lei"], "PtyB": counterparty_by_code(c.counterparty).lei,
                           "CollAcctId": c.booking_entity, "ValtnDt": m.mark_date.isoformat()},
                "MrgnCallRslt": {"MrgnCallAmt": {"Amt": abs(call), "Ccy": c.collateral_ccy},
                                 "DlvrRtrInd": "DLVR" if call > 0 else "RTRN"},
                "Expsr": {"ExpsrTp": "SLEB", "LnVal": m.loan_value, "MrgnPct": c.margin_pct},
            },
        }
    return out


async def run_marks() -> dict[str, Any]:
    async with ctx.session() as s:
        cs = (await s.execute(select(Contract).where(Contract.status == "OPEN").order_by(Contract.id)
                              .with_for_update())).scalars().all()
        calls = []
        for c in cs:
            out = await _mark(s, c)
            if "marginCall" in out:
                calls.append(out)
                await ctx.events.emit(s, "margin_call", "contract", c.contract_id, out, topic="marks")
        await s.commit()
    return {"marked": len(cs), "marginCalls": len(calls), "calls": calls}


@app.post("/marks/run", tags=["lifecycle"])
async def marks_run(who: str = Depends(client("marks:run"))) -> dict:
    return await run_marks()


@app.get("/marks", tags=["lifecycle"])
async def marks(contractId: str | None = None, who: str = Depends(client("marks:read"))) -> list[dict]:
    async with ctx.session() as s:
        q = select(Mark).order_by(Mark.id)
        if contractId:
            q = q.where(Mark.contract_id == contractId)
        return [{"contractId": m.contract_id, "markDate": m.mark_date.isoformat(), "price": m.price,
                 "loanValue": m.loan_value, "requiredCollateral": m.required_collateral,
                 "postedCollateral": m.posted_collateral, "callAmount": m.call_amount, "ts": clock.iso(m.ts)}
                for m in (await s.execute(q)).scalars()]


# -- margin calls -----------------------------------------------------------------------------------
NS_COLR003 = "urn:iso:std:iso:20022:tech:xsd:colr.003.001.05"


def _call_json(m: Mark, c: Contract) -> dict[str, Any]:
    caller = "LENDER" if m.call_amount > 0 else "BORROWER"
    we_call = (caller == "LENDER") == (c.side == "LEND")
    return {"callId": f"MC-{m.id:08d}", "contractId": c.contract_id, "uti": c.uti, "markDate": m.mark_date.isoformat(),
            "direction": "WE_CALL" if we_call else "WE_ARE_CALLED", "amount": abs(m.call_amount),
            "ccy": c.collateral_ccy, "deliverOrReturn": "DLVR" if m.call_amount > 0 else "RTRN",
            "loanValue": m.loan_value, "requiredCollateral": m.required_collateral,
            "postedCollateral": m.posted_collateral, "counterparty": counterparty_by_code(c.counterparty).lei,
            "bookingEntity": c.booking_entity}


async def _calls(s, call_id: str | None = None) -> list[tuple[Mark, Contract]]:
    q = select(Mark, Contract).join(Contract, Contract.contract_id == Mark.contract_id).where(
        (Mark.call_amount >= 1) | (Mark.call_amount <= -1)).order_by(Mark.id)
    if call_id:
        if not call_id.startswith("MC-") or not call_id[3:].isdigit():
            raise HTTPException(404, f"no margin call {call_id}")
        q = q.where(Mark.id == int(call_id[3:]))
    return [(m, c) for m, c in (await s.execute(q)).all()]


@app.get("/margin-calls", tags=["collateral"])
async def margin_calls(since: date | None = None, who: str = Depends(client("marks:read"))) -> list[dict]:
    """Every margin call from the marks, newest last."""
    async with ctx.session() as s:
        return [_call_json(m, c) for m, c in await _calls(s) if since is None or m.mark_date >= since]


@app.get("/margin-calls/{call_id}/colr.003", tags=["collateral"], response_class=Response)
async def margin_call_colr003(call_id: str, who: str = Depends(client("marks:read"))) -> Response:
    """The margin call as an ISO 20022 colr.003 MarginCallRequest (simplified subset)."""
    from xml.etree import ElementTree as ET

    async with ctx.session() as s:
        rows = await _calls(s, call_id)
    if not rows:
        raise HTTPException(404, f"no margin call {call_id}")
    j = _call_json(*rows[0])
    ET.register_namespace("", NS_COLR003)

    def sub(parent: ET.Element, tag: str, text: Any = None, **attrs: str) -> ET.Element:
        el = ET.SubElement(parent, f"{{{NS_COLR003}}}{tag}", attrs)
        if text is not None:
            el.text = str(text)
        return el

    doc = ET.Element(f"{{{NS_COLR003}}}Document")
    req = sub(doc, "MrgnCallReq")
    sub(req, "TxId", j["callId"])
    ob = sub(req, "Oblgtn")
    caller_is_us = j["direction"] == "WE_CALL"
    us, them = OUR_FIRM["lei"], j["counterparty"]
    sub(sub(sub(ob, "PtyA"), "Id"), "LEI", us if caller_is_us else them)
    sub(sub(sub(ob, "PtyB"), "Id"), "LEI", them if caller_is_us else us)
    sub(sub(ob, "CollAcctId"), "Id", j["bookingEntity"])
    sub(sub(ob, "ValtnDt"), "Dt", j["markDate"])
    rslt = sub(sub(req, "MrgnCallRslt"), "MrgnCallAmt")
    sub(rslt, "Amt", f"{j['amount']:.2f}", Ccy=j["ccy"])
    sub(rslt, "DlvrRtrInd", j["deliverOrReturn"])
    ex = sub(req, "Expsr")
    sub(ex, "ExpsrTp", "SLEB")  # securities lending and borrowing
    sub(ex, "LnVal", f"{j['loanValue']:.2f}", Ccy=j["ccy"])
    sub(ex, "ReqrdCollVal", f"{j['requiredCollateral']:.2f}", Ccy=j["ccy"])
    sub(ex, "PstdCollVal", f"{j['postedCollateral']:.2f}", Ccy=j["ccy"])
    sub(ex, "CtrctRef", j["uti"] or j["contractId"])
    ET.indent(doc)
    return Response(ET.tostring(doc, encoding="UTF-8", xml_declaration=True), media_type="application/xml")


@app.websocket("/stream")
async def stream(ws: WebSocket, topics: str = "contracts,breaks,marks") -> None:
    """Live events: contracts (booked, amended, lifecycle), breaks (opened, resolved), marks (margin calls).
    Pass the key as ``?key=``; contracts and breaks need ``contracts:read``, marks ``marks:read``."""
    wanted = [t.strip() for t in topics.split(",") if t.strip()]
    need = {"contracts": "contracts:read", "breaks": "contracts:read", "marks": "marks:read"}
    if await ctx.gate.websocket(ws, *{need[t] for t in wanted if t in need}) is None:
        return
    await ctx.ws_stream(ws, wanted)


# -- end-of-day file -------------------------------------------------------------------------------
EOD_COLUMNS = ("contract_id", "uti", "venue_ref", "booking_entity", "side", "counterparty_lei", "isin",
               "open_quantity", "fee_bps", "collateral_type", "collateral_ccy", "margin_pct", "collateral_value",
               "settlement_date", "status")


@app.get("/eod/contracts.csv", tags=["eod"], response_class=PlainTextResponse)
async def eod_file(asOf: str | None = None, who: str = Depends(client("eod:read"))) -> str:
    as_of = date.fromisoformat(asOf) if asOf else calendar.trade_date(clock.now())
    async with ctx.session() as s:
        rows = (await s.execute(select(Contract).where(Contract.trade_date <= as_of).order_by(Contract.id))).scalars()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EOD_COLUMNS)
        rows = list(rows)
        victim = None
        if _flags["eod_break"] and rows:
            victim = rng.fresh("sfp", "eod", as_of.isoformat()).choice([c.contract_id for c in rows])
        for c in rows:
            qty, fee = c.open_qty, c.fee_bps
            if c.contract_id == victim:
                qty = max(qty - 1000, 0)  # batch file disagrees with the online books
            w.writerow((c.contract_id, c.uti or "", c.venue_ref or "", c.booking_entity, c.side,
                        counterparty_by_code(c.counterparty).lei, c.isin, qty, fee, c.collateral_type,
                        c.collateral_ccy, c.margin_pct, round(c.collateral_value, 2), c.settle_date.isoformat(),
                        c.status))
    return buf.getvalue()


# -- admin -------------------------------------------------------------------------------------------
async def on_reset(c: ServiceContext) -> None:
    _flags.update(break_rate=0.03, force_break_next=False, eod_break=False)
    _buy_ins["afterDays"] = BUY_IN_AFTER_DAYS


async def _nightly_marks() -> None:
    await run_marks()


spec.on_reset = on_reset
# Daily at 00:00Z (20:00 New York during EDT).
ctx.scheduler.every(86_400, "nightly-marks", _nightly_marks)


@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {"header": "X-Api-Key", "apiKey": ctx.gate.bootstrap_key(OUR_FIRM["code"]),
            "readOnlyKey": ctx.gate.bootstrap_key("ops-readonly")}


@app.get("/_admin/flags", tags=["admin"])
async def get_flags() -> dict:
    return dict(_flags)


@app.put("/_admin/flags", tags=["admin"])
async def put_flags(flags: dict[str, Any]) -> dict:
    unknown = set(flags) - set(_flags)
    if unknown:
        raise HTTPException(422, f"unknown flags {sorted(unknown)}")
    _flags.update(flags)
    return dict(_flags)


@ctx.verb("flags")
async def verb_flags(args: dict[str, Any]) -> dict:
    """Set processor flags: {break_rate, force_break_next, eod_break}"""
    return await put_flags(args)


@ctx.verb("marks")
async def verb_marks(args: dict[str, Any]) -> dict:
    """Run the mark job now."""
    return await run_marks()


@app.get("/_admin/injected-breaks", tags=["admin"])
async def injected() -> list[dict]:
    """Which bookings were deliberately broken (the answer key for reconciliation tests)."""
    async with ctx.session() as s:
        rows = (await s.execute(select(Contract).order_by(Contract.id))).scalars()
        return [{"contractId": c.contract_id, **c.injected_break} for c in rows if c.injected_break]

