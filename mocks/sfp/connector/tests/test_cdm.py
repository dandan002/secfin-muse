from datetime import date

import pytest
from sfp_connector import securities_lending_trade

KW = dict(client_ref="BOOK-1", our_lei="OURS", counterparty_lei="THEIRS", isin="XS0000000001", quantity=1000,
          fee_bps=25.0, collateral_type="CASH", collateral_ccy="USD", margin_pct=102, legal_agreement="MSLA",
          settlement_date=date(2026, 10, 28), trade_date=date(2026, 10, 27), booking_entity="LOANDESK-US")


def test_borrow_puts_us_as_borrower_with_ids():
    t = securities_lending_trade(side="BORROW", venue="VENUE_A", venue_trade_id="NGT-T-1", uti="UTI-1", **KW)
    roles = {p["role"]: p["partyId"] for p in t["party"]}
    assert roles == {"LENDER": "THEIRS", "BORROWER": "OURS"}
    assert {"identifierType": "UTI", "assignedIdentifier": "UTI-1"} in t["tradeIdentifier"]
    sl = t["economicTerms"]["securityLending"]
    assert (sl["feeRate"]["bps"], sl["settlementDate"], sl["termination"]) == (25.0, "2026-10-28", {"type": "OPEN"})


def test_term_needs_end_date():
    with pytest.raises(ValueError):
        securities_lending_trade(side="LEND", term="TERM", **KW)
