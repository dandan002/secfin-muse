"""Desk behaviour with every outside system down: reference data, rules, locates, paste and the gate."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
import pytest

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


# -- locate filters ---------------------------------------------------------------------------------
async def _name(where: str, supply: int = 0) -> str:
    """An ISIN for a name matching ``where`` (restricted, threshold, penny or unsupplied), given ``supply``."""
    from sqlalchemy import select

    from loandesk import rules, secmaster
    from loandesk.models import Availability, Instrument

    async with db.session() as s:
        supplied = set((await s.execute(select(Availability.instrument_id))).scalars())
        for i in (await s.execute(select(Instrument).order_by(Instrument.id))).scalars():
            usd = secmaster.live(i.id)["lastPx"] * rules.FX_USD.get(i.ccy, 1.0)
            plain = not i.restricted and not i.threshold_flag and usd >= 1
            if {"restricted": i.restricted and not i.threshold_flag and usd >= 1,
                "threshold": i.threshold_flag and not i.restricted and usd >= 1,
                "penny": usd < 1 and not i.restricted and not i.threshold_flag,
                "unsupplied": plain and i.id not in supplied}[where] and i.id not in supplied:
                if supply:
                    await _supply(i.id, supply)
                return i.isin
    raise AssertionError(where)


async def test_filters_decline_before_anyone_sees_the_line(client: httpx.AsyncClient) -> None:
    await _supply("I00001", 2_000_000)  # keeps the availability feed fresh
    lines = [{"identifier": await _name(w, supply=0 if w == "unsupplied" else 50_000), "quantity": 1000}
             for w in ("restricted", "threshold", "penny", "unsupplied")]
    locs = (await client.post("/locates", json={"counterparty": "PBALPHA", "lines": lines})).json()
    assert {(x["route"], x["status"], x["decidedBy"]) for x in locs} == {("FILTER", "REJECTED", "policy:FILTER")}
    reasons = [x["routeReasons"][0] for x in locs]
    assert reasons[0] == "on the restricted list" and reasons[1].startswith("on the Reg SHO threshold list")
    assert re.fullmatch(r"priced at [A-Z]{3} [\d.]+ \(\$0\.\d\d\), under the \$1 minimum", reasons[2]), reasons[2]
    assert reasons[3] == "no availability"
    assert all(x["rationale"] == x["routeReasons"][0] for x in locs)


async def test_stale_feed_reviews_instead_of_declining(client: httpx.AsyncClient) -> None:
    clock.pin(datetime(2026, 10, 27, 16, 0, tzinfo=UTC))  # two hours past the last availability
    try:
        loc = (await client.post("/locates", json={"counterparty": "PBALPHA", "lines": [
            {"identifier": await _name("unsupplied"), "quantity": 1000}]})).json()[0]
    finally:
        clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))
    assert loc["status"] == "RECEIVED" and loc["route"] != "FILTER"


async def test_a_filter_can_hold_for_the_trader_instead(client: httpx.AsyncClient,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from loandesk.settings import get_settings

    monkeypatch.setattr(get_settings(), "filter_restricted", "hold")
    loc = (await client.post("/locates", json={"counterparty": "PBALPHA", "lines": [
        {"identifier": await _name("restricted", supply=50_000), "quantity": 1000}]})).json()[0]
    assert (loc["route"], loc["status"]) == ("HOLD", "RECEIVED") and "on the restricted list" in loc["routeReasons"]


# -- internal inventory -----------------------------------------------------------------------------
def test_flatten_nets_books_per_name() -> None:
    from loandesk.services import inventory

    flat = inventory.flatten([
        {"instrumentId": "I1", "side": "BORROW", "bookingEntity": "LOANDESK-US", "openQuantity": 100_000,
         "avgFeeBps": 30.0},
        {"instrumentId": "I1", "side": "LEND", "bookingEntity": "LOANDESK-US", "openQuantity": 20_000},
        {"instrumentId": "I1", "side": "LEND", "bookingEntity": "LOANDESK-UK", "openQuantity": 30_000},
        {"instrumentId": "I2", "side": "LEND", "bookingEntity": "LOANDESK-US", "openQuantity": 5_000}])
    assert flat["I1"]["net"] == 50_000 and flat["I1"]["costBps"] == 30.0
    assert flat["I1"]["books"]["LOANDESK-UK"] == {"borrowed": 0, "lent": 30_000}
    assert flat["I2"]["net"] == -5_000


async def test_locates_cross_internal_inventory_first(client: httpx.AsyncClient) -> None:
    from loandesk.services import inventory

    iid, isin = "I00002", (await client.get("/instruments/I00002/market")).json()["instrument"]["isin"]
    await _supply(iid, 500_000)
    async with db.session() as s:
        await inventory.apply(s, [{"instrumentId": iid, "side": "BORROW", "bookingEntity": "LOANDESK-US",
                                   "openQuantity": 80_000, "avgFeeBps": 25.0},
                                  {"instrumentId": iid, "side": "LEND", "bookingEntity": "LOANDESK-UK",
                                   "openQuantity": 20_000}])
        await s.commit()
    inv = next(r for r in (await client.get("/inventory")).json() if r["instrumentId"] == iid)
    assert (inv["net"], inv["free"], len(inv["books"])) == (60_000, 60_000, 2)
    loc = (await client.post("/locates", json={"counterparty": "PBALPHA", "lines": [
        {"identifier": isin, "quantity": 100_000}]})).json()[0]
    if loc["status"] == "RECEIVED":
        loc = (await client.post(f"/locates/{loc['id']}/decision", json={"decision": "APPROVED"})).json()
    basis = loc["regShoBasis"]
    assert "INTERNAL_INVENTORY:60000@" in basis and "(crossed 60000)" in basis
    assert basis.index("INTERNAL_INVENTORY") < basis.index("VENUE_A_AVAIL")
    mkt = (await client.get(f"/instruments/{iid}/market")).json()["market"]
    assert mkt["availableBy"] == {"internal": 0, "street": 460_000}
    assert mkt["internal"]["byBook"][0]["book"] == "LOANDESK-UK"

    async with db.session() as s:  # the box empties: the name's internal supply goes to zero, not stale
        await inventory.apply(s, [])
        await s.commit()
    assert (await client.get(f"/instruments/{iid}/market")).json()["market"]["internal"]["qty"] == 0


# -- pricing and counterparty limits ----------------------------------------------------------------
def test_fee_floor_is_a_bit_above_market() -> None:
    from loandesk import rules

    assert rules.fee_floor(40.0)["floorBps"] == 42.0  # +5%, at least 2 bps
    assert rules.fee_floor(10.0)["floorBps"] == 12.0  # the 2 bp minimum wins on cheap names
    assert rules.fee_floor(40.0, internal_cp=True)["floorBps"] == 40.0  # internal desks pay market
    assert rules.fee_floor(40.0, cost_bps=50.0)["floorBps"] == 52.5  # never under what the shares cost us


async def test_locates_quote_above_market_and_hold_the_floor(client: httpx.AsyncClient) -> None:
    from loandesk import secmaster

    isin = await _name("unsupplied")
    async with db.session() as s:
        from sqlalchemy import select

        from loandesk.models import Instrument
        inst = (await s.execute(select(Instrument).where(Instrument.isin == isin))).scalar_one()
    await _supply(inst.id, 500_000)
    market_fee = secmaster.live(inst.id)["feeBps"]
    loc = (await client.post("/locates", json={"counterparty": "KESTRELHF", "lines": [
        {"identifier": isin, "quantity": 10_000}]})).json()[0]
    assert loc["feeIndBps"] > market_fee
    if loc["status"] != "RECEIVED":
        return  # auto-approved under the ETB policy, at the quote
    under = await client.post(f"/locates/{loc['id']}/decision", json={"decision": "APPROVED", "feeBps": market_fee})
    assert under.status_code == 409 and "under the desk's floor" in under.json()["detail"]
    ok = (await client.post(f"/locates/{loc['id']}/decision", json={
        "decision": "APPROVED", "feeBps": market_fee, "overrideFeeFloor": True}, headers={"x-trader": "sam"})).json()
    assert ok["status"] == "APPROVED" and "overridden" in ok["rationale"]


async def test_lending_under_the_floor_fails_the_ticket(client: httpx.AsyncClient) -> None:
    p = (await client.post("/orders/preview", json={"side": "LEND", "instrument": "AAPL US", "quantity": 10_000,
                                                    "feeBps": 1.0, "counterparty": "KESTRELHF"})).json()
    floor = next(c for c in p["checks"] if c["name"] == "fee floor")
    assert not floor["ok"] and floor["blocking"]


async def test_counterparty_limits_hold_and_cap_approvals(client: httpx.AsyncClient,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from loandesk.settings import get_settings

    await _supply("I00003", 5_000_000)
    monkeypatch.setattr(get_settings(), "cp_locate_limit_pct", 0.0001)  # a tiny locate line for the test
    loc = (await client.post("/locates", json={"counterparty": "HARBORAL", "lines": [
        {"identifier": "I00003", "quantity": 900_000}]})).json()[0]
    assert loc["route"] == "HOLD" and any(r.startswith("locate limit:") for r in loc["routeReasons"]), loc
    r = await client.post(f"/locates/{loc['id']}/decision", json={"decision": "APPROVED"})
    assert r.status_code == 409 and "fits" in r.json()["detail"]
    room = next(x for x in (await client.get("/locates", params={"status": "RECEIVED"})).json()
                if x["id"] == loc["id"])["counterpartyRoom"]
    assert room["maxQty"] < 900_000 and any(f.startswith("locate limit") for f in room["failing"])

    monkeypatch.setattr(get_settings(), "blocked_counterparties", ["HARBORAL"])
    blocked = (await client.post("/locates", json={"counterparty": "HARBORAL", "lines": [
        {"identifier": "I00003", "quantity": 1000}]})).json()[0]
    assert (blocked["route"], blocked["status"]) == ("FILTER", "REJECTED")
    assert blocked["routeReasons"][0].startswith("counterparty blocked")
