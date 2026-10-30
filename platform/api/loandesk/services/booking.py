"""Booking and post-trade (spec module 5): CDM trade to the processor, compare the fill to the ledger, draft the
fix for a break, capture the Rule 10c-1a fields, and instruct the custodian to settle."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from refdata import calendar
from refdata.counterparties import OUR_FIRM

from .. import clock, events
from ..adapters import AdapterError, systems
from ..models import Action, Counterparty, Instrument, Trade
from ..settings import get_settings

VENUE_LABEL = {"VENUE_A": "Venue A (NGT-style)", "VENUE_B": "Venue B (Wematch-style)",
               "BILATERAL": "Bilateral (agreed in chat)"}
# Rule 10c-1a asks for the borrower's type; ours is a broker-dealer desk.
BORROWER_TYPE = {"PRIME_BROKER": "BROKER_DEALER", "BROKER_DEALER": "BROKER_DEALER", "INTERNAL": "BROKER_DEALER",
                 "HEDGE_FUND": "CUSTOMER", "AGENT_LENDER": "OTHER"}
SETTLED = ("SETTLED", "REJECTED", "CANCELLED")


def open_qty(t: Trade) -> int:
    return t.qty if t.open_qty is None else t.open_qty


def recall_status(t: Trade) -> str | None:
    """Where a lender's recall on this borrow stands: RECALLED (shares still to return), RETURNING (the return
    is instructed, not settled), RETURN_FAILING, RETURN_NOT_SENT (the custodian never took it), or None."""
    if t.recalled_qty:
        return "RECALLED"
    if not t.recall_due:
        return None
    legs = [e for e in t.lifecycle or [] if e.get("againstRecall")]
    if any(e.get("settleError") for e in legs):
        return "RETURN_NOT_SENT"
    if any(e.get("settleStatus") in ("FAILING", "UNMATCHED", "REJECTED", "CANCELLED") for e in legs):
        return "RETURN_FAILING"
    return "RETURNING"


def trade_json(t: Trade, inst: Instrument | None = None) -> dict[str, Any]:
    return {"id": t.id, "orderId": t.order_id, "venue": t.venue, "venueTradeId": t.venue_trade_id, "uti": t.uti,
            "side": t.side, "instrumentId": t.instrument_id, "composite": inst.composite if inst else None,
            "counterparty": t.cp_code, "quantity": t.qty, "openQty": open_qty(t), "feeBps": t.fee_bps,
            "pendingRerateBps": t.pending_rerate_bps, "recalledQty": t.recalled_qty or 0,
            "recallDue": t.recall_due.isoformat() if t.recall_due else None, "recallRef": t.recall_ref,
            "recallStatus": recall_status(t),
            "price": t.price, "ccy": t.ccy,
            "notional": round(t.qty * t.price, 2), "settlementDate": t.settle_date.isoformat(),
            "contractId": t.sfp_contract_id, "compareStatus": t.compare_status, "diffs": t.compare_diffs,
            "breakId": t.break_id, "loanStatus": t.loan_status or "OPEN", "lifecycle": t.lifecycle or [],
            "settleStatus": t.settle_status, "settleRef": t.settle_ref, "reg10c1a": t.reg_10c1a,
            "executedAt": clock.iso(t.executed_at), "bookedAt": clock.iso(t.booked_at)}


def cdm(t: Trade, inst: Instrument, cp: Counterparty) -> dict[str, Any]:
    """Our simplified CDM securities-lending trade, from the venue fill."""
    v = t.venue_json
    ours = {"partyId": OUR_FIRM["lei"], "role": "Borrower" if t.side == "BORROW" else "Lender"}
    theirs = {"partyId": cp.lei, "role": "Lender" if t.side == "BORROW" else "Borrower"}
    term = {"type": v.get("term", "OPEN")}
    if v.get("termEndDate"):
        term["endDate"] = v["termEndDate"]
    return {
        "clientRef": f"BOOK-{t.venue_trade_id}", "venue": t.venue, "tradeDate": clock.local_date(t.executed_at)
        .isoformat(),
        "tradeIdentifier": [{"issuer": t.venue, "assignedIdentifier": t.venue_trade_id},
                            {"identifierType": "UTI", "assignedIdentifier": t.uti}],
        "legalAgreement": v.get("legalAgreement"), "bookingEntity": v.get("bookingEntity"),
        "party": [ours, theirs],
        "economicTerms": {"securityLending": {
            "security": {"isin": inst.isin}, "quantity": t.qty, "feeRate": {"bps": t.fee_bps}, "price": t.price,
            "settlementDate": t.settle_date.isoformat(), "termination": term,
            "collateral": {"type": v.get("collateralType"), "currency": v.get("collateralCcy"),
                           "marginPercentage": v.get("marginPct")}}},
    }


def reg_10c1a(t: Trade, inst: Instrument, cp: Counterparty) -> dict[str, Any]:
    """Every field SEC Rule 10c-1a asks of a covered loan, captured now so reporting is a switch, not a rebuild."""
    v = t.venue_json
    lender, borrower = (cp, None) if t.side == "BORROW" else (None, cp)
    return {
        "uti": t.uti, "securityId": inst.isin, "securityIdType": "ISIN", "figi": inst.figi,
        "loanEffectedAt": clock.iso(t.executed_at), "platform": VENUE_LABEL.get(t.venue, t.venue),
        "lenderLei": lender.lei if lender else OUR_FIRM["lei"],
        "borrowerLei": borrower.lei if borrower else OUR_FIRM["lei"],
        "borrowerType": BORROWER_TYPE.get(borrower.kind, "OTHER") if borrower else "BROKER_DEALER",
        "quantity": t.qty, "collateralType": v.get("collateralType"), "collateralCcy": v.get("collateralCcy"),
        "collateralPct": v.get("marginPct"), "feeBps": t.fee_bps,
        "rateType": "REBATE" if v.get("collateralType") == "CASH" else "FEE",
        "termination": v.get("termEndDate") or "OPEN", "settlementDate": t.settle_date.isoformat(),
        "reportable": inst.country == "US",  # covered securities: US-listed equities here
    }


async def book(s: AsyncSession, t: Trade, *, by: str) -> Trade:
    """Post the CDM trade, take the contract ID, and compare what the processor booked against the fill."""
    if t.sfp_contract_id:
        return await compare(s, t, by=by)
    inst = await s.get(Instrument, t.instrument_id)
    cp = await s.get(Counterparty, t.cp_code)
    if cp is None:
        t.compare_status = "FAILED"
        events.record(s, "trade", t.id, "booking_failed", {**trade_json(t, inst),
                                                           "error": f"unknown counterparty {t.cp_code}"}, actor=by)
        return t
    t.cdm_json = cdm(t, inst, cp)
    t.reg_10c1a = reg_10c1a(t, inst, cp)
    if t.open_qty is None:
        t.open_qty = t.qty
    try:
        r = await systems().processor.book(t.cdm_json)
    except AdapterError as e:
        t.compare_status = "FAILED"
        events.record(s, "trade", t.id, "booking_failed", {**trade_json(t, inst), "error": str(e)}, actor=by)
        return t
    t.sfp_contract_id, t.booked_at, t.compare_status = r["contractId"], clock.now(), "BOOKED"
    events.record(s, "trade", t.id, "booked", trade_json(t, inst), actor=by)
    return await compare(s, t, by=by)


async def compare(s: AsyncSession, t: Trade, *, by: str) -> Trade:
    try:
        r = await systems().processor.compare(t.cdm_json or {}, t.sfp_contract_id)
    except AdapterError as e:
        events.record(s, "trade", t.id, "compare_failed", {"error": str(e)}, actor=by)
        return t
    t.compare_status, t.compare_diffs, t.break_id = r["status"], r.get("diffs", []), r.get("breakId")
    events.record(s, "trade", t.id, "compared", trade_json(t), actor=by,
                  topic="trade" if r["status"] == "MATCHED" else "break")
    if t.compare_status == "UNMATCHED":
        await propose_fix(s, t)
    elif t.compare_status == "MATCHED" and get_settings().auto_settle and not t.settle_ref:
        await settle(s, t, by="policy:AUTO-SETTLE")
    return t


async def propose_fix(s: AsyncSession, t: Trade) -> Action | None:
    """Draft the fix for a break as a gate card: align the processor to the venue fill, field by field.

    The venue's execution is the record both sides agreed, so the fill is the golden copy; the trader can edit
    the fields (e.g. when the fill itself was wrong) or reject. One open card per trade."""
    from .. import gate

    pending = (await s.execute(select(Action.id).where(Action.kind == "amend", Action.status == "PENDING",
                                                       Action.source_ref == t.id))).scalar()
    if pending or not t.compare_diffs:
        return None
    inst = await s.get(Instrument, t.instrument_id)
    fields = {d["field"]: d["ours"] for d in t.compare_diffs}
    said = "; ".join(f"{d['field']}: processor {d['processor']}, fill {d['ours']}" for d in t.compare_diffs)
    try:
        return await gate.propose(
            s, kind="amend", title=f"Fix break on {inst.composite} {t.sfp_contract_id}: "
                                   f"{', '.join(fields)} to the fill", payload={"tradeId": t.id, "fields": fields},
            proposed_by="rules:break-fix", source_ref=t.id, risk_flags=[f"break {t.break_id}"] if t.break_id else [],
            original={d["field"]: d["processor"] for d in t.compare_diffs},
            rationale=f"The processor booked {t.sfp_contract_id} differently from the {VENUE_LABEL.get(t.venue)} "
                      f"fill {t.venue_trade_id} ({said}). The fill is what both sides executed, so amend the "
                      f"contract to it.")
    except gate.GateError as e:  # kill switch: the break stays on the blotter for the trader
        events.record(s, "trade", t.id, "fix_not_proposed", {"error": str(e)}, actor="rules:break-fix")
        return None


async def amend_to_fill(s: AsyncSession, t: Trade, *, by: str, fields: dict[str, Any] | None = None) -> Trade:
    """Fix a break by aligning the processor's contract to our venue fill (or to ``fields``, if given)."""
    if t.compare_status != "UNMATCHED" or not t.sfp_contract_id:
        return t
    fields = fields or {d["field"]: d["ours"] for d in t.compare_diffs}
    try:
        await systems().processor.amend(t.sfp_contract_id, fields, "align to venue fill")
    except AdapterError as e:
        events.record(s, "trade", t.id, "amend_failed", {"error": str(e)}, actor=by)
        return t
    events.record(s, "trade", t.id, "amended", {"fields": fields}, actor=by)
    return await compare(s, t, by=by)


# -- settlement -------------------------------------------------------------------------------------
STATUS_FROM_CUSTODIAN = {"ACCEPTED": "PENDING", "MATCHED": "MATCHED", "UNMATCHED": "UNMATCHED",
                         "FAILING": "FAILING", "SETTLED": "SETTLED", "REJECTED": "REJECTED",
                         "CANCELLED": "CANCELLED"}


async def instruct(s: AsyncSession, t: Trade, *, leg: str, quantity: int, by: str) -> dict[str, Any]:
    """One sese.023 (via the custodian's JSON form) for a loan or return leg.

    Loan leg: we receive the shares we borrowed (SECB) or deliver the ones we lent (SECL). A return or recall
    reverses it (SBRE / SLRE). Cash collateral moves against the shares (APMT) at the contract's margin."""
    inst = await s.get(Instrument, t.instrument_id)
    cp = await s.get(Counterparty, t.cp_code)
    borrow = t.side == "BORROW"
    loan = leg == "LOAN"
    v = t.venue_json
    body: dict[str, Any] = {
        "txId": f"{t.id}-{leg[0]}{len(t.lifecycle or [])}"[:35],
        "movement": "RECE" if borrow == loan else "DELI",
        "txType": ("SECB" if borrow else "SECL") if loan else ("SBRE" if borrow else "SLRE"),
        "instrument": inst.isin, "quantity": quantity, "account": get_settings().settlement_account,
        "tradeDate": clock.local_date().isoformat(),
        "settlementDate": (t.settle_date if loan else calendar.settle_date(clock.now(), inst.country)).isoformat(),
        "counterpartyBic": cp.bic, "payment": "FREE"}
    if v.get("collateralType") == "CASH":
        body.update(payment="APMT", ccy=v.get("collateralCcy") or t.ccy,
                    amount=round(quantity * t.price * (v.get("marginPct") or 100) / 100, 2))
    r = await systems().custodian.instruct(body)
    i = r["instruction"]
    events.record(s, "trade", t.id, "settlement_instructed", {"leg": leg, "instruction": i}, actor=by)
    return i


async def settle(s: AsyncSession, t: Trade, *, by: str) -> Trade:
    if t.settle_ref:
        return t
    try:
        i = await instruct(s, t, leg="LOAN", quantity=t.qty, by=by)
    except AdapterError as e:
        t.settle_status = "FAILED"
        events.record(s, "trade", t.id, "settlement_failed", {"error": str(e)}, actor=by)
        return t
    t.settle_ref, t.settle_status = i["instructionId"], STATUS_FROM_CUSTODIAN.get(i["status"], "PENDING")
    return t


async def apply_settlement(s: AsyncSession, instructions: list[dict[str, Any]]) -> int:
    """Fold the custodian's instruction states into our trades' loan legs, and their return legs: a recall is
    answered only when the shares are back with the lender, not when we instruct the return."""
    by_ref = {i["instructionId"]: i for i in instructions}
    rows = (await s.execute(select(Trade).where(Trade.settle_ref.is_not(None),
                                                Trade.settle_status.not_in(SETTLED)))).scalars().all()
    changed = 0
    for t in rows:
        i = by_ref.get(t.settle_ref)
        new = STATUS_FROM_CUSTODIAN.get(i["status"], t.settle_status) if i else t.settle_status
        if new != t.settle_status:
            before = trade_json(t)
            t.settle_status = new
            events.record(s, "trade", t.id, f"settlement_{new.lower()}", trade_json(t), before=before,
                          actor="custodian")
            changed += 1
    for t in (await s.execute(select(Trade).where(Trade.returns_settling > 0))).scalars().all():
        before, log, moved = trade_json(t), [], []
        for e in t.lifecycle or []:
            i = by_ref.get(e.get("settleRef") or "")
            new = STATUS_FROM_CUSTODIAN.get(i["status"]) if i else None
            if new and new != e.get("settleStatus") and e.get("settleStatus") not in SETTLED:
                e = {**e, "settleStatus": new}
                moved.append(new)
            log.append(e)
        if not moved:
            continue
        t.lifecycle = log
        t.returns_settling = sum(1 for e in log if e.get("settleRef") and e.get("settleStatus") not in SETTLED)
        if not t.recalled_qty and t.recall_due and not any(
                e.get("againstRecall") and e.get("settleStatus") != "SETTLED" for e in log):
            t.recall_due = None  # every return against the recall has settled: answered
        events.record(s, "trade", t.id, f"return_settlement_{moved[-1].lower()}", trade_json(t), before=before,
                      actor="custodian")
        changed += 1
    return changed


async def listing(s: AsyncSession, limit: int = 300) -> list[dict[str, Any]]:
    rows = list((await s.execute(select(Trade).order_by(Trade.executed_at.desc()).limit(limit))).scalars())
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(
        Instrument.id.in_({t.instrument_id for t in rows})))).scalars()}
    return [trade_json(t, insts.get(t.instrument_id)) for t in rows]
