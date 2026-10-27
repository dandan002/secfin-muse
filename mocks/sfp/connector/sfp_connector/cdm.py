"""Build the CDM-shaped securities-lending trade the processor books (see ../../API.md#cdm-trade).

This follows FINOS CDM naming (tradeIdentifier, party roles, economicTerms) in simplified form;
it is not a full CDM object.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal


def securities_lending_trade(
    *, client_ref: str, our_lei: str, counterparty_lei: str, side: Literal["BORROW", "LEND"], isin: str,
    quantity: int, fee_bps: float, collateral_type: Literal["CASH", "NONCASH"], collateral_ccy: str,
    margin_pct: float, settlement_date: date, trade_date: date, legal_agreement: str, booking_entity: str,
    venue: str | None = None, venue_trade_id: str | None = None, uti: str | None = None,
    price: float | None = None, term: Literal["OPEN", "TERM"] = "OPEN", term_end: date | None = None,
) -> dict[str, Any]:
    """A loan or borrow as a CDM-shaped document. ``client_ref`` makes booking idempotent."""
    ids: list[dict[str, str]] = []
    if venue and venue_trade_id:
        ids.append({"issuer": venue, "assignedIdentifier": venue_trade_id})
    if uti:
        ids.append({"identifierType": "UTI", "assignedIdentifier": uti})
    lender, borrower = (counterparty_lei, our_lei) if side == "BORROW" else (our_lei, counterparty_lei)
    termination: dict[str, Any] = {"type": term}
    if term == "TERM":
        if term_end is None:
            raise ValueError("term_end is required for TERM")
        termination["endDate"] = term_end.isoformat()
    sl: dict[str, Any] = {
        "security": {"isin": isin}, "quantity": quantity, "feeRate": {"bps": fee_bps},
        "collateral": {"type": collateral_type, "currency": collateral_ccy, "marginPercentage": margin_pct},
        "termination": termination, "settlementDate": settlement_date.isoformat(),
    }
    if price is not None:
        sl["price"] = price
    return {
        "clientRef": client_ref, "venue": venue, "tradeDate": trade_date.isoformat(), "tradeIdentifier": ids,
        "party": [{"role": "LENDER", "partyId": lender}, {"role": "BORROWER", "partyId": borrower}],
        "economicTerms": {"securityLending": sl},
        "legalAgreement": legal_agreement, "bookingEntity": booking_entity,
    }
