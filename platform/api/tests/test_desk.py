"""Desk behaviour with every outside system down: reference data, rules, locates, paste and the gate."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from loandesk import clock, db, gate
from loandesk.ingest import paste
from loandesk.services import locates, market


async def _supply(instrument_id: str, qty: int) -> None:
    async with db.session() as s:
        await market.upsert_availability(s, "VENUE_A", [{"instrumentId": instrument_id, "quantity": qty, "lenders": 3,
                                                         "bestFeeBps": 20.0, "asOf": "2026-10-27T13:59:00Z"}])
        await s.commit()


# -- security master --------------------------------------------------------------------------------
async def test_resolve_by_every_identifier(client: httpx.AsyncClient) -> None:
    for q in ("AAPL US", "aapl", "US9988883099", "9LHVVY5", "998888309", "AAPL US Equity"):
        body = (await client.get("/instruments/resolve", params={"q": q})).json()
        assert body["instrument"]["composite"] == "AAPL US", q
        assert body["instrument"]["tier"] in ("GC", "WARM", "HOT")


async def test_bad_check_digit_suggests_a_fix(client: httpx.AsyncClient) -> None:
    body = (await client.get("/instruments/resolve", params={"q": "US9988883098"})).json()
    assert body["instrument"] is None
    assert body["error"] == "bad ISIN check digit"
    assert "US9988883099" in body["suggestions"]


async def test_search_prefix(client: httpx.AsyncClient) -> None:
    rows = (await client.get("/instruments/search", params={"q": "MSF"})).json()
    assert rows[0]["composite"] == "MSFT US"


# -- locates ----------------------------------------------------------------------------------------
async def test_etb_auto_approves_and_holds_availability(client: httpx.AsyncClient) -> None:
    await _supply("I00001", 2_000_000)  # NVDA, GC
    r = await client.post("/locates", json={"counterparty": "PBALPHA", "lines": [{"identifier": "NVDA US",
                                                                                   "quantity": 250_000}]})
    loc = r.json()[0]
    assert (loc["route"], loc["status"], loc["decidedBy"]) == ("AUTO", "APPROVED", "policy:ETB-AUTO")
    assert "ETB_LIST@" in loc["regShoBasis"] and "VENUE_A_AVAIL:2000000" in loc["regShoBasis"]
    async with db.session() as s:
        assert await market.available(s, "I00001") == 1_750_000


async def test_hot_name_waits_for_trader_then_partial(client: httpx.AsyncClient) -> None:
    await _supply("I00192", 100_000)  # MRNA, HOT
    loc = (await client.post("/locates", json={"counterparty": "KESTRELHF", "lines": [
        {"identifier": "MRNA US", "quantity": 80_000}]})).json()[0]
    assert (loc["route"], loc["status"]) == ("HOLD", "RECEIVED")
    assert "hot name" in loc["routeReasons"]
    r = await client.post(f"/locates/{loc['id']}/decision", json={"decision": "APPROVED", "quantity": 40_000},
                          headers={"x-trader": "sam"})
    body = r.json()
    assert (body["status"], body["qtyApproved"], body["decidedBy"]) == ("PARTIAL", 40_000, "trader:sam")
    again = await client.post(f"/locates/{loc['id']}/decision", json={"decision": "REJECTED"})
    assert again.status_code == 409


async def test_typo_line_is_invalid_with_hint(client: httpx.AsyncClient) -> None:
    loc = (await client.post("/locates", json={"counterparty": "PBALPHA", "lines": [
        {"identifier": "US9988883098", "quantity": 1000}]})).json()[0]
    assert loc["status"] == "INVALID" and "did you mean US9988883099" in loc["error"]


async def test_batch_approve(client: httpx.AsyncClient) -> None:
    await _supply("I00039", 10_000)  # DELL, WARM: goes to review
    locs = (await client.post("/locates", json={"counterparty": "PBBRAVO", "lines": [
        {"identifier": "DELL US", "quantity": 1000}, {"identifier": "DELL US", "quantity": 2000}]})).json()
    assert {x["route"] for x in locs} == {"REVIEW"}
    r = (await client.post("/locates/decisions", json={"ids": [x["id"] for x in locs] + ["nope"],
                                                        "decision": "APPROVED"})).json()
    assert len(r["decided"]) == 2 and r["failed"][0]["id"] == "nope"


# -- orders: the venue is down, checks still run --------------------------------------------------------
async def test_order_preview_routes_and_checks(client: httpx.AsyncClient) -> None:
    p = (await client.post("/orders/preview", json={"instrument": "AAPL US", "quantity": 100_000,
                                                    "feeBps": 30})).json()
    assert (p["venue"], p["type"]) == ("VENUE_A", "FIRM_BID")
    assert p["terms"]["legalAgreement"] == "MSLA" and p["terms"]["marginPct"] == 102
    big = (await client.post("/orders/preview", json={"instrument": "AAPL US", "quantity": 100_000,
                                                      "feeBps": 900})).json()
    assert any(c["name"] == "fat finger (fee)" and not c["ok"] for c in big["checks"])
    r = await client.post("/orders", json={"instrument": "AAPL US", "quantity": 100_000, "feeBps": 900})
    assert r.status_code == 422 and "fat finger" in r.json()["detail"]["error"]


async def test_order_to_a_down_venue_fails_visibly(client: httpx.AsyncClient) -> None:
    r = await client.post("/orders", json={"instrument": "AAPL US", "quantity": 1000, "feeBps": 35,
                                           "override": True})
    assert r.status_code == 201 and r.json()["status"] == "FAILED"


# -- paste ------------------------------------------------------------------------------------------
def test_paste_chat_shorthand() -> None:
    p = paste.parse("can u locate 250k AAPL US, 75k MSFT @ 25bps pls\nhow's your day")
    assert p.kind == "CHAT"
    got = [(r.instrument.composite, r.quantity, r.fee_bps) for r in p.rows]
    assert got == [("AAPL US", 250_000, 25.0), ("MSFT US", 75_000, 25.0)]
    assert p.unparsed == ["how's your day"]


def test_paste_sheet_finds_isin_column_whatever_the_header() -> None:
    text = "Ref\tSec\tShares\tNote\n1\tUS9988883099\t50,000\tx\n2\tUS9950368293\t1.2m\t"
    p = paste.parse(text)
    assert p.kind == "TABLE"
    assert [(r.instrument.composite, r.quantity) for r in p.rows] == [("AAPL US", 50_000), ("MSFT US", 1_200_000)]
    roles = {c["name"]: c["role"] for c in p.columns}
    assert roles["Sec"] == "id" and roles["Shares"] == "qty"


def test_paste_bloomberg_sheet() -> None:
    text = ("Security\tPX_LAST\tEQY_SH_OUT\tQty\nAAPL US Equity\t344.4\t#N/A Requesting Data...\t20000\n"
            "MSFT US Equity\t#N/A N/A\t7400\t15000")
    p = paste.parse(text)
    assert p.kind == "BLOOMBERG"
    assert [r.instrument.composite for r in p.rows] == ["AAPL US", "MSFT US"]
    assert p.rows[0].extra == {"PX_LAST": "344.4"} and p.rows[1].quantity == 15_000


async def test_paste_apply_as_locates(client: httpx.AsyncClient) -> None:
    prev = (await client.post("/ingest/paste", json={"text": "100k AAPL US\n50k NVDA"})).json()
    assert prev["resolved"] == 2
    r = (await client.post(f"/ingest/paste/{prev['id']}/apply", json={"target": "locates",
                                                                      "counterparty": "PBALPHA"})).json()
    assert [x["channel"] for x in r["created"]] == ["PASTE", "PASTE"]


# -- the approval gate ----------------------------------------------------------------------------------
async def test_gate_edit_approve_and_kill_switch(client: httpx.AsyncClient) -> None:
    await _supply("I00057", 500_000)  # ARM, WARM
    loc = (await client.post("/locates", json={"counterparty": "PBALPHA", "lines": [
        {"identifier": "ARM US", "quantity": 60_000}]})).json()[0]
    async with db.session() as s:
        a = await gate.propose(s, kind="locate_decision", title="Approve ARM 60k", proposed_by="agent:test",
                               payload={"locateId": loc["id"], "decision": "APPROVED", "quantity": 60_000},
                               rationale="availability 8x request")
        await s.commit()
    queue = (await client.get("/actions")).json()
    assert a.id in [x["id"] for x in queue]
    await client.put("/kill-switch", json={"on": True})
    assert (await client.post(f"/actions/{a.id}/approve")).status_code == 409
    await client.put("/kill-switch", json={"on": False})
    r = (await client.post(f"/actions/{a.id}/approve", json={"payload": {
        "locateId": loc["id"], "decision": "PARTIAL", "quantity": 30_000}})).json()
    assert r["status"] == "EXECUTED" and r["result"]["qtyApproved"] == 30_000
    assert r["transcript"][-1]["step"] == "edited"
    trail = (await client.get("/events", params={"entity": "locate", "id": loc["id"]})).json()
    assert trail[0]["actor"] == "agent:test+trader:desk"


async def test_agent_off_by_default(client: httpx.AsyncClient) -> None:
    st = (await client.get("/agent/status")).json()
    assert st["mode"] == "off" and "create_action" in st["tools"]
    assert (await client.post("/agent/locates/x/review")).status_code == 503


async def test_locates_expire_at_end_of_day(setup: None) -> None:
    clock.pin(datetime(2026, 10, 28, 4, 0, tzinfo=UTC))  # past 23:59 New York
    try:
        async with db.session() as s:
            n = await locates.expire_due(s)
            await s.commit()
    finally:
        clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))
    assert n > 0
