"""Each system driven through its own connector, the way the LoanDesk platform will use them.

The connectors are standalone packages (``mocks/<system>/connector``); nothing here imports a
system's server code, so these tests check each API from the outside.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from analytics_connector import AnalyticsClient, FigiJob, FigiResolver
from analytics_connector import ApiError as AnalyticsError
from control_connector import SimControl
from custodian_connector import CustodianClient, CustodianRejected, SettlementInstruction
from gateway_connector import CounterpartyClient, Decision, GatewayPlatformClient
from gateway_connector import webhooks as gw_webhooks
from ib_connector import DeskChatBot
from ngt_connector import OrderRequest, VenueAClient, VenueAError
from sfp_connector import ProcessorClient, securities_lending_trade
from wematch_connector import ApiError as WematchError
from wematch_connector import ListLine, VenueBClient


def run(coro):
    return asyncio.run(coro)


def _isin(mocks, ref: str) -> str:
    return mocks.get("ngt", f"/v1/availability?instrument={ref}").json()["instruments"][0]["isin"]


# -- custodian: ISO 20022 ----------------------------------------------------------------------------
def _custodian(mocks) -> CustodianClient:
    cred = mocks.creds("custodian")
    return CustodianClient(mocks.urls["custodian"], api_key=cred["apiKey"], our_bic=cred["ourBic"])


def _leg(mocks, tx_id: str, **kw) -> SettlementInstruction:
    cred = mocks.creds("custodian")
    return SettlementInstruction(**{
        "tx_id": tx_id, "movement": "RECE", "tx_type": "SECB", "isin": _isin(mocks, "@gc:1:US"),
        "quantity": 100_000, "trade_date": date(2026, 10, 27), "settlement_date": date(2026, 10, 28),
        "counterparty_bic": "MKHAUS33XXX", "our_bic": cred["ourBic"], "common_id": f"UTI-{tx_id}", **kw})


def test_custodian_instruction_to_settlement(mocks):
    mocks.verb("custodian", "config", fail_rate=0.0, unmatch_rate=0.0)

    async def go():
        async with _custodian(mocks) as cust:
            ack = await cust.instruct(_leg(mocks, "TX-1"))
            assert (ack.msg_type, ack.status, ack.tx_id) == ("sese.024", "PACK", "TX-1") and ack.custodian_ref
            await asyncio.to_thread(mocks.advance, 30)
            await asyncio.to_thread(mocks.post, "control", "/clock/advance",
                                    json={"to": "2026-10-28T10:01:00-04:00"})
            mine = [a for _, a in await cust.outbox() if a.tx_id == "TX-1"]
            assert [(a.msg_type, a.status) for a in mine] == [("sese.024", "PACK"), ("sese.024", "MACH"),
                                                              ("sese.025", "SETT")]
            assert mine[-1].settled_quantity == 100_000 and mine[-1].is_final

            with pytest.raises(CustodianRejected) as bad:  # a field the XSD rejects
                await cust.instruct(_leg(mocks, "TX-BAD", isin="NOT-AN-ISIN"))
            assert bad.value.status == 400 and "ISIN" in (bad.value.errors or "")

    run(go())


def test_custodian_sese020_cancellation(mocks):
    mocks.verb("custodian", "config", fail_rate=0.0, unmatch_rate=0.0)

    async def go():
        async with _custodian(mocks) as cust:
            ack = await cust.instruct(_leg(mocks, "TX-CXL", settlement_date=date(2026, 10, 30)))
            res = await cust.cancel("TX-CXL", ack.custodian_ref)
            assert (res.msg_type, res.status, res.tx_id) == ("sese.027", "ACCP", "TX-CXL")
            mine = [a for _, a in await cust.outbox() if a.tx_id == "TX-CXL"]
            assert [(a.msg_type, a.status) for a in mine][-2:] == [("sese.027", "ACCP"), ("sese.024", "CAND")]
            assert (await cust.instruction("TX-CXL"))["status"] == "CANCELLED"

            with pytest.raises(CustodianRejected) as again:  # already cancelled: sese.027 Rjctd
                await cust.cancel("TX-CXL")
            assert again.value.status == 409 and again.value.advice.status == "RJCT"
            with pytest.raises(CustodianRejected) as unknown:
                await cust.cancel("TX-NOPE")
            assert unknown.value.status == 404

    run(go())


# -- Venue A: REST + WebSocket ------------------------------------------------------------------------
def _venue_a(mocks) -> VenueAClient:
    return VenueAClient(mocks.urls["ngt"], participant_key=mocks.creds("ngt")["participantKey"])


def test_venue_a_rest_order_stream_and_errors(mocks):
    async def go():
        async with _venue_a(mocks) as venue:
            avail = await venue.availability(instrument="@gc:2:US")
            gc = avail.instruments[0]
            assert avail.byLender and gc.bestFeeBps

            events: list[dict] = []

            async def listen() -> None:
                async for e in venue.stream(["orders", "trades"]):
                    events.append(e)
                    if e["type"] == "trade_executed":
                        return

            listener = asyncio.create_task(listen())
            await asyncio.sleep(0.3)  # subscribed before we trade
            first = OrderRequest(
                clientOrderId="REST-FB-1", type="FIRM_BID", instrument=gc.isin, quantity=10_000,
                feeBps=round(gc.bestFeeBps * 1.5, 1), collateralType="CASH", collateralCcy="USD", marginPct=102,
                settlementDate=date(2026, 10, 28), legalAgreement="MSLA", bookingEntity="LOANDESK-US")
            order = await venue.place_order(first)
            await asyncio.wait_for(listener, 10)
            assert order.type == "FIRM_BID"
            assert {"order_new", "trade_executed"} <= {e["type"] for e in events}
            for topic in ("orders", "trades"):  # each topic numbered 1, 2, 3, ... with no gaps
                seqs = [e["topicSeq"] for e in events if e["topic"] == topic]
                assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), (topic, seqs)

            trade = (await venue.trades())[-1]
            assert trade.clientOrderId == "REST-FB-1" and (await venue.trade(trade.uti)).tradeId == trade.tradeId
            assert (await venue.order("REST-FB-1")).status == "EXECUTED"

            with pytest.raises(VenueAError) as dup:  # clientOrderId already used
                await venue.place_order(first)
            assert (dup.value.status, dup.value.code) == (409, "DUPLICATE_CLORDID")

    run(go())


# -- Processor: CDM booking, reconciliation, marks, colr.003, EOD ---------------------------------------
def _processor(mocks) -> ProcessorClient:
    return ProcessorClient(mocks.urls["sfp"], api_key=mocks.creds("sfp")["apiKey"])


def _cdm(mocks, ref: str, isin: str, **kw):
    lei = {p["code"]: p["lei"] for p in mocks.get("ngt", "/v1/participants").json()}
    return securities_lending_trade(**{
        "client_ref": ref, "our_lei": lei["0001"], "counterparty_lei": next(v for k, v in lei.items() if k != "0001"),
        "side": "BORROW", "isin": isin, "quantity": 50_000, "fee_bps": 35.0, "collateral_type": "CASH",
        "collateral_ccy": "USD", "margin_pct": 102, "settlement_date": date(2026, 10, 28),
        "trade_date": date(2026, 10, 27), "legal_agreement": "MSLA", "booking_entity": "LOANDESK-US",
        "venue": "VENUE_A", "venue_trade_id": f"T-{ref}", "uti": f"UTI-{ref}", **kw})


def test_processor_booking_break_amend_marks_and_eod(mocks):
    mocks.verb("sfp", "flags", break_rate=0.0)
    isin = _isin(mocks, "@warm:2:US")

    async def go():
        async with _processor(mocks) as sfp:
            events: list[dict] = []

            async def listen() -> None:
                async for e in sfp.stream(["contracts", "breaks"]):
                    events.append(e)
                    if e["type"] == "break_resolved":
                        return

            listener = asyncio.create_task(listen())
            await asyncio.sleep(0.3)
            ok = await sfp.book(_cdm(mocks, "B-1", isin))
            assert ok.status == "BOOKED" and ok.contract.uti == "UTI-B-1"
            assert (await sfp.book(_cdm(mocks, "B-1", isin))).status == "DUPLICATE"  # idempotent on clientRef
            assert (await sfp.compare(_cdm(mocks, "B-1", isin))).status == "MATCHED"

            mocks.verb("sfp", "flags", force_break_next=True)
            bad = await sfp.book(_cdm(mocks, "B-2", isin))
            cmp = await sfp.compare(_cdm(mocks, "B-2", isin))
            assert cmp.status == "UNMATCHED" and cmp.breakId and len(cmp.diffs) == 1
            diff = cmp.diffs[0]
            fixed = await sfp.amend(bad.contractId, {diff.field: diff.ours}, reason="match venue fill")
            assert fixed.resolvedBreaks == [cmp.breakId]
            await asyncio.wait_for(listener, 10)
            assert [e["type"] for e in events][:1] == ["contract_booked"] and events[-1]["type"] == "break_resolved"

            await asyncio.to_thread(mocks.advance, 86_400 * 2)  # prices move; the nightly mark runs
            calls = await sfp.margin_calls()
            assert calls, "two days of price moves should leave a margin call"
            xml = await sfp.margin_call_colr003(calls[0].callId)
            assert b"colr.003" in xml and f"<Amt Ccy=\"USD\">{calls[0].amount:.2f}</Amt>".encode() in xml

            rows = await sfp.eod_contracts()
            assert {r["contract_id"] for r in rows} >= {ok.contractId, bad.contractId}

            closed = await sfp.return_shares(ok.contractId)
            assert closed.status == "CLOSED" and closed.openQuantity == 0

    run(go())


# -- Venue B: lists, optimiser, negotiation, loans ------------------------------------------------------
def test_venue_b_list_to_loan(mocks):
    dealer = mocks.get("wematch", "/_admin/dealer-lines?side=LEND&limit=2").json()
    lines = [ListLine(instrument=_isin_of_id(mocks, d["instrumentId"]), side="BORROW", quantity=d["quantity"],
                      feeBps=d["feeMax"]) for d in dealer]
    mocks.verb("wematch", "config", dealer_accept_p=1.0)

    async def go():
        async with VenueBClient(mocks.urls["wematch"], api_key=mocks.creds("wematch")["apiKey"]) as wm:
            lst = await wm.upload_csv(lines, client_ref="csv-1")
            assert lst.lines == 2
            run_ = await wm.run_optimiser(lst.listId)
            assert run_["matchesProposed"] >= 2
            m = (await wm.matches(list_id=lst.listId))[0]
            assert (await wm.accept(m.matchId)).ourAccept
            await asyncio.to_thread(mocks.advance, 30)
            m = await wm.match(m.matchId)
            if m.status == "PROPOSED":  # the dealer countered: take it
                m = await wm.accept(m.matchId)
            assert m.status == "AGREED"
            loan = next(ln for ln in await wm.loans() if ln.matchId == m.matchId)
            half = loan.quantityOpen // 2
            assert (await wm.return_shares(loan.loanId, half)).quantityOpen == loan.quantityOpen - half
            with pytest.raises(WematchError) as missing:
                await wm.match("WM-M-NOPE")
            assert missing.value.status == 404

    run(go())


def _isin_of_id(mocks, instrument_id: str) -> str:
    return mocks.get("ib", f"/api/instruments/resolve?q={instrument_id}").json()["isin"]


# -- Gateway: counterparty -> platform -> signed webhook ------------------------------------------------
def test_gateway_counterparty_platform_and_signed_webhook(mocks):
    import http.server
    import threading

    cred = mocks.creds("gateway")
    pb = next(c for c in cred["counterparties"] if c["code"] == "PBALPHA")
    got: list[tuple[bytes, str]] = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["content-length"]))
            got.append((body, self.headers[gw_webhooks.SIGNATURE_HEADER]))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    callback = f"http://127.0.0.1:{server.server_port}/locates/cb"

    async def go():
        async with (CounterpartyClient(mocks.urls["gateway"], api_key=pb["apiKey"]) as cp,
                    GatewayPlatformClient(mocks.urls["gateway"], platform_token=cred["platformToken"]) as gw):
            receipt = await cp.submit_locates([(_isin(mocks, "@gc:1:US"), 100_000), ("9ZZZZZ1", 5_000)],
                                              request_id="PB-1", callback_url=callback, idempotency_key="idem-x")
            assert receipt.lines == 2 and receipt.invalidLines[0]["lineNo"] == 2
            replay = await cp.submit_locates([(_isin(mocks, "@gc:1:US"), 100_000), ("9ZZZZZ1", 5_000)],
                                             request_id="PB-1", callback_url=callback, idempotency_key="idem-x")
            assert replay.idempotentReplay and replay.locateId == receipt.locateId

            [req] = await gw.inbox()
            assert (req.locateId, req.format, req.counterparty) == (receipt.locateId, "CDM", "PBALPHA")
            await gw.decide(req.locateId, [Decision(lineNo=1, decision="PARTIAL", approvedQuantity=60_000,
                                                    indicativeFeeBps=30, regShoBasis="VENUE_A_AVAILABILITY")])
            await asyncio.to_thread(mocks.advance, 5)
            assert (await cp.locate(req.locateId)).lines[0].approvedQuantity == 60_000
            assert any(d["ok"] for d in await gw.deliveries(req.locateId))

    try:
        run(go())
    finally:
        server.shutdown()
    body, header = got[0]
    assert gw_webhooks.verify(pb["webhookSecret"], body, header, tolerance_s=None)
    assert not gw_webhooks.verify(pb["webhookSecret"], body.replace(b"60000", b"99999"), header, tolerance_s=None)


# -- Desk Chat bot ----------------------------------------------------------------------------------------
def test_desk_chat_bot_history_send_and_subscribe(mocks):
    mocks.verb("ib", "persona", persona="pb-alpha", action="locate_blast", tierCounts={"GC": 5}, spacingS=1)

    async def go():
        async with DeskChatBot(mocks.urls["ib"], bot_token=mocks.creds("ib")["botToken"]) as bot:
            streams = {s.streamId: s for s in await bot.streams()}
            room = next(sid for sid, s in streams.items() if s.lastSeq > 0)
            history = [m async for m in bot.history(room)]
            assert history and [m.seq for m in history] == list(range(1, len(history) + 1))
            assert any(m.chips for m in history)  # instruments recognised in desk shorthand

            mine: list = []

            async def listen() -> None:
                async for m in bot.subscribe():
                    if m.clientMsgId == "bot-ack-1":
                        mine.append(m)
                        return

            listener = asyncio.create_task(listen())
            await asyncio.sleep(0.3)
            await bot.send(room, "looking at these now", client_msg_id="bot-ack-1")
            await asyncio.wait_for(listener, 10)
            assert mine[0].via == "bot" and mine[0].seq == history[-1].seq + 1

    run(go())


# -- Simulation control -----------------------------------------------------------------------------------
def test_sim_control(mocks):
    async def go():
        async with SimControl(mocks.urls["control"], api_key=mocks.creds("control")["operatorKey"]) as sim:
            before = await sim.clock()
            after = await sim.advance(seconds=90)
            assert before["mode"] == "stepped" and after["now"] > before["now"]
            assert all(s["up"] for s in (await sim.services()).values())
            await sim.set_fault("ngt", outage=True)
            assert mocks.get("ngt", "/v1/participants").status_code == 503
            await sim.clear_faults()
            assert mocks.get("ngt", "/v1/participants").status_code == 200
            assert {s["name"] for s in await sim.scenarios()} >= {"demo", "chaos"}

    run(go())


# -- Market analytics: snapshots, screen, exchange SI, daily file, alerts, OpenFIGI-shaped mapping ---------
def test_analytics_feed_alerts_and_mapping(mocks):
    key = mocks.creds("analytics")["apiKey"]
    hot, gc = _isin(mocks, "@hot:0:US"), _isin(mocks, "@gc:0:US")

    async def go():
        async with AnalyticsClient(mocks.urls["analytics"], api_key=key) as data:
            snap = await data.security(hot)
            assert snap.identifiers["isin"] == hot and snap.borrow.tier == "HOT"
            assert snap.crowdingScore > (await data.security(gc)).crowdingScore
            assert len(await data.history(hot)) == 31

            top = await data.screen(country="US", sort="squeezeRisk", limit=10)
            assert top.total > 500 and len(top.items) == 10
            assert top.items[0].squeezeRisk >= top.items[-1].squeezeRisk

            reported = await data.exchange_short_interest(instrument=hot)
            assert reported[0].settlementDate.isoformat() == "2026-10-15"

            rows = await data.daily_file()
            assert len(rows) == (await data.files())[0]["rows"] > 1000
            assert {"si_pct_float", "days_to_cover", "indicative_fee_bps"} <= set(rows[0])
            mocks.verb("analytics", "flags", late_file=True)
            with pytest.raises(AnalyticsError) as late:
                await data.daily_file(date.fromisoformat(rows[0]["as_of"]))
            assert late.value.status == 404
            mocks.verb("analytics", "flags", late_file=False, stale_feed=True)
            assert (await data.security(hot)).asOf.isoformat().startswith("2026-10-25T21:00")
            mocks.verb("analytics", "flags", stale_feed=False)

            await data.set_watchlist([hot, gc])
            events: list[dict] = []

            async def listen() -> None:
                async for e in data.stream():
                    events.append(e)
                    return

            listener = asyncio.create_task(listen())
            await asyncio.sleep(0.3)
            mocks.verb("analytics", "alert", instrument=hot, kind="SQUEEZE")
            await asyncio.wait_for(listener, 10)
            assert events[0]["data"]["isin"] == hot and events[0]["data"]["kind"] == "SQUEEZE"
            await asyncio.to_thread(mocks.advance, 3_600)  # four scans; Hot watchlist names run >90% utilized
            kinds = {(a.isin, a.kind) for a in await data.alerts()}
            assert (hot, "SQUEEZE") in kinds

        async with FigiResolver(mocks.urls["analytics"] + "/v1") as figi:  # no key: 10 jobs per request
            jobs = [FigiJob("ID_ISIN", hot), FigiJob("ID_ISIN", "US0000000000"), FigiJob("TICKER", "NOPE", "US")]
            out = await figi.map(jobs)
            assert out[0][0]["figi"] == snap.identifiers["figi"] and out[1] == [] == out[2]
            assert (await figi.map(jobs)) == out and figi.requests == 1  # second look-up served from cache
            ticker, exch = snap.composite.split()
            assert await figi.figi("TICKER", ticker, exch_code=exch) == snap.identifiers["figi"]

    run(go())
