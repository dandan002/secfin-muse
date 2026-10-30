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


def _short(sl: dict[str, Any]) -> list[dict[str, Any]]:
    """The lines the clients are short (the list also carries names we hold borrows in, from other tests)."""
    return [x for x in sl["lines"] if x["clientShort"]]


async def _line(client: httpx.AsyncClient, trade_date: str, instrument_id: str) -> dict[str, Any]:
    sl = (await client.get("/short-list", params={"date": trade_date})).json()
    return next(x for x in sl["lines"] if x["instrumentId"] == instrument_id)


async def _lendable(instrument_id: str) -> int:
    """Our own box's lendable supply in a name (the INTERNAL availability row)."""
    from loandesk.models import Availability

    async with db.session() as s:
        a = (await s.execute(select(Availability).where(Availability.source == "INTERNAL",
                                                        Availability.instrument_id == instrument_id))).scalar()
    return a.qty if a else 0


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
    [ln] = _short(sl)
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
    ln = await _line(client, "2026-10-23", a.id)
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
    ln = await _line(client, "2026-10-26", hot.id)
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
    ln = await _line(client, "2026-10-27", hot.id)
    [card] = ln["drafted"]
    assert card["kind"] == "chat_send"
    assert (await client.post(f"/actions/{card['actionId']}/approve")).json()["status"] == "EXECUTED"
    room, text = sent[-1]
    ln = await _line(client, "2026-10-27", hot.id)
    assert (ln["status"], ln["working"], ln["drafted"]) == ("WORKING", 30_000, [])  # the bid is out

    async with db.session() as s:
        m = await chat.ingest(s, {"messageId": "m-counter-1", "streamId": room, "seq": 20_001,
                                  "ts": "2026-10-27T14:05:00Z",
                                  "sender": {"handle": "mdiaz.harbor", "kind": "persona", "cpCode": "HARBORAL"},
                                  "text": f"best I can do on {hot.composite} is 9000bps"})
        await s.commit()
    assert m.intent == "COUNTER"
    ln = await _line(client, "2026-10-27", hot.id)
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
        ln = await _line(client, "2026-10-28", name.id)
        assert (ln["box"], ln["unbooked"], ln["shortfall"], ln["status"]) == (0, 40_000, 0, "COVERED")
        assert (await client.post("/short-list/draft")).json()["drafted"] == []
    finally:
        clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))


async def test_borrows_the_clients_no_longer_need_are_returned_dearest_first(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The box holds more than the clients are short: return the excess, the dearest borrow first, and never a
    failing one. A return made before the box is read again must not be drafted a second time."""
    from datetime import UTC, date, datetime

    from loandesk import clock
    from loandesk.models import Trade

    x, y = (await _names("GC", 8))[6:8]  # x: clients short 30k of a 60k box; y: nobody short any more
    calls: list[tuple[str, str, int | None]] = []

    async def lifecycle(contract_id: str, event: str, quantity: int | None = None,
                        fee_bps: float | None = None) -> dict[str, Any]:
        calls.append((event, contract_id, quantity))
        return {}

    async def instruct(body: dict[str, Any]) -> dict[str, Any]:
        return {"instruction": {**body, "instructionId": f"CUST-RET-{len(calls)}", "status": "ACCEPTED"}}

    monkeypatch.setattr(systems().processor, "lifecycle", lifecycle)
    monkeypatch.setattr(systems().custodian, "instruct", instruct)

    def borrow(tid: str, inst: Instrument, qty: int, fee: float, settle: str = "SETTLED") -> Trade:
        at = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)  # booked before the box was read: it's in the box
        return Trade(id=tid, venue="VENUE_A", venue_trade_id=f"NGT-{tid}", uti=f"UTI{tid}", side="BORROW",
                     instrument_id=inst.id, cp_code="HARBORAL", qty=qty, open_qty=qty, fee_bps=fee, price=50.0,
                     ccy="USD", settle_date=date(2026, 10, 28), venue_json={}, executed_at=at,
                     sfp_contract_id=f"SFP-{tid}", booked_at=at, loan_status="OPEN", settle_status=settle,
                     lifecycle=[])

    async with db.session() as s:
        s.add_all([borrow("trd_xs_cheap", x, 40_000, 20.0), borrow("trd_xs_dear", x, 20_000, 60.0),
                   borrow("trd_xs_y", y, 25_000, 15.0), borrow("trd_xs_yfail", y, 10_000, 90.0, "FAILING")])
        await inventory.apply(s, [{"instrumentId": x.id, "side": "BORROW", "openQuantity": 60_000,
                                   "bookingEntity": "LOANDESK-US", "avgFeeBps": 33.0},
                                  {"instrumentId": y.id, "side": "BORROW", "openQuantity": 35_000,
                                   "bookingEntity": "LOANDESK-US", "avgFeeBps": 36.0}])
        await s.commit()
    _gateway(monkeypatch, {"tradeDate": "2026-10-29", "settlementDate": "2026-10-30",
                           "closedAt": "2026-10-29T20:00:00Z", "shortSales": [], "locateUsage": [],
                           "positions": [{"counterparty": "CEDARHF", "instrumentId": x.id, "shortQuantity": 30_000,
                                          "soldQuantity": 0, "coveredQuantity": 10_000}]})
    await sync.sync_stock_record()  # drafts the returns along with any covers

    lx = await _line(client, "2026-10-29", x.id)
    assert (lx["clientShort"], lx["cover"], lx["excess"], lx["status"]) == (30_000, 60_000, 30_000, "EXCESS")
    assert lx["excessCostUsd"] > 0 and lx["toReturn"] == 0  # all of it is at the gate
    async with db.session() as s:
        cards = [await s.get(Action, r["actionId"]) for r in lx["returning"]]
    assert sorted((a.payload["tradeId"], a.payload["quantity"]) for a in cards) == [
        ("trd_xs_cheap", 10_000), ("trd_xs_dear", 20_000)]  # the 60 bps borrow goes back whole
    assert all(a.kind == "lifecycle" and a.payload["event"] == "RETURN" for a in cards)
    assert "30,000 more than they need" in cards[0].rationale

    ly = await _line(client, "2026-10-29", y.id)
    assert (ly["clientShort"], ly["fails"], ly["excess"], ly["status"]) == (0, 10_000, 25_000, "EXCESS")
    [ry] = ly["returning"]
    async with db.session() as s:
        assert (await s.get(Action, ry["actionId"])).payload == {"tradeId": "trd_xs_y", "event": "RETURN",
                                                                 "quantity": 25_000}  # not the failing one
    assert (await client.post("/short-list/draft")).json()["drafted"] == []  # one card per borrow

    clock.pin(datetime(2026, 10, 27, 14, 5, tzinfo=UTC))
    try:
        for a in cards:
            assert (await client.post(f"/actions/{a.id}/approve")).json()["status"] == "EXECUTED"
        assert sorted(calls) == [("RETURN", "SFP-trd_xs_cheap", 10_000), ("RETURN", "SFP-trd_xs_dear", 20_000)]
        lx = await _line(client, "2026-10-29", x.id)  # the box still shows them until it's read again
        assert (lx["box"], lx["returnedSinceBox"], lx["excess"], lx["status"]) == (60_000, 30_000, 0, "COVERED")
        assert (await client.post("/short-list/draft")).json()["drafted"] == []
        async with db.session() as s:
            await inventory.apply(s, [{"instrumentId": x.id, "side": "BORROW", "openQuantity": 30_000,
                                       "bookingEntity": "LOANDESK-US", "avgFeeBps": 20.0},
                                      {"instrumentId": y.id, "side": "BORROW", "openQuantity": 35_000,
                                       "bookingEntity": "LOANDESK-US", "avgFeeBps": 36.0}])
            await s.commit()
        lx = await _line(client, "2026-10-29", x.id)
        assert (lx["box"], lx["returnedSinceBox"], lx["excess"]) == (30_000, 0, 0)
    finally:
        clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))


async def test_a_lenders_recall_comes_off_the_cover_gets_a_return_and_the_short_list_redrafts(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clients short 50k; we borrow 60k from Harbor and 20k from Northgate, so 30k is drafted back as excess. Then
    Harbor recalls 40k: that's owed back, not cover. The recall gets its own return card on Harbor's borrow, the
    excess returns are superseded (nothing is spare now), and the 10k the clients still need gets a cover."""
    from datetime import UTC, date, datetime

    from loandesk import clock
    from loandesk.models import Trade

    [z] = (await _names("GC", 10))[9:]

    async def lifecycle(contract_id: str, event: str, quantity: int | None = None,
                        fee_bps: float | None = None) -> dict[str, Any]:
        return {}

    async def instruct(body: dict[str, Any]) -> dict[str, Any]:
        return {"instruction": {**body, "instructionId": "CUST-RCL-1", "status": "ACCEPTED"}}

    monkeypatch.setattr(systems().processor, "lifecycle", lifecycle)
    monkeypatch.setattr(systems().custodian, "instruct", instruct)
    at = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)

    def borrow(tid: str, cp: str, qty: int, fee: float) -> Trade:
        return Trade(id=tid, venue="VENUE_A", venue_trade_id=f"NGT-{tid}", uti=f"UTI{tid}", side="BORROW",
                     instrument_id=z.id, cp_code=cp, qty=qty, open_qty=qty, fee_bps=fee, price=50.0, ccy="USD",
                     settle_date=date(2026, 10, 28), venue_json={}, executed_at=at, sfp_contract_id=f"SFP-{tid}",
                     booked_at=at, loan_status="OPEN", settle_status="SETTLED", lifecycle=[])

    async with db.session() as s:
        s.add_all([borrow("trd_rcl_harbor", "HARBORAL", 60_000, 20.0),
                   borrow("trd_rcl_north", "NORTHAL", 20_000, 25.0)])
        await inventory.apply(s, [{"instrumentId": z.id, "side": "BORROW", "openQuantity": 80_000,
                                   "bookingEntity": "LOANDESK-US", "avgFeeBps": 21.0}])
        await s.commit()
    _gateway(monkeypatch, {"tradeDate": "2026-10-30", "settlementDate": "2026-11-02",
                           "closedAt": "2026-10-30T20:00:00Z", "shortSales": [], "locateUsage": [],
                           "positions": [{"counterparty": "SPRUCEHF", "instrumentId": z.id, "shortQuantity": 50_000,
                                          "soldQuantity": 50_000, "coveredQuantity": 0}]})
    await sync.sync_stock_record()
    ln = await _line(client, "2026-10-30", z.id)
    assert (ln["excess"], len(ln["returning"])) == (30_000, 2)
    excess_cards = [r["actionId"] for r in ln["returning"]]
    assert await _lendable(z.id) == 80_000

    async with db.session() as s:
        m = await chat.ingest(s, {"messageId": "m-recall-1", "streamId": "room-harbor-avails", "seq": 30_001,
                                  "ts": "2026-10-27T15:00:00Z",
                                  "sender": {"handle": "mdiaz.harbor", "kind": "persona", "cpCode": "HARBORAL"},
                                  "text": f"Recall notice: {z.composite} 40,000, ref HAR-777, please return by T+2. "
                                          f"Thanks!"})
        await s.commit()
    assert (m.intent, m.status) == ("RECALL", "ACTIONED"), m.note
    assert await _lendable(z.id) == 40_000  # owed back: off the lendable box the moment the recall lands
    async with db.session() as s:  # and it stays off when the box is read again, until the shares go back
        await inventory.apply(s, [{"instrumentId": z.id, "side": "BORROW", "openQuantity": 80_000,
                                   "bookingEntity": "LOANDESK-US", "avgFeeBps": 21.0}])
        await s.commit()
    assert await _lendable(z.id) == 40_000
    async with db.session() as s:
        t = await s.get(Trade, "trd_rcl_harbor")
        assert (t.recalled_qty, t.recall_due, t.recall_ref) == (40_000, date(2026, 10, 29), "HAR-777")
        assert [(await s.get(Action, i)).status for i in excess_cards] == ["SUPERSEDED"] * 2
        [card] = (await s.execute(select(Action).where(Action.source_ref == "recall:m-recall-1"))).scalars().all()
    assert card.payload == {"tradeId": "trd_rcl_harbor", "event": "RETURN", "quantity": 40_000}
    assert "recall from HARBORAL: return 40,000" in card.title and "by 2026-10-29" in card.title

    ln = await _line(client, "2026-10-30", z.id)
    assert (ln["recalled"], ln["cover"], ln["shortfall"], ln["excess"]) == (40_000, 40_000, 10_000, 0)
    [cover] = ln["drafted"]  # redrafted on the recall: borrow the 10k the clients still need
    async with db.session() as s:
        cover = await s.get(Action, cover["actionId"])
    assert (cover.kind, cover.payload["side"], cover.payload["quantity"]) == ("order", "BORROW", 10_000)
    assert "recalled 40,000" in cover.rationale
    assert ln["returning"] == []  # the recall's return isn't excess

    clock.pin(datetime(2026, 10, 27, 14, 5, tzinfo=UTC))
    try:
        assert (await client.post(f"/actions/{card.id}/approve")).json()["status"] == "EXECUTED"
        [tr] = [x for x in (await client.get("/trades")).json() if x["id"] == "trd_rcl_harbor"]
        # Returned on the books, but the recall stands until the shares settle back with the lender.
        assert (tr["openQty"], tr["recalledQty"], tr["recallDue"], tr["recallStatus"]) == (
            20_000, 0, "2026-10-29", "RETURNING")
        assert tr["lifecycle"][-1]["againstRecall"] == 40_000
        assert await _lendable(z.id) == 40_000  # not handed back to the box on the way out
        async with db.session() as s:
            from loandesk.services import booking

            await booking.apply_settlement(s, [{"instructionId": "CUST-RCL-1", "status": "FAILING"}])
            await s.commit()
        [tr] = [x for x in (await client.get("/trades")).json() if x["id"] == "trd_rcl_harbor"]
        assert (tr["recallStatus"], tr["recallDue"]) == ("RETURN_FAILING", "2026-10-29")
        async with db.session() as s:
            await booking.apply_settlement(s, [{"instructionId": "CUST-RCL-1", "status": "SETTLED"}])
            await s.commit()
        [tr] = [x for x in (await client.get("/trades")).json() if x["id"] == "trd_rcl_harbor"]
        assert (tr["recallStatus"], tr["recallDue"], tr["lifecycle"][-1]["settleStatus"]) == (None, None, "SETTLED")
        ln = await _line(client, "2026-10-30", z.id)  # returned, not yet off the box: the same 40k of cover
        assert (ln["recalled"], ln["returnedSinceBox"], ln["cover"], ln["shortfall"]) == (0, 40_000, 40_000, 10_000)
    finally:
        clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))


async def test_a_recall_we_cant_match_waits_for_the_trader(client: httpx.AsyncClient) -> None:
    [w] = (await _names("GC", 11))[10:]
    async with db.session() as s:
        m = await chat.ingest(s, {"messageId": "m-recall-2", "streamId": "room-harbor-avails", "seq": 30_002,
                                  "ts": "2026-10-27T15:01:00Z",
                                  "sender": {"handle": "mdiaz.harbor", "kind": "persona", "cpCode": "HARBORAL"},
                                  "text": f"Sorry - beneficial owner is selling, recalling 25k {w.composite} "
                                          f"(HAR-55501), T+1"})
        await s.commit()
    assert (m.intent, m.status) == ("RECALL", "OPEN")
    assert "recalls 25,000 more than we borrow from them" in m.note


async def test_a_recall_from_the_processor_is_put_on_the_borrow_once(client: httpx.AsyncClient,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A Venue A fill has no venue lifecycle: its lender's recall arrives at the processor, on the contract."""
    from datetime import UTC, date, datetime

    from loandesk.models import Trade

    [v] = (await _names("GC", 12))[11:]
    at = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    async with db.session() as s:
        s.add(Trade(id="trd_prc_1", venue="VENUE_A", venue_trade_id="NGT-T-PRC1", uti="UTIPRC1", side="BORROW",
                    instrument_id=v.id, cp_code="MERIDIAL", qty=50_000, open_qty=50_000, fee_bps=18.0, price=40.0,
                    ccy="USD", settle_date=date(2026, 10, 28), venue_json={}, executed_at=at,
                    sfp_contract_id="SFP-C-PRC1", booked_at=at, loan_status="OPEN", settle_status="SETTLED",
                    lifecycle=[]))
        await s.commit()
    notices = [{"seq": 7, "recallId": "SFP-R-000007", "contractId": "SFP-C-PRC1", "counterparty": "MERIDIAL",
                "status": "OPEN", "quantity": 20_000, "outstanding": 20_000, "dueDate": "2026-10-28"}]

    async def recalls(after: int = 0) -> list[dict[str, Any]]:
        return [n for n in notices if n["seq"] > after]

    monkeypatch.setattr(systems().processor, "recalls", recalls)
    await sync.sync_recalls()
    await sync.sync_recalls()  # the cursor: the same notice isn't taken twice
    async with db.session() as s:
        t = await s.get(Trade, "trd_prc_1")
        assert (t.recalled_qty, t.recall_due, t.recall_ref) == (20_000, date(2026, 10, 28), "SFP-R-000007")
        [card] = (await s.execute(select(Action).where(Action.source_ref == "recall:SFP-R-000007"))).scalars().all()
    assert card.payload == {"tradeId": "trd_prc_1", "event": "RETURN", "quantity": 20_000}
    assert "at the processor" in card.rationale and "buy the shares in" in card.rationale


def _cns(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """The gateway's CNS fails feed, and a record of what we delivered against them."""
    delivered: list[tuple[str, int]] = []

    async def fails(after: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        return [r for r in rows if r["seq"] > after]

    async def deliver_fail(fail_id: str, quantity: int, ref: str | None = None) -> dict[str, Any]:
        delivered.append((fail_id, quantity))
        return {"failId": fail_id}

    monkeypatch.setattr(systems().gateway, "fails", fails)
    monkeypatch.setattr(systems().gateway, "deliver_fail", deliver_fail)
    return delivered


def _fail(seq: int, fail_id: str, cp: str, iid: str, settle: str, qty: int) -> dict[str, Any]:
    return {"seq": seq, "failId": fail_id, "counterparty": cp, "instrumentId": iid, "settlementDate": settle,
            "quantity": qty, "outstanding": qty, "status": "OPEN"}


async def test_a_short_that_fails_at_cns_is_closed_out_by_the_open_and_delivered_once_the_shares_settle(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 204(a): a client's short fails to deliver on its settlement date; we close it out before regular trading
    opens the next business day. The cover card says so; a borrow closes it out; once the shares settle into our
    account, they're delivered against the fail."""
    from datetime import UTC, date, datetime

    from loandesk import clock
    from loandesk.models import DeliveryFail, Trade

    [name] = (await _names("GC", 16))[14:15]
    delivered = _cns(monkeypatch, [_fail(1, "CNS-F-000001", "BASALTHF", name.id, "2026-11-03", 30_000)])
    clock.pin(datetime(2026, 11, 3, 21, 30, tzinfo=UTC))  # 16:30 New York, after the fail
    try:
        async with db.session() as s:
            await inventory.apply(s, [])  # nothing in the box
            await s.commit()
        _gateway(monkeypatch, {"tradeDate": "2026-11-02", "settlementDate": "2026-11-03",
                               "closedAt": "2026-11-02T21:00:00Z", "shortSales": [], "locateUsage": [],
                               "positions": [{"counterparty": "BASALTHF", "instrumentId": name.id,
                                              "shortQuantity": 30_000, "soldQuantity": 30_000, "coveredQuantity": 0}]})
        await sync.sync_stock_record()  # the cover is drafted before the fail is known
        await sync.sync_fails()
        await sync.sync_fails()  # the cursor: the same fail isn't taken twice
        async with db.session() as s:
            [old] = (await s.execute(select(Action).where(Action.source_ref == f"short:{name.id}",
                                                          Action.status == "SUPERSEDED"))).scalars().all()
        sl = (await client.get("/short-list")).json()
        assert sl["lines"][0]["instrumentId"] == name.id  # a close-out due comes first
        ln = sl["lines"][0]
        assert (ln["cnsFails"], ln["closeOutBy"], ln["preBorrow"], ln["shortfall"]) == (
            30_000, "2026-11-04T14:30:00.000Z", False, 30_000)  # 09:30 New York the next morning
        async with db.session() as s:
            card = (await s.execute(select(Action).where(Action.source_ref == f"short:{name.id}",
                                                         Action.status == "PENDING"))).scalar()
        assert "Rule 204 close-out by 09:30 ET on Wed 04 Nov" in card.rationale
        assert "warning: Rule 204 close-out due by 09:30 ET on Wed 04 Nov" in card.risk_flags

        # The borrow fills overnight: closed out in time, but the shares haven't settled to deliver.
        clock.pin(datetime(2026, 11, 4, 13, 0, tzinfo=UTC))
        async with db.session() as s:
            s.add(Trade(id="trd_cns_1", venue="VENUE_A", venue_trade_id="NGT-T-CNS1", uti="UTICNS1", side="BORROW",
                        instrument_id=name.id, cp_code="HARBORAL", qty=30_000, open_qty=30_000, fee_bps=20.0,
                        price=50.0, ccy="USD", settle_date=date(2026, 11, 4), venue_json={}, executed_at=clock.now(),
                        loan_status="OPEN", settle_status="PENDING", lifecycle=[]))
            await s.commit()
        await sync.sync_fails()
        [f] = (await client.get("/fails")).json()
        assert (f["status"], f["missed"], f["openQty"]) == ("CLOSED_OUT", False, 30_000) and delivered == []
        assert (await _line(client, "2026-11-02", name.id))["closeOutBy"] is None

        clock.pin(datetime(2026, 11, 4, 15, 0, tzinfo=UTC))  # past the open: closed out already, so not missed
        async with db.session() as s:
            (await s.get(Trade, "trd_cns_1")).settle_status = "SETTLED"
            await s.commit()
        await sync.sync_fails()
        assert delivered == [("CNS-F-000001", 30_000)]
        async with db.session() as s:
            f = await s.get(DeliveryFail, "CNS-F-000001")
            assert (f.status, f.open_qty, f.missed) == ("DELIVERED", 0, False)
        assert (await client.get("/fails")).json() == []
        await sync.sync_fails()
        assert len(delivered) == 1
    finally:
        clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))


async def test_a_fail_not_closed_out_by_the_open_puts_the_name_on_pre_borrow_until_it_delivers(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 204(b): missed, no short in the name without the shares borrowed first. Its locates wait for the trader
    and only shares we hold can be set aside for them, until the fail is delivered."""
    from datetime import UTC, datetime

    from loandesk import clock
    from loandesk.models import DeliveryFail

    [name] = (await _names("GC", 16))[15:16]
    _cns(monkeypatch, [_fail(2, "CNS-F-000002", "KESTRELHF", name.id, "2026-11-04", 20_000)])
    clock.pin(datetime(2026, 11, 5, 15, 0, tzinfo=UTC))  # 10:00 New York: the open has passed
    try:
        _gateway(monkeypatch, {"tradeDate": "2026-11-03", "settlementDate": "2026-11-04",
                               "closedAt": "2026-11-03T21:00:00Z", "shortSales": [], "locateUsage": [],
                               "positions": [{"counterparty": "KESTRELHF", "instrumentId": name.id,
                                              "shortQuantity": 20_000, "soldQuantity": 20_000, "coveredQuantity": 0}]})
        await sync.sync_stock_record()
        await sync.sync_fails()
        [f] = (await client.get("/fails")).json()
        assert (f["status"], f["missed"], f["preBorrow"]) == ("OPEN", True, True)
        assert (await _line(client, "2026-11-03", name.id))["preBorrow"] is True

        await _supply(name.id, 5_000_000)  # plenty on the street: doesn't count now
        async with db.session() as s:
            [loc] = await locates.intake(s, counterparty="BASALTHF", channel="API", request_ref="GW-PB-1",
                                         lines=[locates.LineIn(name.isin, 10_000, line_no=1)])
            assert loc.status == "RECEIVED" and loc.route == "HOLD"
            assert any("pre-borrow (Rule 204(b))" in r for r in loc.route_reasons)
            with pytest.raises(locates.DecisionError, match="only 0 of ours"):
                await locates.decide(s, loc, "APPROVED", 10_000, by="trader:sam", reply=False, override_floor=True)
            await market.upsert_availability(s, "INTERNAL", [{"instrumentId": name.id, "quantity": 25_000,
                                                             "lenders": 1, "asOf": "2026-11-05T14:00:00Z"}])
            await locates.decide(s, loc, "APPROVED", 10_000, by="trader:sam", reply=False, override_floor=True)
            assert loc.reg_sho_basis.startswith("PRE_BORROW_RULE_204B + INTERNAL_INVENTORY")
            await s.commit()
        [row] = [x for x in (await client.get("/locates")).json() if x["id"] == loc.id]
        assert row["preBorrow"] is True

        r = await client.post("/fails/CNS-F-000002/deliver", json={}, headers={"x-trader": "sam"})
        assert r.status_code == 200 and r.json()["status"] == "DELIVERED" and r.json()["preBorrow"] is False
        async with db.session() as s:
            assert (await s.get(DeliveryFail, "CNS-F-000002")).missed is True  # the record keeps it
            [again] = await locates.intake(s, counterparty="BASALTHF", channel="API", request_ref="GW-PB-2",
                                           lines=[locates.LineIn(name.isin, 10_000, line_no=1)])
            assert not any("pre-borrow" in r for r in again.route_reasons)
            await s.rollback()
    finally:
        clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))


async def test_a_recall_on_a_borrow_we_havent_received_warns_the_return_cant_settle_first(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, date, datetime

    from loandesk.models import Trade

    [v] = (await _names("GC", 18))[17:]
    at = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    async with db.session() as s:
        s.add(Trade(id="trd_prc_2", venue="VENUE_A", venue_trade_id="NGT-T-PRC2", uti="UTIPRC2", side="BORROW",
                    instrument_id=v.id, cp_code="MERIDIAL", qty=40_000, open_qty=40_000, fee_bps=18.0, price=40.0,
                    ccy="USD", settle_date=date(2026, 10, 28), venue_json={}, executed_at=at,
                    sfp_contract_id="SFP-C-PRC2", booked_at=at, loan_status="OPEN", settle_status="FAILING",
                    lifecycle=[]))
        await s.commit()

    async def recalls(after: int = 0) -> list[dict[str, Any]]:
        return [{"seq": 99, "recallId": "SFP-R-000099", "contractId": "SFP-C-PRC2", "counterparty": "MERIDIAL",
                 "status": "OPEN", "quantity": 40_000, "outstanding": 40_000, "dueDate": "2026-10-28"}][:1 if
                                                                                                      after < 99 else 0]

    monkeypatch.setattr(systems().processor, "recalls", recalls)
    await sync.sync_recalls()
    async with db.session() as s:
        card = (await s.execute(select(Action).where(Action.source_ref == "recall:SFP-R-000099"))).scalar()
    assert any("haven't reached us yet (our delivery is FAILING)" in f for f in card.risk_flags)
