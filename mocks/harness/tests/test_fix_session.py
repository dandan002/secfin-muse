"""Venue A over FIX 4.4, driven through the participant connector (simplefix framing), as the
platform would: logon auth, order flow with TradeCaptureReport, order status, market data, and
recovery of reports raised while offline."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from ngt_connector import (
    ExecutionReport,
    FixLogonError,
    FixSession,
    MarketData,
    MemorySeqStore,
    OrderRequest,
    TradeCaptureReport,
)

TERMS = dict(collateralType="CASH", collateralCcy="USD", marginPct=102, settlementDate=date(2026, 10, 28),
             legalAgreement="MSLA", bookingEntity="LOANDESK-US")


def _session(mocks, **kw) -> FixSession:
    cred = mocks.creds("ngt")["fix"]
    return FixSession("127.0.0.1", mocks.stack.fix_port, password=cred["password"], **kw)


def _bid(cl_ord_id: str, isin: str, qty: int, fee: float) -> OrderRequest:
    return OrderRequest(clientOrderId=cl_ord_id, type="BID", instrument=isin, quantity=qty, feeBps=fee, **TERMS)


def _report(cl_ord_id: str, *exec_types: str):
    return lambda m: isinstance(m, ExecutionReport) and m.cl_ord_id == cl_ord_id and m.exec_type in exec_types


def test_logon_rejects_bad_password(mocks):
    async def go():
        fix = FixSession("127.0.0.1", mocks.stack.fix_port, password="wrong")
        with pytest.raises(FixLogonError, match="bad password"):
            await fix.logon(reset_seq_nums=True)
        await fix.disconnect()

    asyncio.run(go())


def test_bid_counter_accept_confirm_and_status(mocks):
    mocks.verb("ngt", "config", hot_spread=[1.2, 1.2], warm_spread=[1.1, 1.1], counter_band=0.5)
    hot = mocks.get("ngt", "/v1/availability?instrument=@hot:1:US").json()["instruments"][0]

    async def go():
        fix = _session(mocks)
        await fix.logon(reset_seq_nums=True)
        await fix.new_order_single(_bid("FIX-B1", hot["isin"], 20000, round(hot["bestFeeBps"] * 0.9)))
        new = await fix.wait_for(_report("FIX-B1", "NEW"))
        await asyncio.to_thread(mocks.advance, 30)
        counter = await fix.wait_for(_report("FIX-B1", "RESTATED"))
        assert counter.counter_fee_bps and counter.counter_fee_bps > 0 and counter.order_id == new.order_id

        await fix.accept("FIX-B1", "FIX-B1-ACC")
        fill = await fix.wait_for(lambda m: isinstance(m, ExecutionReport) and m.exec_type == "TRADE"
                                  and m.order_id == new.order_id)
        tcr = await fix.wait_for(lambda m: isinstance(m, TradeCaptureReport) and m.order_id == new.order_id)
        assert fill.last_fee_bps == counter.counter_fee_bps and fill.ord_status == "FILLED"
        assert tcr.uti.startswith("MOCK00VENUEANGT") and tcr.venue_trade_id == fill.venue_trade_id
        assert (tcr.side, tcr.isin, tcr.quantity, tcr.settle_date) == ("BORROW", hot["isin"], fill.last_qty,
                                                                       "20261028")
        assert tcr.contra_party == counter.message.get(20010)

        status = await fix.order_status(order_id=new.order_id)
        assert (status.exec_type, status.ord_status, status.order_id) == ("ORDER_STATUS", "FILLED", new.order_id)
        unknown = await fix.order_status(cl_ord_id="NOPE")
        assert unknown.ord_status == "REJECTED" and unknown.message.get(103) == "5"
        await fix.logout()

    asyncio.run(go())
    trades = mocks.get("ngt", "/v1/trades", headers={"x-participant-key": mocks.creds("ngt")["participantKey"]}).json()
    assert trades[-1]["clientOrderId"] == "FIX-B1"


def test_market_data_snapshot_subscribe_and_reject(mocks):
    rest = mocks.get("ngt", "/v1/availability?instrument=@warm:1:US").json()
    isin = rest["instruments"][0]["isin"]
    lender = next(r for r in rest["byLender"] if r["quantity"] > 0)

    async def go():
        fix = _session(mocks)
        await fix.logon(reset_seq_nums=True)
        [snap] = await fix.market_data([isin], subscribe=True, req_id="MD-1")
        assert snap.isin == isin and snap.entries
        assert {e.lender_code for e in snap.entries} == {r["lenderCode"] for r in rest["byLender"] if r["quantity"]}

        mocks.verb("ngt", "supply", instrument=isin, lender=lender["lenderCode"], quantity=777000)
        inc = await fix.wait_for(lambda m: isinstance(m, MarketData) and not m.snapshot and m.req_id == "MD-1")
        line = next(e for e in inc.entries if e.lender_code == lender["lenderCode"])
        assert (line.quantity, line.action) == (777000, "CHANGE")

        with pytest.raises(ValueError, match="unknown instrument"):
            await fix.market_data(["XS9999999999"], req_id="MD-BAD")
        await fix.unsubscribe_market_data("MD-1")
        await fix.logout()

    asyncio.run(go())


def test_reports_raised_while_offline_are_recovered(mocks):
    hot = mocks.get("ngt", "/v1/availability?instrument=@hot:2:US").json()["instruments"][0]
    store = MemorySeqStore()  # the same store across reconnects, as a production engine keeps its seqnums

    async def go():
        fix = _session(mocks, store=store)
        await fix.logon(reset_seq_nums=True)
        await fix.new_order_single(_bid("FIX-OFF", hot["isin"], 1000, 1))  # far below any ask -> rejected
        await fix.wait_for(_report("FIX-OFF", "NEW"))
        await fix.logout()  # log off before the lender responds
        await asyncio.sleep(0.3)
        await asyncio.to_thread(mocks.advance, 60)

        again = _session(mocks, store=store)
        await again.logon()  # sequence numbers continue: the venue's gap triggers our ResendRequest
        rej = await again.wait_for(_report("FIX-OFF", "REJECTED"), timeout=10)
        assert rej.message.poss_dup and rej.reject_code == "FEE_TOO_LOW"
        await again.logout()

    asyncio.run(go())
