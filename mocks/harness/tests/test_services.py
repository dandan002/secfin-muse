"""End-to-end tests against the running mocks (real HTTP, real Postgres, stepped sim clock)."""

from __future__ import annotations

import json

from mockcore import auth
from refdata.counterparties import OUR_FIRM

TRADE_TERMS = {"collateralType": "CASH", "collateralCcy": "USD", "marginPct": 102, "settlementDate": "2026-10-28",
               "legalAgreement": "MSLA", "bookingEntity": "LOANDESK-US"}


# -- control -----------------------------------------------------------------------------------
def test_control_clock_and_services(mocks):
    c = mocks.get("control", "/clock").json()
    assert c["mode"] == "stepped" and c["now"] == "2026-10-27T13:00:00.000Z"
    out = mocks.advance(90)
    assert out["now"] == "2026-10-27T13:01:30.000Z"
    assert all("ran" in v for v in out["ticks"].values()), out["ticks"]
    services = mocks.get("control", "/services").json()
    assert all(v["up"] for v in services.values())
    assert "Not affiliated" in services["ngt"]["disclaimer"]
    assert services["ngt"]["label"] == "Venue A (NGT-style)"


def test_each_mock_has_its_own_database_and_gets_the_clock_pushed(mocks):
    import asyncio

    import asyncpg

    from conftest import ADMIN_URL, TEST_DB

    async def schemas(db: str) -> set[str]:
        conn = await asyncpg.connect(ADMIN_URL.rsplit("/", 1)[0] + "/" + db)
        try:
            rows = await conn.fetch("SELECT schema_name FROM information_schema.schemata "
                                    "WHERE schema_name NOT LIKE 'pg_%' AND schema_name <> 'information_schema'")
            return {r["schema_name"] for r in rows} - {"public"}
        finally:
            await conn.close()

    services = ("control", "ib", "gateway", "ngt", "wematch", "sfp", "custodian", "analytics")
    for svc in services:  # schema names match the service names; nobody else's tables, no shared clock row
        assert asyncio.run(schemas(TEST_DB.format(service=svc))) == {svc}, svc

    out = mocks.advance(30)
    for svc in services[1:]:
        assert mocks.get(svc, "/_admin/clock").json()["now"] == out["now"], svc


def test_faults_outage_and_clear(mocks):
    mocks.put("control", "/faults/ngt", json={"outage": True}).raise_for_status()
    assert mocks.get("ngt", "/v1/availability?limit=1").status_code == 503
    assert mocks.get("ngt", "/health").status_code == 200  # admin/health stay up
    mocks.delete("control", "/faults").raise_for_status()
    assert mocks.get("ngt", "/v1/availability?limit=1").status_code == 200


# -- mock-ib --------------------------------------------------------------------------------------
def test_ib_persona_borrows_labels_and_bot_api(mocks):
    out = mocks.verb("ib", "persona", persona="pb-bravo", action="borrow_list", n=6)
    assert out["lines"] == 6
    mocks.verb("ib", "persona", persona="harbor-lending", action="recall")
    token = mocks.creds("ib")["botToken"]
    h = {"authorization": f"Bearer {token}"}
    msgs = mocks.get("ib", "/streams/pbbravo-desk/messages?after=0&limit=1000", headers=h).json()["messages"]
    assert [m for m in msgs if m["ts"].startswith("2026-10-27")]
    labels = [x for x in mocks.get("ib", "/_admin/labels").json() if x["ts"].startswith("2026-10-27")]
    assert {x["label"]["intent"] for x in labels} >= {"RFQ", "RECALL"}
    assert sum(len(x["label"]["lines"]) for x in labels if x["label"]["persona"] == "pb-bravo") == 6
    # each labelled identifier really appears in its message text
    for x in labels:
        for ln in x["label"]["lines"]:
            assert ln["identifier"] in x["text"]


def test_ib_rfq_negotiation_reaction(mocks):
    mocks.verb("ib", "persona", persona="kestrel-hf", action="rfq", instrument="@hot:0:US", qty=500000,
               maxFeeBps=720)
    h = {"authorization": f"Bearer {mocks.creds('ib')['botToken']}"}
    r = mocks.post("ib", "/chats/kestrel-desk/messages", headers=h, json={"text": "can show 650bps open",
                                                                          "clientMsgId": "q-1"})
    assert r.status_code == 200
    dup = mocks.post("ib", "/chats/kestrel-desk/messages", headers=h, json={"text": "can show 650bps open",
                                                                            "clientMsgId": "q-1"})
    assert dup.json()["messageId"] == r.json()["messageId"]  # idempotent
    mocks.advance(60)
    last = mocks.get("ib", "/streams/kestrel-desk/messages?after=0&limit=1000", headers=h).json()["messages"][-1]
    assert last["sender"]["handle"] == "rpatel.kestrel" and "650" in last["text"]
    label = mocks.get("ib", "/_admin/labels").json()[-1]["label"]
    assert label["intent"] == "ACCEPT" and label["lines"][0]["feeBps"] == 650


def test_ib_human_api_unread_search_blast_compose(mocks):
    rooms = {r["id"]: r for r in mocks.get("ib", "/api/rooms", headers={"x-user": "srivera"}).json()}
    assert rooms["pbalpha-desk"]["unread"] == 0 and rooms["pbalpha-desk"]["folder"] == "Clients"
    mocks.post("ib", "/api/rooms/pbalpha-desk/messages", headers={"x-user": "jkowalski.pba"},
               json={"text": "@srivera loc 100k ZSXS pls"}).raise_for_status()
    rooms = {r["id"]: r for r in mocks.get("ib", "/api/rooms", headers={"x-user": "srivera"}).json()}
    assert rooms["pbalpha-desk"]["unread"] == 1 and rooms["pbalpha-desk"]["unreadMentions"] == 1
    hits = mocks.get("ib", "/api/search?q=ZSXS", headers={"x-user": "srivera"}).json()
    assert any("ZSXS" in m["text"] for m in hits)
    b = mocks.post("ib", "/api/blast", headers={"x-user": "srivera"},
                   json={"roomIds": ["pbalpha-desk", "pbbravo-desk"], "text": "GC rates unchanged today"}).json()
    assert len(b["messages"]) == 2 and b["messages"][0]["blastId"] == b["messages"][1]["blastId"]
    h = {"authorization": f"Bearer {mocks.creds('ib')['botToken']}"}
    assert mocks.post("ib", "/compose", headers=h, json={"roomId": "pbalpha-desk", "text": "draft"}).json()["ok"]
    assert mocks.get("ib", "/streams", headers={"authorization": "Bearer nope"}).status_code == 401


# -- mock-ngt ----------------------------------------------------------------------------------------
def _ngt(mocks):
    return {"x-participant-key": mocks.creds("ngt")["participantKey"]}


def test_ngt_firm_bid_fills_and_ledger(mocks):
    h = _ngt(mocks)
    gc = mocks.get("ngt", "/v1/availability?instrument=@gc:5:US").json()["instruments"][0]
    qty = min(100000, gc["quantity"] // 2)  # inventory scales with each name's real share count
    r = mocks.post("ngt", "/v1/orders", headers=h, json={"clientOrderId": "FB-1", "type": "FIRM_BID",
                                                         "instrument": gc["isin"], "quantity": qty,
                                                         "feeBps": gc["bestFeeBps"], **TRADE_TERMS})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "EXECUTED"
    trades = mocks.get("ngt", "/v1/trades", headers=h).json()
    assert trades[0]["uti"].startswith("MOCK00VENUEANGT") and trades[0]["borrower"]["code"] == OUR_FIRM["ngt_code"]
    after = mocks.get("ngt", f"/v1/availability?instrument={gc['isin']}").json()["instruments"][0]
    assert after["quantity"] == gc["quantity"] - qty
    dup = mocks.post("ngt", "/v1/orders", headers=h, json={"clientOrderId": "FB-1", "type": "FIRM_BID",
                                                           "instrument": gc["isin"], "quantity": 1, "feeBps": 1,
                                                           **TRADE_TERMS})
    assert dup.status_code == 409


def test_ngt_bid_counter_accept(mocks):
    h = _ngt(mocks)
    mocks.verb("ngt", "config", warm_spread=[1.1, 1.1], hot_spread=[1.2, 1.2], counter_band=0.5)
    hot = mocks.get("ngt", "/v1/availability?instrument=@hot:0:US").json()["instruments"][0]
    bid = round(hot["bestFeeBps"] * 0.9)  # always inside [0.5, 1.0) of any lender's ask
    o = mocks.post("ngt", "/v1/orders", headers=h, json={"clientOrderId": "B-1", "type": "BID",
                                                         "instrument": hot["isin"], "quantity": 10000,
                                                         "feeBps": bid, **TRADE_TERMS}).json()
    assert o["status"] == "PENDING"
    mocks.advance(30)
    o = mocks.get("ngt", "/v1/orders/B-1", headers=h).json()
    assert o["status"] == "COUNTERED", o
    assert o["counterFeeBps"] > bid
    t = mocks.post("ngt", f"/v1/orders/{o['orderId']}/accept", headers=h).json()
    assert t["feeBps"] == o["counterFeeBps"] and t["quantity"] == 10000
    assert mocks.get("ngt", "/v1/orders/B-1", headers=h).json()["status"] == "EXECUTED"


def test_ngt_validation_errors(mocks):
    h = _ngt(mocks)
    lender = next(p for p in mocks.get("ngt", "/v1/participants").json() if p["role"] == "LENDER"
                  and p.get("agreement") != "MSLA")
    r = mocks.post("ngt", "/v1/orders", headers=h, json={"clientOrderId": "V-1", "type": "BID",
                                                         "instrument": "@gc:1:US", "quantity": 1000, "feeBps": 30,
                                                         "counterpartyCode": lender["code"], **TRADE_TERMS})
    assert r.status_code == 422 and r.json()["error"] == "AGREEMENT_MISMATCH"
    r = mocks.post("ngt", "/v1/orders", headers=h, json={"clientOrderId": "V-2", "type": "BID", "instrument": "x",
                                                         "quantity": 1000, "feeBps": 30, **TRADE_TERMS})
    assert r.json()["error"] == "UNKNOWN_INSTRUMENT"
    assert mocks.post("ngt", "/v1/orders", json={}).status_code in (401, 422)


# -- mock-wematch -----------------------------------------------------------------------------------
def test_wematch_list_optimise_negotiate_lifecycle(mocks):
    h = {"x-api-key": mocks.creds("wematch")["apiKey"]}
    dealer = mocks.get("wematch", "/_admin/dealer-lines?side=LEND&limit=3").json()
    lines = [{"instrument": _isin_of(mocks, d["instrumentId"]), "side": "BORROW", "quantity": d["quantity"],
              "feeBps": d["feeMax"]} for d in dealer]
    up = mocks.post("wematch", "/v1/interest-lists", headers=h,
                    json={"name": "t", "clientRef": "list-1", "lines": lines + [{"instrument": "NOPE", "side": "BORROW",
                                                                                 "quantity": 1, "feeBps": 1}]}).json()
    assert up["lines"] == 3 and up["rejectedLines"] == 1
    again = mocks.post("wematch", "/v1/interest-lists", headers=h, json={"name": "t", "clientRef": "list-1",
                                                                         "lines": lines}).json()
    assert again["duplicate"] and again["listId"] == up["listId"]
    run = mocks.post("wematch", "/v1/optimiser/run", headers=h, json={"listId": up["listId"]}).json()
    assert run["matchesProposed"] >= 3
    mocks.verb("wematch", "config", dealer_accept_p=1.0)
    mid = run["matches"][0]
    m = mocks.post("wematch", f"/v1/matches/{mid}/accept", headers=h).json()
    assert m["ourAccept"]
    mocks.advance(30)
    m = mocks.get("wematch", f"/v1/matches/{mid}", headers=h).json()
    if m["status"] == "PROPOSED":  # dealer countered (decided before accept_p changed): take it
        m = mocks.post("wematch", f"/v1/matches/{mid}/accept", headers=h).json()
    assert m["status"] == "AGREED"
    loan = mocks.get("wematch", "/v1/loans", headers=h).json()[0]
    ret = mocks.post("wematch", f"/v1/loans/{loan['loanId']}/return", headers=h,
                     json={"quantity": loan["quantityOpen"] // 2}).json()
    assert ret["quantityOpen"] == loan["quantityOpen"] - loan["quantityOpen"] // 2
    mocks.post("wematch", f"/v1/loans/{loan['loanId']}/rerate", headers=h, json={"feeBps": loan["feeBps"]})
    mocks.advance(30)
    stats = mocks.get("wematch", "/v1/stats", headers=h).json()
    assert stats["openLoans"] >= 1 and stats["ongoingNotionalUsd"] > 0


def _isin_of(mocks, instrument_id: str) -> str:
    from mockcore import rng
    from refdata.universe import instrument_by_id

    rng.set_seed(20261027)
    return instrument_by_id(instrument_id).isin


def test_wematch_csv_upload(mocks):
    h = {"x-api-key": mocks.creds("wematch")["apiKey"], "content-type": "text/csv"}
    csv = "ISIN,Side,Qty,Rate\n" + f"{_isin_of(mocks, 'I00001')},B,\"100,000\",40\n"
    out = mocks.post("wematch", "/v1/interest-lists?name=csv", headers=h, content=csv).json()
    assert out["lines"] == 1 and out["rejectedLines"] == 0


# -- mock-sfp ------------------------------------------------------------------------------------------
def _sfp(mocks):
    return {"x-api-key": mocks.creds("sfp")["apiKey"]}


def test_sfp_booking_break_compare_amend(mocks):
    h = _sfp(mocks)
    mocks.verb("sfp", "flags", force_break_next=True)
    trade = {"clientRef": "BK-1", "uti": "UTI-TEST-1", "venue": "VENUE_A", "venueRef": "NGT-T-1", "side": "BORROW",
             "counterparty": "HARBORAL", "instrument": "@hot:0:US", "quantity": 250000, "feeBps": 700, **TRADE_TERMS}
    b = mocks.post("sfp", "/trades", headers=h, json=trade).json()
    assert b["status"] == "BOOKED"
    inj = mocks.get("sfp", "/_admin/injected-breaks").json()
    assert inj and inj[0]["contractId"] == b["contractId"]
    cmp = mocks.post("sfp", "/compare", headers=h, json={"trade": trade}).json()
    assert cmp["status"] == "UNMATCHED" and cmp["diffs"][0]["field"] == inj[0]["field"]
    fix = {inj[0]["field"]: inj[0]["submitted"]}
    am = mocks.post("sfp", f"/contracts/{b['contractId']}/amend", headers=h,
                    json={"fields": fix, "reason": "align to venue fill"}).json()
    assert am["resolvedBreaks"] == [cmp["breakId"]]
    assert mocks.post("sfp", "/compare", headers=h, json={"trade": trade}).json()["status"] == "MATCHED"
    assert mocks.post("sfp", "/trades", headers=h, json=trade).json()["status"] == "DUPLICATE"


def test_sfp_cdm_lifecycle_marks_eod(mocks):
    h = _sfp(mocks)
    mocks.verb("sfp", "flags", break_rate=0.0)
    cdm = {"tradeIdentifier": [{"issuer": "VENUE_B", "assignedIdentifier": "WM-T-1"},
                               {"identifierType": "UTI", "assignedIdentifier": "UTI-CDM-1"}],
           "venue": "VENUE_B", "tradeDate": "2026-10-27", "legalAgreement": "MSLA", "bookingEntity": "LOANDESK-US",
           "party": [{"partyId": OUR_FIRM["lei"], "role": "Lender"}, {"partyId": "KESTRELHF", "role": "Borrower"}],
           "economicTerms": {"securityLending": {"security": {"isin": _isin_of(mocks, "I00001")}, "quantity": 100000,
                                                 "feeRate": {"bps": 25}, "settlementDate": "2026-10-28",
                                                 "collateral": {"type": "CASH", "currency": "USD",
                                                                "marginPercentage": 102}}}}
    b = mocks.post("sfp", "/trades", headers=h, json=cdm).json()
    c = b["contract"]
    assert c["side"] == "LEND" and c["counterparty"]["code"] == "KESTRELHF" and c["uti"] == "UTI-CDM-1"
    mocks.advance(6 * 3600)
    m = mocks.post("sfp", f"/lifecycle/{c['contractId']}", headers=h, json={"event": "MARK"}).json()["mark"]
    assert m["requiredCollateral"] > 0
    r = mocks.post("sfp", "/lifecycle/UTI-CDM-1", headers=h, json={"event": "RECALL", "quantity": 40000}).json()
    assert r["contract"]["openQuantity"] == 60000
    pos = mocks.get("sfp", "/positions", headers=h).json()
    assert pos[0]["openQuantity"] == 60000 and pos[0]["side"] == "LEND"
    mocks.verb("sfp", "flags", eod_break=True)
    csv_text = mocks.get("sfp", "/eod/contracts.csv", headers=h).text
    assert csv_text.splitlines()[1].split(",")[7] == "59000"  # the seeded EOD disagreement


# -- mock-custodian ----------------------------------------------------------------------------------
def test_custodian_fail_then_settle(mocks):
    h = {"x-api-key": mocks.creds("custodian")["apiKey"]}
    mocks.verb("custodian", "config", fail_rate=0.0, unmatch_rate=0.0, fail_next=True)
    r = mocks.post("custodian", "/instructions", headers=h, json={
        "txId": "TX-F", "movement": "DELI", "txType": "SECL", "instrument": "@gc:3:US", "quantity": 5000,
        "settlementDate": "2026-10-27", "counterpartyBic": "MKKEUS33XXX"})
    assert r.status_code == 202, r.text
    mocks.post("control", "/clock/advance", json={"to": "2026-10-27T10:01:00-04:00"}).raise_for_status()
    i = mocks.get("custodian", "/instructions/TX-F", headers=h).json()
    assert i["status"] == "FAILING"
    mocks.post("control", "/clock/advance", json={"to": "2026-10-28T10:00:00-04:00"}).raise_for_status()
    assert mocks.get("custodian", "/instructions/TX-F", headers=h).json()["status"] == "SETTLED"


# -- mock-gateway -------------------------------------------------------------------------------------
def test_gateway_locates_idempotency_decision_webhook(mocks):
    cred = mocks.creds("gateway")
    key = next(c["apiKey"] for c in cred["counterparties"] if c["code"] == "PBALPHA")
    h = {"authorization": f"Bearer {key}", "idempotency-key": "idem-1"}
    body = {"requestId": "PBALPHA-20261027-0412", "counterparty": "PBALPHA", "settlementDate": "2026-10-28",
            "lines": [{"identifier": _isin_of(mocks, "I00001"), "idType": "ISIN", "quantity": 250000},
                      {"identifier": "9ZZZZZ1", "idType": "SEDOL", "quantity": 75000}]}
    r = mocks.post("gateway", "/v1/locates", headers=h, json=body)
    assert r.status_code == 202
    loc = r.json()
    assert loc["invalidLines"] and loc["invalidLines"][0]["lineNo"] == 2
    assert mocks.post("gateway", "/v1/locates", headers=h, json=body).json()["idempotentReplay"]
    assert mocks.post("gateway", "/v1/locates", headers=h, json={**body, "requestId": "x"}).status_code == 409
    cdm = {"borrowerLocate": {"locateRequestId": "CDM-1", "settlementDate": "2026-10-28", "locateLines": [
        {"security": {"identifier": "@gc:0:US", "identifierType": "TICKER"}, "quantity": {"amount": 1000}}]}}
    assert mocks.post("gateway", "/v1/locates", headers={"authorization": f"Bearer {key}"}, json=cdm).status_code == 202
    ph = {"authorization": f"Bearer {cred['platformToken']}"}
    inbox = mocks.get("gateway", "/v1/inbox", headers=ph).json()
    assert [x["format"] for x in inbox] == ["FLAT", "CDM"]
    d = mocks.post("gateway", "/v1/decisions", headers=ph, json={"locateId": loc["locateId"], "lines": [
        {"lineNo": 1, "decision": "PARTIAL", "approvedQuantity": 40000, "indicativeFeeBps": 425,
         "regShoBasis": "NGT_AVAILABILITY@09:31"}]}).json()
    assert d["status"] == "DECIDED"
    mocks.advance(5)
    got = mocks.get("gateway", "/cp/PBALPHA/received").json()
    assert got and got[0]["signatureOk"] and got[0]["body"]["lines"][0]["approvedQuantity"] == 40000
    secret = next(c["webhookSecret"] for c in cred["counterparties"] if c["code"] == "PBALPHA")
    raw = json.dumps(got[0]["body"], sort_keys=True).encode()
    assert auth.verify(secret, raw, auth.sign(secret, raw))


def test_gateway_rate_limit_and_loadgen(mocks):
    cred = mocks.creds("gateway")
    key = next(c["apiKey"] for c in cred["counterparties"] if c["code"] == "PBBRAVO")
    mocks.put("gateway", "/_admin/limits?rate_per_s=0.01&burst=2").raise_for_status()
    codes = [mocks.post("gateway", "/v1/locates", headers={"authorization": f"Bearer {key}"},
                        json={"identifier": "@gc:0:US", "quantity": 1000}).status_code for _ in range(3)]
    assert codes == [202, 202, 429]
    mocks.verb("gateway", "loadgen", on=True, base_per_min=6, burst_mult=5)
    mocks.advance(600)  # 09:00-09:10 is inside the burst window: ~300 expected arrivals
    ph = {"authorization": f"Bearer {cred['platformToken']}"}
    n = len(mocks.get("gateway", "/v1/inbox?limit=5000", headers=ph).json())
    assert 150 < n < 500, n


def test_gateway_close_uses_locates_and_writes_the_stock_record(mocks):
    """A fund's locates: agreed fee, entry and expiry at the close; at the close it shorts against some of them
    (never one priced over its limit), and the stock record carries the positions settling the next day."""
    cred = mocks.creds("gateway")
    key = next(c["apiKey"] for c in cred["counterparties"] if c["code"] == "KESTRELHF")
    lines = [{"identifier": _isin_of(mocks, f"I{n:05d}"), "quantity": 20_000 * n} for n in range(1, 11)]
    lines[1]["maxFeeBps"] = 10  # we'll price it at 30: the fund goes elsewhere
    loc = mocks.post("gateway", "/v1/locates", headers={"authorization": f"Bearer {key}"},
                     json={"requestId": "KHF-1", "lines": lines}).json()
    ph = {"authorization": f"Bearer {cred['platformToken']}"}
    inbox = mocks.get("gateway", "/v1/inbox", headers=ph).json()
    assert inbox[0]["lines"][1]["maxFeeBps"] == 10
    d = mocks.post("gateway", "/v1/decisions", headers=ph, json={"locateId": loc["locateId"], "lines": [
        {"lineNo": n, "decision": "APPROVED", "feeBps": 30, "regShoBasis": "ETB"} for n in range(1, 11)]}).json()
    line = d["lines"][0]
    assert line["feeBps"] == 30 and line["enteredAt"] and line["expiresAt"] == "2026-10-27T20:00:00.000Z"  # 16:00 NY
    assert mocks.get("gateway", "/v1/stock-record", headers=ph).status_code == 404  # no close yet

    mocks.post("control", "/clock/advance", json={"to": "2026-10-27T16:02:00-04:00"}).raise_for_status()
    rec = mocks.get("gateway", "/v1/stock-record", headers=ph).json()
    assert rec["tradeDate"] == "2026-10-27" and rec["settlementDate"] == "2026-10-28"
    usage = {u["lineNo"]: u for u in rec["locateUsage"]}
    assert len(usage) == 10 and usage[2]["usedQuantity"] == 0  # over the fund's limit
    sales = rec["shortSales"]
    assert sales and all(x["locateId"] == loc["locateId"] and x["counterparty"] == "KESTRELHF" for x in sales)
    assert all(0 < x["quantity"] <= usage[x["lineNo"]]["approvedQuantity"] for x in sales)
    assert all(line["enteredAt"] <= x["executedAt"] <= line["expiresAt"] for x in sales)
    assert sum(p["shortQuantity"] for p in rec["positions"]) == sum(x["quantity"] for x in sales)
    got = mocks.get("gateway", f"/v1/locates/{loc['locateId']}", headers={"authorization": f"Bearer {key}"}).json()
    assert {ln["lineNo"]: ln["usedQuantity"] for ln in got["lines"]} == {n: u["usedQuantity"] for n, u in usage.items()}

    # The next close carries the positions forward (less what the fund bought back).
    mocks.post("control", "/clock/advance", json={"to": "2026-10-28T16:02:00-04:00"}).raise_for_status()
    nxt = mocks.get("gateway", "/v1/stock-record", headers=ph).json()
    assert nxt["tradeDate"] == "2026-10-28" and not nxt["shortSales"]
    before = {(p["counterparty"], p["instrumentId"]): p["shortQuantity"] for p in rec["positions"]}
    for p in nxt["positions"]:
        assert p["shortQuantity"] == before[(p["counterparty"], p["instrumentId"])] - p["coveredQuantity"]
    assert mocks.get("gateway", "/v1/stock-record?date=2026-10-27", headers=ph).json()["shortSales"] == sales


def test_gateway_shorts_that_fail_at_cns_stay_open_until_delivered(mocks):
    """Shorts sold at one close settle at the next; some fail to deliver at CNS (here all the US ones), and each
    stays open, for the desk to close out under Rule 204, until it delivers the shares against it."""
    from refdata.universe import instrument_by_id

    cred = mocks.creds("gateway")
    key = next(c["apiKey"] for c in cred["counterparties"] if c["code"] == "BASALTHF")
    ph = {"authorization": f"Bearer {cred['platformToken']}"}
    lines = [{"identifier": _isin_of(mocks, f"I{n:05d}"), "quantity": 50_000} for n in range(1, 21)]
    loc = mocks.post("gateway", "/v1/locates", headers={"authorization": f"Bearer {key}"},
                     json={"requestId": "BHF-F1", "lines": lines}).json()
    mocks.post("gateway", "/v1/decisions", headers=ph, json={"locateId": loc["locateId"], "lines": [
        {"lineNo": n, "decision": "APPROVED", "feeBps": 30, "regShoBasis": "ETB"} for n in range(1, 21)]})
    mocks.post("control", "/clock/advance", json={"to": "2026-10-27T16:02:00-04:00"}).raise_for_status()
    sold = {p["instrumentId"]: p["soldQuantity"] for p in mocks.get(
        "gateway", "/v1/stock-record", headers=ph).json()["positions"]}
    assert mocks.get("gateway", "/v1/fails", headers=ph).json() == []  # nothing has settled yet
    assert mocks.verb("gateway", "fail_rate", rate=1.0) == {"rate": 1.0}
    mocks.post("control", "/clock/advance", json={"to": "2026-10-28T16:02:00-04:00"}).raise_for_status()
    fails = mocks.get("gateway", "/v1/fails", headers=ph).json()
    us = {iid for iid in sold if instrument_by_id(iid).country == "US"}
    assert us and {f["instrumentId"] for f in fails} == us
    assert all(f["settlementDate"] == "2026-10-28" and f["status"] == "OPEN" and f["counterparty"] == "BASALTHF"
               and 100 <= f["quantity"] <= sold[f["instrumentId"]] for f in fails)
    f = fails[0]
    path = f"/v1/fails/{f['failId']}/deliver"
    assert mocks.post("gateway", path, headers=ph, json={"quantity": f["quantity"] + 100}).status_code == 409
    got = mocks.post("gateway", path, headers=ph, json={"quantity": f["quantity"], "ref": "trd_1"}).json()
    assert (got["status"], got["outstanding"], got["deliveries"][0]["ref"]) == ("CLOSED", 0, "trd_1")
    assert [x["failId"] for x in mocks.get("gateway", "/v1/fails?status=OPEN", headers=ph).json()] == [
        x["failId"] for x in fails[1:]]
    assert mocks.get("gateway", f"/v1/fails?after={fails[-1]['seq']}", headers=ph).json() == []
    made = mocks.verb("gateway", "fail", counterparty="BASALTHF", quantity=1_000)
    assert made["status"] == "OPEN" and made["quantity"] == 1_000


# -- mock-analytics -----------------------------------------------------------------------------------
def test_analytics_auth_limits_and_exchange_cycle(mocks):
    h = {"x-api-key": mocks.creds("analytics")["apiKey"]}
    assert mocks.get("analytics", "/v1/securities/@hot:0").status_code == 401
    assert mocks.get("analytics", "/v1/securities/NOPE", headers=h).status_code == 404
    r = mocks.get("analytics", "/v1/securities/@hot:0/history?from=2025-01-01&to=2026-10-26", headers=h)
    assert r.status_code == 422
    jobs = [{"idType": "ID_ISIN", "idValue": "US0000000000"}] * 11
    assert mocks.post("analytics", "/v1/mapping", json=jobs).status_code == 413  # 10 without a key
    assert mocks.post("analytics", "/v1/mapping", json=jobs, headers={"x-openfigi-apikey": h["x-api-key"]}).json()[
        0] == {"warning": "No identifier found."}
    assert mocks.get("analytics", "/v1/exchange-short-interest?settlementDate=2026-10-30", headers=h).status_code == 404
    assert mocks.get("analytics", "/v1/exchange-short-interest?settlementDate=2026-10-20", headers=h).status_code == 422
    assert mocks.get("analytics", "/v1/exchange-short-interest?instrument=@hot:0:GB", headers=h).status_code == 422
