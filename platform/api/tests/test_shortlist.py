"""The prime broker's day: a locate is a quantity at an agreed fee, entered and good until the close; the client
shorts against it or not; the stock record lands after the close and the short list is what the desk must borrow
to deliver. Covers go to the gate as borrow orders, or as a bid in chat to an agent lender for a hot name."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from loandesk import db, secmaster, sync
from loandesk.adapters import systems
from loandesk.ingest import chat
from loandesk.models import Action, ChatMessage, Counterparty, Instrument, Locate
from loandesk.services import inventory, locates, market


async def _names(tier: str, n: int) -> list[Instrument]:
    async with db.session() as s:
        insts = (await s.execute(select(Instrument).where(Instrument.ccy == "USD").order_by(Instrument.id))
                 ).scalars().all()
    return [i for i in insts if secmaster.live(i.id)["tier"] == tier and (secmaster.live(i.id)["lastPx"] or 0) > 5
            ][:n]


async def _supply(instrument_id: str, qty: int) -> None:
    async with db.session() as s:
        await market.upsert_availability(s, "VENUE_A", [{"instrumentId": instrument_id, "quantity": qty, "lenders": 3,
                                                         "bestFeeBps": 20.0, "asOf": "2026-10-27T13:59:00Z"}])
        await s.commit()


def _gateway(monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any]) -> None:
    async def stock_record(trade_date: str | None = None) -> dict[str, Any]:
        return rec

    monkeypatch.setattr(systems().gateway, "stock_record", stock_record)


async def _api_locate(ref: str, inst: Instrument, qty: int, cp: str = "KESTRELHF") -> Locate:
    await _supply(inst.id, 5_000_000)
    async with db.session() as s:
        [loc] = await locates.intake(s, counterparty=cp, channel="API", request_ref=ref,
                                     lines=[locates.LineIn(inst.isin, qty, line_no=1)])
        if loc.status == "RECEIVED":
            await locates.decide(s, loc, "APPROVED", qty, by="trader:sam", reply=False, override_floor=True)
        await s.commit()
        return loc


async def test_a_locate_is_a_quantity_at_a_fee_entered_and_good_until_the_close(client: httpx.AsyncClient) -> None:
    [inst] = await _names("WARM", 1)
    await _supply(inst.id, 5_000_000)
    [loc] = (await client.post("/locates", json={"counterparty": "KESTRELHF", "lines": [
        {"identifier": inst.isin, "quantity": 10_000}]})).json()
    assert loc["feeBps"] and loc["enteredAt"] is None  # quoted, not granted
    r = await client.post(f"/locates/{loc['id']}/decision", json={"decision": "APPROVED"},
                          headers={"x-trader": "sam"})
    assert r.status_code == 200, r.text
    ok = r.json()
    assert ok["qtyApproved"] == 10_000 and ok["feeBps"] == loc["feeBps"]
    assert ok["enteredAt"] == "2026-10-27T14:00:00.000Z" and ok["expiresAt"] == "2026-10-27T20:00:00.000Z"  # 16:00 NY
    [back] = [x for x in (await client.get("/locates", params={"status": "APPROVED"})).json() if x["id"] == loc["id"]]
    assert back["expiresAt"] == "2026-10-27T20:00:00.000Z"  # as stored, not just as decided
    [loc2] = (await client.post("/locates", json={"counterparty": "KESTRELHF", "lines": [
        {"identifier": inst.isin, "quantity": 5_000}]})).json()
    past = await client.post(f"/locates/{loc2['id']}/decision", json={
        "decision": "APPROVED", "expiresAt": "2026-10-27T13:00:00Z"})
    assert past.status_code == 409 and "already passed" in past.json()["detail"]
    longer = (await client.post(f"/locates/{loc2['id']}/decision", json={
        "decision": "APPROVED", "expiresAt": "2026-10-28T20:00:00Z"})).json()
    assert longer["expiresAt"] == "2026-10-28T20:00:00.000Z"  # a trader can give a longer one


async def test_a_client_limit_under_our_floor_waits_for_the_trader(client: httpx.AsyncClient) -> None:
    [inst] = await _names("GC", 1)
    await _supply(inst.id, 5_000_000)
    [loc] = (await client.post("/locates", json={"counterparty": "KESTRELHF", "lines": [
        {"identifier": inst.isin, "quantity": 1_000, "maxFeeBps": 0.01}]})).json()
    assert loc["status"] == "RECEIVED" and loc["route"] == "HOLD" and loc["feeLimitBps"] == 0.01
    assert "under our floor" in loc["routeReasons"][0]
    ok = (await client.post(f"/locates/{loc['id']}/decision", json={"decision": "APPROVED"},
                            headers={"x-trader": "sam"})).json()
    assert "over the client's limit" in ok["rationale"]  # granted anyway: the client may well go elsewhere


async def test_the_stock_record_uses_locates_and_builds_the_short_list(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = await _names("GC", 2)
    la, lb = await _api_locate("LOC-T3-A", a, 80_000), await _api_locate("LOC-T3-B", b, 30_000)
    rec = {"tradeDate": "2026-10-23", "settlementDate": "2026-10-26", "closedAt": "2026-10-23T20:00:00Z",
           "positions": [{"counterparty": "KESTRELHF", "instrumentId": a.id, "shortQuantity": 60_000,
                          "soldQuantity": 60_000, "coveredQuantity": 0},
                         {"counterparty": "CEDARHF", "instrumentId": a.id, "shortQuantity": 15_000,
                          "soldQuantity": 0, "coveredQuantity": 5_000}],
           "shortSales": [],
           "locateUsage": [{"locateId": "LOC-T3-A", "lineNo": 1, "approvedQuantity": 80_000, "usedQuantity": 60_000},
                           {"locateId": "LOC-T3-B", "lineNo": 1, "approvedQuantity": 30_000, "usedQuantity": 0}]}
    _gateway(monkeypatch, rec)
    await sync.sync_stock_record()
    async with db.session() as s:
        assert (await s.get(Locate, la.id)).status == "USED" and (await s.get(Locate, la.id)).qty_used == 60_000
        assert (await s.get(Locate, lb.id)).status == "EXPIRED"  # the client went elsewhere
    sl = (await client.get("/short-list", params={"date": "2026-10-23"})).json()
    assert sl["settlementDate"] == "2026-10-26"
    [ln] = sl["lines"]
    assert (ln["instrumentId"], ln["clientShort"], ln["box"], ln["shortfall"]) == (a.id, 75_000, 0, 75_000)
    assert [c["counterparty"] for c in ln["clients"]] == ["KESTRELHF", "CEDARHF"]
    [card] = ln["drafted"]  # drafted as the record landed; the trader approves it
    assert ln["status"] == "WORKING" and ln["uncovered"] == 0
    async with db.session() as s:
        act = await s.get(Action, card["actionId"])
    assert (act.kind, act.payload["side"], act.payload["quantity"]) == ("order", "BORROW", 75_000)
    assert act.payload["feeBps"] > secmaster.live(a.id)["feeBps"]  # over market, so a firm bid fills
    assert "75,000 to borrow" in act.rationale
    assert (await client.post("/short-list/draft")).json()["drafted"] == []  # one card per name
    await sync.sync_stock_record()  # the same record again: nothing changes

    # The box covers the name once the borrow is on our books at the processor.
    async with db.session() as s:
        await inventory.apply(s, [{"instrumentId": a.id, "side": "BORROW", "openQuantity": 75_000,
                                   "bookingEntity": "LOANDESK-US", "avgFeeBps": 12.0}])
        await s.commit()
    [ln] = (await client.get("/short-list", params={"date": "2026-10-23"})).json()["lines"]
    assert (ln["box"], ln["shortfall"], ln["status"]) == (75_000, 0, "COVERED")


async def test_a_hot_cover_is_a_bid_in_chat_and_the_lenders_done_drafts_the_borrow(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    [hot] = await _names("HOT", 1)
    async with db.session() as s:
        await chat.ingest(s, {"messageId": "m-harbor-1", "streamId": "room-harbor-avails", "seq": 1,
                              "ts": "2026-10-27T13:00:00Z",
                              "sender": {"handle": "mdiaz.harbor", "kind": "persona", "cpCode": "HARBORAL"},
                              "text": "morning all"})
        await s.commit()
    _gateway(monkeypatch, {"tradeDate": "2026-10-26", "settlementDate": "2026-10-27",
                           "closedAt": "2026-10-26T20:00:00Z", "shortSales": [], "locateUsage": [],
                           "positions": [{"counterparty": "JUNIPERHF", "instrumentId": hot.id,
                                          "shortQuantity": 120_000, "soldQuantity": 120_000,
                                          "coveredQuantity": 0}]})
    await sync.sync_stock_record()
    [ln] = (await client.get("/short-list", params={"date": "2026-10-26"})).json()["lines"]
    [card] = ln["drafted"]
    assert card["kind"] == "chat_send" and ln["status"] == "WORKING"
    async with db.session() as s:
        act = await s.get(Action, card["actionId"])
        room = act.payload["roomId"]  # the agent lender we last spoke to (other tests' rooms may be newer)
        cp = (await s.execute(select(ChatMessage.counterparty).where(ChatMessage.stream_id == room).limit(1))).scalar()
        assert (await s.get(Counterparty, cp)).kind == "AGENT_LENDER"
    assert act.payload["text"].startswith(f"Bid 120k {hot.composite} @ ")

    # Our bid goes out; the lender's "done" is a borrow we agreed, so it drafts the order to them.
    async with db.session() as s:
        await chat.ingest(s, {"messageId": "m-us-1", "streamId": room, "seq": 10_002, "ts": "2026-10-27T14:00:00Z",
                              "sender": {"handle": "loandesk.bot", "kind": "bot"}, "text": act.payload["text"]})
        m = await chat.ingest(s, {"messageId": "m-harbor-2", "streamId": room, "seq": 10_003,
                                  "ts": "2026-10-27T14:01:00Z",
                                  "sender": {"handle": "mdiaz.harbor", "kind": "persona", "cpCode": "HARBORAL"},
                                  "text": f"done - 120k {hot.composite} @ 900bps open, ref HAR-12345"})
        await s.commit()
    assert (m.intent, m.status, m.our_side) == ("ACCEPT", "AGREED", "BORROW")
    async with db.session() as s:
        order = (await s.execute(select(Action).where(Action.source_ref == m.id))).scalar_one()
    assert (order.kind, order.payload["side"], order.payload["counterparty"]) == ("order", "BORROW", "HARBORAL")
    assert order.payload["quantity"] == 120_000 and order.payload["feeBps"] == 900


async def test_a_bid_out_in_chat_is_working_until_the_lender_counters_then_the_cover_goes_to_a_venue(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    hot = (await _names("HOT", 2))[1]
    sent: list[tuple[str, str]] = []

    async def send(room: str, text: str, client_msg_id: str | None = None) -> dict[str, Any]:
        sent.append((room, text))
        return {"id": f"m-{len(sent)}"}

    monkeypatch.setattr(systems().chat, "send", send)
    _gateway(monkeypatch, {"tradeDate": "2026-10-27", "settlementDate": "2026-10-28",
                           "closedAt": "2026-10-27T20:00:00Z", "shortSales": [], "locateUsage": [],
                           "positions": [{"counterparty": "CEDARHF", "instrumentId": hot.id, "shortQuantity": 30_000,
                                          "soldQuantity": 30_000, "coveredQuantity": 0}]})
    await sync.sync_stock_record()
    [ln] = (await client.get("/short-list", params={"date": "2026-10-27"})).json()["lines"]
    [card] = ln["drafted"]
    assert card["kind"] == "chat_send"
    assert (await client.post(f"/actions/{card['actionId']}/approve")).json()["status"] == "EXECUTED"
    room, text = sent[-1]
    [ln] = (await client.get("/short-list", params={"date": "2026-10-27"})).json()["lines"]
    assert (ln["status"], ln["working"], ln["drafted"]) == ("WORKING", 30_000, [])  # the bid is out

    async with db.session() as s:
        m = await chat.ingest(s, {"messageId": "m-counter-1", "streamId": room, "seq": 20_001,
                                  "ts": "2026-10-27T14:05:00Z",
                                  "sender": {"handle": "mdiaz.harbor", "kind": "persona", "cpCode": "HARBORAL"},
                                  "text": f"best I can do on {hot.composite} is 9000bps"})
        await s.commit()
    assert m.intent == "COUNTER"
    [ln] = (await client.get("/short-list", params={"date": "2026-10-27"})).json()["lines"]
    assert (ln["status"], ln["working"]) == ("SHORT", 0)  # answered, and not done: the trader's call again
    [again] = (await client.post("/short-list/draft")).json()["drafted"]
    async with db.session() as s:
        a = await s.get(Action, again)
    assert a.kind == "order" and "didn't do it" in a.rationale  # not a second bid in the same chat


async def test_a_borrow_booked_since_the_box_was_read_still_covers(client: httpx.AsyncClient,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Between a fill booking and the next read of the processor's positions, the borrow is in neither: the short
    list must still count it, or the next draft borrows the name twice."""
    from datetime import UTC, date, datetime

    from loandesk import clock
    from loandesk.models import Trade

    [name] = (await _names("GC", 6))[5:]
    async with db.session() as s:
        await inventory.apply(s, [])  # the box, read at 14:00: nothing in this name
        await s.commit()
    _gateway(monkeypatch, {"tradeDate": "2026-10-28", "settlementDate": "2026-10-29",
                           "closedAt": "2026-10-28T20:00:00Z", "shortSales": [], "locateUsage": [],
                           "positions": [{"counterparty": "BASALTHF", "instrumentId": name.id, "shortQuantity": 40_000,
                                          "soldQuantity": 40_000, "coveredQuantity": 0}]})
    clock.pin(datetime(2026, 10, 27, 14, 10, tzinfo=UTC))
    try:
        async with db.session() as s:
            s.add(Trade(id="trd_box_lag_1", venue="VENUE_A", venue_trade_id="NGT-T-BOXLAG1", uti="UTIBOXLAG1",
                        side="BORROW", instrument_id=name.id, cp_code="HARBORAL", qty=40_000, open_qty=40_000,
                        fee_bps=20.0, price=50.0, ccy="USD", settle_date=date(2026, 10, 29), venue_json={},
                        executed_at=clock.now(), sfp_contract_id="SFP-C-BOXLAG1", booked_at=clock.now(),
                        loan_status="OPEN", lifecycle=[]))
            await s.commit()
        await sync.sync_stock_record()
        [ln] = (await client.get("/short-list", params={"date": "2026-10-28"})).json()["lines"]
        assert (ln["box"], ln["unbooked"], ln["shortfall"], ln["status"]) == (0, 40_000, 0, "COVERED")
        assert (await client.post("/short-list/draft")).json()["drafted"] == []
    finally:
        clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))
