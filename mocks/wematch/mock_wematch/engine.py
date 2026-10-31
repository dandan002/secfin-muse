"""Venue B matching engine: interest lists, a deterministic optimiser, negotiation and loan lifecycle.

Optimiser: for every open line (largest notional first), take compatible dealer lines on the
other side, best price for us first, and propose matches at the midpoint of the overlapping
fee range. Partial fills are allowed down to the line's minimum quantity. Simulated dealers
then accept, counter or reject each proposal after a latency.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mockcore import clock, rng
from mockcore.app import ServiceContext
from refdata import calendar, market
from refdata.counterparties import counterparties, counterparty_by_code
from refdata.universe import instrument_by_id, resolve, universe

from .models import DealerLine, InterestList, ListLine, Loan, Match

FX_USD = {"USD": 1.0, "GBP": 1.27, "EUR": 1.08, "JPY": 0.0067}
MAX_LINES = 10_000


class EngineConfig(BaseModel):
    dealer_latency_s: tuple[float, float] = (3.0, 15.0)
    dealer_accept_p: float = 0.8
    max_rounds: int = 3
    proposal_ttl_s: int = 1800
    optimiser_every_s: float = 300.0
    buy_in_after_days: int | None = 1  # business days past a recall's due date before the lender buys in


config = EngineConfig()


class LineIn(BaseModel):
    instrument: str
    side: Literal["BORROW", "LEND"]
    quantity: int = Field(..., gt=0)
    feeBps: float = Field(..., ge=0, description="BORROW: max fee we pay. LEND: min fee we accept.")
    minQuantity: int = Field(0, ge=0)
    term: Literal["OPEN", "TERM"] = "OPEN"
    collateralType: Literal["CASH", "NONCASH"] = "NONCASH"
    collateralCcy: str = "USD"


class ListIn(BaseModel):
    name: str = "interest list"
    product: Literal["SBL", "TRS", "REPO"] = "SBL"
    clientRef: str | None = None
    autoAccept: bool = Field(False, description="Accept the optimiser's proposal on our side automatically")
    lines: list[LineIn] = Field(..., min_length=1, max_length=MAX_LINES)


class EngineError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code, self.status = code, status


# -- serializers ---------------------------------------------------------------------------------
def _cp(code: str) -> dict[str, Any]:
    cp = counterparty_by_code(code)
    return {"entity": cp.wematch_entity, "name": cp.name, "lei": cp.lei} if cp else {"entity": code}


def list_json(lst: InterestList) -> dict[str, Any]:
    return {"listId": lst.list_id, "name": lst.name, "product": lst.product, "status": lst.status,
            "lines": lst.lines, "rejectedLines": lst.rejected_lines, "clientRef": lst.client_ref,
            "autoAccept": lst.auto_accept,
            "uploadedAt": clock.iso(lst.uploaded_at)}


def line_json(ln: ListLine) -> dict[str, Any]:
    return {"lineId": ln.id, "lineNo": ln.line_no, "instrumentId": ln.instrument_id, "identifier": ln.identifier,
            "side": ln.side, "quantity": ln.qty, "matchedQuantity": ln.matched_qty, "feeBps": ln.fee_limit,
            "minQuantity": ln.min_qty, "term": ln.term, "collateralType": ln.collateral_type,
            "collateralCcy": ln.collateral_ccy, "status": ln.status}


def match_json(m: Match) -> dict[str, Any]:
    inst = instrument_by_id(m.instrument_id)
    return {"matchId": m.match_id, "listId": m.list_id, "lineId": m.line_id, "counterparty": _cp(m.dealer),
            "instrumentId": m.instrument_id, "isin": inst.isin, "composite": inst.composite, "side": m.side,
            "quantity": m.qty, "feeBps": m.fee_bps, "proposedBy": m.proposed_by, "status": m.status,
            "ourAccept": m.our_accept, "counterpartyAccept": m.dealer_accept, "rounds": m.rounds, "term": m.term,
            "collateralType": m.collateral_type, "collateralCcy": m.collateral_ccy,
            "createdAt": clock.iso(m.created_at), "updatedAt": clock.iso(m.updated_at), "history": m.history}


def loan_json(ln: Loan) -> dict[str, Any]:
    inst = instrument_by_id(ln.instrument_id)
    px = market.price(inst, clock.now())
    return {"loanId": ln.loan_id, "matchId": ln.match_id, "instrumentId": ln.instrument_id, "isin": ln.isin,
            "composite": inst.composite, "side": ln.side, "counterparty": _cp(ln.counterparty),
            "quantityOriginal": ln.qty_original, "quantityOpen": ln.qty_open, "feeBps": ln.fee_bps,
            "pendingRerateBps": ln.pending_rerate_bps, "priceAtTrade": ln.price, "price": px, "ccy": ln.ccy,
            "notional": round(ln.qty_open * px, 2), "notionalUsd": round(ln.qty_open * px * FX_USD[ln.ccy], 2),
            "term": ln.term, "collateralType": ln.collateral_type, "collateralCcy": ln.collateral_ccy,
            "tradeDate": ln.trade_date.isoformat(), "settlementDate": ln.settle_date.isoformat(),
            "status": ln.status, "history": ln.history,
            "recall": {"quantity": ln.recall_qty, "dueDate": ln.recall_due.isoformat(),
                       "overdue": ln.recall_due < calendar.trade_date(clock.now()), "reason": ln.recall_reason,
                       "eventId": ln.recall_event}
            if ln.recall_qty and ln.recall_due else None,
            "buyIns": [{k: v for k, v in h.items() if k != "event"} for h in ln.history if h["event"] == "BOUGHT_IN"]}


def _hist(obj: Match | Loan, event: str, **data: Any) -> None:
    obj.history = [*obj.history, {"ts": clock.iso(clock.now()), "event": event, **data}]
    if isinstance(obj, Match):
        obj.updated_at = clock.now()


# -- seeding -------------------------------------------------------------------------------------
async def seed_dealers(ctx: ServiceContext) -> None:
    r = rng.fresh("wematch", "dealers")
    insts = [i for i in universe() if not i.restricted]
    weights = [{"GC": 1.0, "WARM": 3.0, "HOT": 3.0}[i.tier] for i in insts]
    rows = []
    for cp in counterparties():
        if cp.kind == "INTERNAL":
            continue
        sides = {"AGENT_LENDER": ("LEND",), "HEDGE_FUND": ("BORROW",)}.get(cp.kind, ("LEND", "BORROW"))
        for inst in r.choices(insts, weights, k=160):
            side = r.choice(sides)
            base = inst.base_fee_bps
            qty = max(int(r.lognormvariate(0, 0.8) * 400_000) // 1000 * 1000, 10_000)
            if side == "LEND":
                lo = round(base * r.uniform(0.9, 1.1), 1)
                hi = round(lo * r.uniform(1.1, 1.4), 1)
            else:
                hi = round(base * r.uniform(0.95, 1.25), 1)
                lo = round(hi * r.uniform(0.6, 0.9), 1)
            rows.append({"dealer": cp.code, "instrument_id": inst.id, "side": side, "qty": qty, "fee_min": lo,
                         "fee_max": hi, "product": "SBL"})
    async with ctx.session() as s:
        await s.execute(DealerLine.__table__.insert(), rows)
        await s.commit()


# -- lists -----------------------------------------------------------------------------------------
_CSV_ALIASES = {
    "instrument": ("instrument", "isin", "sedol", "cusip", "ticker", "security", "identifier", "figi"),
    "side": ("side", "direction", "b/l", "bl"),
    "quantity": ("quantity", "qty", "shares", "size"),
    "feeBps": ("feebps", "fee", "fee_bps", "rate", "rate_bps", "bps", "limit"),
    "minQuantity": ("minquantity", "min_qty", "minqty", "min"),
    "term": ("term",),
    "collateralType": ("collateraltype", "collateral", "coll"),
    "collateralCcy": ("collateralccy", "ccy", "currency"),
}


def parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        raise EngineError("EMPTY_FILE", "no header row")
    mapping: dict[str, str] = {}
    for col in reader.fieldnames:
        key = col.strip().lower().replace(" ", "")
        for field, aliases in _CSV_ALIASES.items():
            if key in aliases and field not in mapping.values():
                mapping[col] = field
                break
    if not {"instrument", "side", "quantity", "feeBps"} <= set(mapping.values()):
        raise EngineError("BAD_HEADER", f"need instrument, side, quantity, fee columns; got {reader.fieldnames}")
    out = []
    for row in reader:
        d: dict[str, Any] = {}
        for col, field in mapping.items():
            v = (row.get(col) or "").strip()
            if v:
                d[field] = v
        side = str(d.get("side", "")).upper()
        d["side"] = {"B": "BORROW", "L": "LEND"}.get(side, side)
        d["quantity"] = d.get("quantity", "0").replace(",", "")
        out.append(d)
    return out


async def upload(ctx: ServiceContext, s: AsyncSession, raw_lines: list[dict[str, Any]], name: str, product: str,
                 client_ref: str | None, auto_accept: bool) -> dict[str, Any]:
    if len(raw_lines) > MAX_LINES:
        raise EngineError("TOO_MANY_LINES", f"max {MAX_LINES} lines per list")
    if client_ref:
        dup = (await s.execute(select(InterestList).where(InterestList.client_ref == client_ref))).scalar()
        if dup:
            return {**list_json(dup), "duplicate": True, "errors": []}
    lst = InterestList(list_id="pending", name=name, product=product, status="ACTIVE", lines=0, rejected_lines=0,
                       uploaded_at=clock.now(), client_ref=client_ref, auto_accept=auto_accept)
    s.add(lst)
    await s.flush()
    lst.list_id = f"WM-L-{lst.id:06d}"
    errors, rows = [], []
    for n, raw in enumerate(raw_lines, start=1):
        try:
            line = LineIn.model_validate(raw)
        except Exception as e:
            errors.append({"lineNo": n, "error": str(e).splitlines()[0][:200], "input": raw})
            continue
        inst = resolve(line.instrument)
        if inst is None:
            errors.append({"lineNo": n, "error": f"unknown instrument {line.instrument!r}", "input": raw})
            continue
        rows.append({"list_id": lst.list_id, "line_no": n, "instrument_id": inst.id, "identifier": line.instrument,
                     "side": line.side, "qty": line.quantity, "matched_qty": 0, "fee_limit": line.feeBps,
                     "min_qty": line.minQuantity, "term": line.term, "collateral_type": line.collateralType,
                     "collateral_ccy": line.collateralCcy.upper(), "status": "OPEN"})
    if rows:
        await s.execute(ListLine.__table__.insert(), rows)
    lst.lines, lst.rejected_lines = len(rows), len(errors)
    await ctx.events.emit(s, "list_uploaded", "list", lst.list_id, list_json(lst), topic="lists")
    return {**list_json(lst), "errors": errors}


# -- optimiser ---------------------------------------------------------------------------------------
async def run_optimiser(ctx: ServiceContext, s: AsyncSession, list_id: str | None = None) -> dict[str, Any]:
    q = select(ListLine).join(InterestList, InterestList.list_id == ListLine.list_id).where(
        InterestList.status == "ACTIVE", ListLine.status.in_(("OPEN", "PARTIAL")))
    if list_id:
        q = q.where(ListLine.list_id == list_id)
    lines = (await s.execute(q.with_for_update(of=ListLine))).scalars().all()
    auto = dict((await s.execute(select(InterestList.list_id, InterestList.auto_accept))).all())
    now = clock.now()

    def notional(ln: ListLine) -> float:
        inst = instrument_by_id(ln.instrument_id)
        return (ln.qty - ln.matched_qty) * inst.base_px * FX_USD[inst.ccy]

    lines = sorted(lines, key=lambda ln: (-notional(ln), ln.id))
    created: list[Match] = []
    for ln in lines:
        remaining = ln.qty - ln.matched_qty
        dealer_side = "LEND" if ln.side == "BORROW" else "BORROW"
        cq = select(DealerLine).where(DealerLine.instrument_id == ln.instrument_id, DealerLine.side == dealer_side,
                                      DealerLine.qty > 0)
        if ln.side == "BORROW":
            cq = cq.where(DealerLine.fee_min <= ln.fee_limit).order_by(DealerLine.fee_min, DealerLine.qty.desc(),
                                                                       DealerLine.dealer)
        else:
            cq = cq.where(DealerLine.fee_max >= ln.fee_limit).order_by(DealerLine.fee_max.desc(),
                                                                       DealerLine.qty.desc(), DealerLine.dealer)
        for dl in (await s.execute(cq.with_for_update())).scalars():
            if remaining <= 0:
                break
            q_ = min(remaining, dl.qty)
            if q_ < max(ln.min_qty, 1):
                continue
            if ln.side == "BORROW":
                fee = round((dl.fee_min + min(dl.fee_max, ln.fee_limit)) / 2, 1)
            else:
                fee = round((ln.fee_limit + dl.fee_max) / 2, 1)
            m = Match(match_id="pending", list_id=ln.list_id, line_id=ln.id, dealer=dl.dealer, dealer_line_id=dl.id,
                      instrument_id=ln.instrument_id, side=ln.side, qty=q_, fee_bps=fee, proposed_by="ENGINE",
                      status="PROPOSED", our_accept=auto.get(ln.list_id, False), dealer_accept=False,
                      rounds=0, term=ln.term, collateral_type=ln.collateral_type, collateral_ccy=ln.collateral_ccy,
                      created_at=now, updated_at=now, history=[])
            s.add(m)
            await s.flush()
            m.match_id = f"WM-M-{m.id:08d}"
            _hist(m, "PROPOSED", by="ENGINE", feeBps=fee, quantity=q_)
            dl.qty -= q_
            ln.matched_qty += q_
            remaining -= q_
            created.append(m)
        ln.status = "FILLED" if ln.matched_qty >= ln.qty else ("PARTIAL" if ln.matched_qty else "OPEN")
    for m in created:
        await ctx.events.emit(s, "match_proposed", "match", m.match_id, match_json(m), topic="matches")
        _schedule_dealer(ctx, m.match_id)
        ctx.scheduler.at(now + timedelta(seconds=config.proposal_ttl_s), f"expire:{m.match_id}", expire_job, ctx,
                         m.match_id)
    return {"ranAt": clock.iso(now), "linesConsidered": len(lines), "matchesProposed": len(created),
            "matchedQuantity": sum(m.qty for m in created), "matches": [m.match_id for m in created]}


def _schedule_dealer(ctx: ServiceContext, match_id: str) -> None:
    delay = rng.stream("wematch", "latency").uniform(*config.dealer_latency_s)
    ctx.scheduler.after(delay, f"dealer:{match_id}", dealer_job, ctx, match_id)


async def _get_match(s: AsyncSession, match_id: str) -> Match:
    m = (await s.execute(select(Match).where(Match.match_id == match_id).with_for_update())).scalar()
    if m is None:
        raise EngineError("UNKNOWN_MATCH", f"no match {match_id}", 404)
    return m


async def dealer_job(ctx: ServiceContext, match_id: str) -> None:
    async with ctx.session() as s:
        m = await _get_match(s, match_id)
        if m.status != "PROPOSED" or m.dealer_accept:
            return
        dl = await s.get(DealerLine, m.dealer_line_id)
        r = rng.stream("wematch", "dealer")
        dealer_lends = m.side == "BORROW"
        ok = m.fee_bps >= dl.fee_min if dealer_lends else m.fee_bps <= dl.fee_max
        if ok and (m.proposed_by == "US" or r.random() < config.dealer_accept_p):
            m.dealer_accept = True
            _hist(m, "COUNTERPARTY_ACCEPTED", feeBps=m.fee_bps)
            await _maybe_agree(ctx, s, m)
        elif m.rounds < config.max_rounds:
            fee = dl.fee_min * r.uniform(1.0, 1.1) if dealer_lends else dl.fee_max * r.uniform(0.9, 1.0)
            fee = round(max(fee, m.fee_bps) if dealer_lends else min(fee, m.fee_bps), 1)
            m.fee_bps, m.proposed_by, m.dealer_accept, m.our_accept = fee, "DEALER", True, False
            m.rounds += 1
            _hist(m, "COUNTERPARTY_COUNTERED", feeBps=fee)
            await ctx.events.emit(s, "match_countered", "match", m.match_id, match_json(m), topic="matches")
        else:
            await _reject(ctx, s, m, "COUNTERPARTY")
        await s.commit()


async def _maybe_agree(ctx: ServiceContext, s: AsyncSession, m: Match) -> None:
    if not (m.our_accept and m.dealer_accept):
        await ctx.events.emit(s, "match_updated", "match", m.match_id, match_json(m), topic="matches")
        return
    m.status = "AGREED"
    _hist(m, "AGREED", feeBps=m.fee_bps, quantity=m.qty)
    ctx.scheduler.cancel(f"expire:{m.match_id}")
    inst = instrument_by_id(m.instrument_id)
    now = clock.now()
    loan = Loan(loan_id="pending", match_id=m.match_id, instrument_id=inst.id, isin=inst.isin, side=m.side,
                counterparty=m.dealer, qty_original=m.qty, qty_open=m.qty, fee_bps=m.fee_bps,
                price=market.price(inst, now), ccy=inst.ccy, term=m.term, collateral_type=m.collateral_type,
                collateral_ccy=m.collateral_ccy, trade_date=calendar.trade_date(now),
                settle_date=calendar.settle_date(now, inst.country), status="OPEN", created_at=now, history=[])
    s.add(loan)
    await s.flush()
    loan.loan_id = f"WM-T-{loan.id:08d}"
    _hist(loan, "BOOKED", feeBps=loan.fee_bps, quantity=loan.qty_open)
    await ctx.events.emit(s, "match_agreed", "match", m.match_id, {**match_json(m), "loanId": loan.loan_id},
                          topic="matches")
    await ctx.events.emit(s, "loan_booked", "loan", loan.loan_id, loan_json(loan), topic="loans")


async def _release(s: AsyncSession, m: Match) -> None:
    ln = await s.get(ListLine, m.line_id, with_for_update=True)
    dl = await s.get(DealerLine, m.dealer_line_id, with_for_update=True)
    ln.matched_qty -= m.qty
    if ln.status != "WITHDRAWN":  # a withdrawn line stays out of matching
        ln.status = "PARTIAL" if ln.matched_qty else "OPEN"
    dl.qty += m.qty


async def _reject(ctx: ServiceContext, s: AsyncSession, m: Match, by: str) -> None:
    m.status = "REJECTED"
    _hist(m, "REJECTED", by=by)
    ctx.scheduler.cancel(f"expire:{m.match_id}")
    ctx.scheduler.cancel(f"dealer:{m.match_id}")
    await _release(s, m)
    await ctx.events.emit(s, "match_rejected", "match", m.match_id, match_json(m), topic="matches")


async def expire_job(ctx: ServiceContext, match_id: str) -> None:
    async with ctx.session() as s:
        m = await _get_match(s, match_id)
        if m.status != "PROPOSED":
            return
        m.status = "EXPIRED"
        _hist(m, "EXPIRED")
        await _release(s, m)
        await ctx.events.emit(s, "match_expired", "match", m.match_id, match_json(m), topic="matches")
        await s.commit()


# -- negotiation (our side) ----------------------------------------------------------------------
async def accept(ctx: ServiceContext, s: AsyncSession, match_id: str) -> Match:
    m = await _get_match(s, match_id)
    if m.status != "PROPOSED":
        raise EngineError("BAD_STATE", f"match is {m.status}", 409)
    m.our_accept = True
    _hist(m, "WE_ACCEPTED", feeBps=m.fee_bps)
    await _maybe_agree(ctx, s, m)
    return m


async def propose(ctx: ServiceContext, s: AsyncSession, match_id: str, fee: float, qty: int | None) -> Match:
    m = await _get_match(s, match_id)
    if m.status != "PROPOSED":
        raise EngineError("BAD_STATE", f"match is {m.status}", 409)
    if qty and qty != m.qty:
        if qty > m.qty:
            raise EngineError("QTY_TOO_LARGE", "can only reduce quantity in negotiation")
        ln = await s.get(ListLine, m.line_id, with_for_update=True)
        dl = await s.get(DealerLine, m.dealer_line_id, with_for_update=True)
        ln.matched_qty -= m.qty - qty
        dl.qty += m.qty - qty
        m.qty = qty
    m.fee_bps, m.proposed_by, m.our_accept, m.dealer_accept = fee, "US", True, False
    m.rounds += 1
    _hist(m, "WE_PROPOSED", feeBps=fee, quantity=m.qty)
    await ctx.events.emit(s, "match_updated", "match", m.match_id, match_json(m), topic="matches")
    ctx.scheduler.cancel(f"dealer:{m.match_id}")
    _schedule_dealer(ctx, m.match_id)
    return m


async def reject(ctx: ServiceContext, s: AsyncSession, match_id: str) -> Match:
    m = await _get_match(s, match_id)
    if m.status != "PROPOSED":
        raise EngineError("BAD_STATE", f"match is {m.status}", 409)
    await _reject(ctx, s, m, "US")
    return m


# -- lifecycle -------------------------------------------------------------------------------------
async def _get_loan(s: AsyncSession, loan_id: str) -> Loan:
    ln = (await s.execute(select(Loan).where(Loan.loan_id == loan_id).with_for_update())).scalar()
    if ln is None:
        raise EngineError("UNKNOWN_LOAN", f"no loan {loan_id}", 404)
    return ln


async def rerate(ctx: ServiceContext, s: AsyncSession, loan_id: str, fee: float) -> Loan:
    ln = await _get_loan(s, loan_id)
    if ln.status != "OPEN":
        raise EngineError("BAD_STATE", f"loan is {ln.status}", 409)
    ln.pending_rerate_bps = fee
    _hist(ln, "RERATE_REQUESTED", feeBps=fee)
    await ctx.events.emit(s, "loan_rerate_requested", "loan", ln.loan_id, loan_json(ln), topic="loans")
    ctx.scheduler.after(rng.stream("wematch", "latency").uniform(*config.dealer_latency_s), f"rerate:{loan_id}",
                        rerate_job, ctx, loan_id)
    return ln


async def rerate_job(ctx: ServiceContext, loan_id: str) -> None:
    async with ctx.session() as s:
        ln = await _get_loan(s, loan_id)
        if ln.pending_rerate_bps is None:
            return
        new = ln.pending_rerate_bps
        fair = market.fee_bps(instrument_by_id(ln.instrument_id), clock.now())
        # Counterparty agrees if the new rate is in its favour, or within 15% of fair.
        favourable = new >= ln.fee_bps if ln.side == "BORROW" else new <= ln.fee_bps
        if favourable or abs(new - fair) / fair <= 0.15:
            _hist(ln, "RERATED", fromBps=ln.fee_bps, toBps=new)
            ln.fee_bps = new
            event = "loan_rerated"
        else:
            _hist(ln, "RERATE_DECLINED", feeBps=new, fairBps=round(fair, 1))
            event = "loan_rerate_declined"
        ln.pending_rerate_bps = None
        await ctx.events.emit(s, event, "loan", ln.loan_id, loan_json(ln), topic="loans")
        await s.commit()


async def return_qty(ctx: ServiceContext, s: AsyncSession, loan_id: str, qty: int | None) -> Loan:
    ln = await _get_loan(s, loan_id)
    if ln.status != "OPEN":
        raise EngineError("BAD_STATE", f"loan is {ln.status}", 409)
    qty = qty or ln.qty_open
    if qty > ln.qty_open:
        raise EngineError("QTY_TOO_LARGE", f"only {ln.qty_open} open")
    ln.qty_open -= qty
    event = "RECALLED" if ln.side == "LEND" else "RETURNED"
    _hist(ln, event, quantity=qty, remaining=ln.qty_open)
    if event == "RETURNED" and ln.recall_qty:  # a return answers the lender's recall first
        ln.recall_qty = max(ln.recall_qty - qty, 0) or None
        ln.recall_due = ln.recall_due if ln.recall_qty else None
    if ln.qty_open == 0:
        ln.status = "RETURNED"
    await ctx.events.emit(s, f"loan_{event.lower()}", "loan", ln.loan_id, loan_json(ln), topic="loans")
    return ln


async def recall_notice(ctx: ServiceContext, s: AsyncSession, ln: Loan, qty: int | None, due_days: int,
                        *, due: date | None = None, reason: str | None = None, event_id: str | None = None) -> Loan:
    """The lender on a loan we borrowed recalls shares: we owe them back by the due date."""
    if ln.status != "OPEN" or ln.side != "BORROW":
        raise EngineError("BAD_STATE", "only an open loan we borrowed can be recalled by its lender", 409)
    free = ln.qty_open - (ln.recall_qty or 0)
    qty = min(qty or free, free)
    if qty <= 0:
        raise EngineError("BAD_STATE", "recalled in full already", 409)
    today = calendar.trade_date(clock.now())
    ln.recall_qty = (ln.recall_qty or 0) + qty
    ln.recall_due = due or calendar.add_business_days(today, due_days)
    ln.recall_reason, ln.recall_event = reason, event_id
    _hist(ln, "RECALL_NOTICE", quantity=qty, dueDate=ln.recall_due.isoformat(), by=ln.counterparty,
          **({"reason": reason, "eventId": event_id} if event_id else {}))
    await ctx.events.emit(s, "loan_recall_notice", "loan", ln.loan_id, loan_json(ln), topic="loans")
    return ln


async def buy_in(ctx: ServiceContext, s: AsyncSession, ln: Loan, qty: int | None = None,
                 price: float | None = None) -> Loan:
    """The lender buys in recalled shares we didn't return: the loan closes for them at our cost. The lender keeps
    that share of our collateral (102% of the loan value at trade) against it."""
    if ln.status != "OPEN" or not ln.recall_qty:
        raise EngineError("BAD_STATE", "only an open recall can be bought in", 409)
    qty = min(qty or ln.recall_qty, ln.recall_qty, ln.qty_open)
    inst = instrument_by_id(ln.instrument_id)
    n = sum(1 for h in ln.history if h["event"] == "BOUGHT_IN") + 1
    r = rng.stream("wematch", f"buy-in:{ln.loan_id}:{n}")
    px = round(price or market.price(inst, clock.now()) * (1 + r.uniform(0.005, 0.03)), 4)
    kept = round(qty * ln.price * 1.02, 2)
    ln.qty_open -= qty
    ln.recall_qty = (ln.recall_qty - qty) or None
    ln.recall_due = ln.recall_due if ln.recall_qty else None
    if ln.qty_open == 0:
        ln.status = "RETURNED"
    _hist(ln, "BOUGHT_IN", buyInId=f"{ln.loan_id}-B{n}", quantity=qty, price=px, ccy=ln.ccy,
          cost=round(qty * px, 2), collateralKept=kept, amountDue=round(qty * px - kept, 2), remaining=ln.qty_open)
    await ctx.events.emit(s, "loan_bought_in", "loan", ln.loan_id, loan_json(ln), topic="loans")
    return ln


async def ca_recalls(ctx: ServiceContext, s: AsyncSession) -> int:
    """Lenders who want their dividend or their vote recall ``NOTICE_DAYS`` business days before the record date,
    due the day before (each lender decides once per loan and event, seeded)."""
    from datetime import time

    from refdata.corpactions import NOTICE_DAYS, corporate_actions
    from refdata.corpactions import recalls as lender_recalls

    now = clock.now()
    today = calendar.trade_date(now)
    if not calendar.is_business_day(today) or now.astimezone(clock.NEW_YORK).time() < time(9, 0):
        return 0
    events = [e for e in corporate_actions(today, calendar.add_business_days(today, NOTICE_DAYS + 1))
              if e.notice_date <= today <= e.due_date]
    n = 0
    for e in events:
        for ln in (await s.execute(select(Loan).where(Loan.status == "OPEN", Loan.side == "BORROW",
                                                      Loan.instrument_id == e.instrument_id)
                                   .order_by(Loan.id).with_for_update())).scalars().all():
            seen = any(h["event"] == "RECALL_NOTICE" and h.get("eventId") == e.event_id for h in ln.history)
            if seen or not lender_recalls(e, ln.loan_id) or ln.qty_open - (ln.recall_qty or 0) <= 0:
                continue
            await recall_notice(ctx, s, ln, None, 0, due=e.due_date, reason=e.kind, event_id=e.event_id)
            n += 1
    return n


async def auto_buy_ins(ctx: ServiceContext, s: AsyncSession) -> int:
    """Recalls still open ``buy_in_after_days`` business days past due are bought in, in market hours."""
    from datetime import time

    now = clock.now()
    today = calendar.trade_date(now)
    if config.buy_in_after_days is None or not calendar.is_business_day(today) or not (
            time(9, 30) <= now.astimezone(clock.NEW_YORK).time() < calendar.MARKET_CLOSE):
        return 0
    n = 0
    for ln in (await s.execute(select(Loan).where(Loan.status == "OPEN", Loan.recall_qty > 0)
                               .order_by(Loan.id).with_for_update())).scalars().all():
        if ln.recall_due and calendar.add_business_days(ln.recall_due, config.buy_in_after_days) <= today:
            await buy_in(ctx, s, ln)
            n += 1
    return n


async def stats(s: AsyncSession) -> dict[str, Any]:
    loans = (await s.execute(select(Loan).where(Loan.status == "OPEN"))).scalars().all()
    notional = sum(ln.qty_open * market.price(instrument_by_id(ln.instrument_id), clock.now()) * FX_USD[ln.ccy]
                   for ln in loans)
    counts = dict((await s.execute(select(Match.status, func.count()).group_by(Match.status))).all())
    return {"asOf": clock.iso(clock.now()), "openLoans": len(loans), "ongoingNotionalUsd": round(notional, 2),
            "matchesByStatus": counts}
