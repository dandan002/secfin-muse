"""Booking and post-trade (spec module 5): CDM trade to the processor, then compare the fill to the ledger."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from refdata.counterparties import OUR_FIRM

from .. import clock, events
from ..adapters import AdapterError, systems
from ..models import Counterparty, Instrument, Trade


def trade_json(t: Trade, inst: Instrument | None = None) -> dict[str, Any]:
    return {"id": t.id, "orderId": t.order_id, "venue": t.venue, "venueTradeId": t.venue_trade_id, "uti": t.uti,
            "side": t.side, "instrumentId": t.instrument_id, "composite": inst.composite if inst else None,
            "counterparty": t.cp_code, "quantity": t.qty, "feeBps": t.fee_bps, "price": t.price, "ccy": t.ccy,
            "notional": round(t.qty * t.price, 2), "settlementDate": t.settle_date.isoformat(),
            "contractId": t.sfp_contract_id, "compareStatus": t.compare_status, "diffs": t.compare_diffs,
            "breakId": t.break_id, "executedAt": clock.iso(t.executed_at), "bookedAt": clock.iso(t.booked_at)}


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


async def book(s: AsyncSession, t: Trade, *, by: str) -> Trade:
    """Post the CDM trade, take the contract ID, and compare what the processor booked against the fill."""
    if t.sfp_contract_id:
        return await compare(s, t, by=by)
    inst = await s.get(Instrument, t.instrument_id)
    cp = await s.get(Counterparty, t.cp_code)
    t.cdm_json = cdm(t, inst, cp)
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
    return t


async def amend_to_fill(s: AsyncSession, t: Trade, *, by: str) -> Trade:
    """Fix a break by aligning the processor's contract to our venue fill (our side is the golden copy)."""
    if t.compare_status != "UNMATCHED" or not t.sfp_contract_id:
        return t
    fields = {d["field"]: d["ours"] for d in t.compare_diffs}
    try:
        await systems().processor.amend(t.sfp_contract_id, fields, "align to venue fill")
    except AdapterError as e:
        events.record(s, "trade", t.id, "amend_failed", {"error": str(e)}, actor=by)
        return t
    events.record(s, "trade", t.id, "amended", {"fields": fields}, actor=by)
    return await compare(s, t, by=by)


async def listing(s: AsyncSession, limit: int = 300) -> list[dict[str, Any]]:
    rows = list((await s.execute(select(Trade).order_by(Trade.executed_at.desc()).limit(limit))).scalars())
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(
        Instrument.id.in_({t.instrument_id for t in rows})))).scalars()}
    return [trade_json(t, insts.get(t.instrument_id)) for t in rows]
