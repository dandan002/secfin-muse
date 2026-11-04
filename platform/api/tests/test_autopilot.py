"""Autopilot and danger mode: which cards approve themselves, the limits, the breaker, and what never loosens."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from loandesk import autopilot, db, gate, secmaster
from loandesk.models import Action, Event, Locate, Order
from loandesk.services import market
from loandesk.settings import get_settings


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(autopilot, "_state", {"mode": "off", "by": None, "at": None, "tripped": None})
    monkeypatch.setattr(autopilot, "_failures", 0)
    monkeypatch.setattr(autopilot, "worker", None)
    gate.set_kill_switch(False, "test")
    yield
    gate.set_kill_switch(False, "test")


def _on(m: str) -> None:
    autopilot._state["mode"] = m


async def _card(
    kind: str = "order", payload: dict[str, Any] | None = None, flags: list[str] | None = None, by: str = "rules:test"
) -> str:
    async with db.session() as s:
        a = await gate.propose(
            s,
            kind=kind,
            title=f"{kind} card",
            proposed_by=by,
            risk_flags=flags or [],
            payload=payload or {"instrument": "AAPL US", "quantity": 1000, "feeBps": 35, "override": True},
        )
        await s.commit()
        return a.id


async def _why(action_id: str, m: str) -> str | None:
    async with db.session() as s:
        return (await autopilot.verdict(s, await s.get(Action, action_id), m))[0]


def _hot_name() -> str:
    for inst in secmaster.universe():
        if secmaster.live(inst.id)["tier"] == "HOT":
            return inst.composite
    pytest.skip("no Hot name in today's universe")


async def test_what_autopilot_takes_and_what_waits(setup: None) -> None:
    assert await _why(await _card(), "autopilot") is None  # a plain order inside the limits
    assert "risk flag" in await _why(await _card(flags=["warning: thin supply"]), "autopilot")
    assert "BLOCKED" in await _why(await _card(flags=["BLOCKED: fat finger (fee)"]), "autopilot")
    assert "wait for a trader" in await _why(await _card("amend", {"tradeId": "t", "fields": {}}), "autopilot")
    big = await _card(payload={"instrument": "AAPL US", "quantity": 10_000_000, "feeBps": 35})
    assert "card limit" in await _why(big, "autopilot")
    low = await _card(payload={"instrument": "AAPL US", "quantity": 1000, "feeBps": 35, "confidence": 0.6})
    assert "confidence 0.60" in await _why(low, "autopilot")
    hot = await _card(payload={"instrument": _hot_name(), "quantity": 100, "feeBps": 500})
    assert await _why(hot, "autopilot") == "a Hot name"
    assert "trader's own" in await _why(await _card(by="trader:sam"), "autopilot")


async def test_danger_goes_further_but_never_past_a_hard_check(setup: None) -> None:
    assert await _why(await _card(flags=["warning: thin supply"]), "danger") is None
    assert await _why(await _card("amend", {"tradeId": "t", "fields": {}}), "danger") is None
    assert (
        await _why(await _card(payload={"instrument": _hot_name(), "quantity": 100, "feeBps": 500}), "danger") is None
    )
    assert "BLOCKED" in await _why(await _card(flags=["BLOCKED: restricted list"]), "danger")
    huge = await _card(payload={"instrument": "AAPL US", "quantity": 10_000_000, "feeBps": 35})
    assert "card limit" in await _why(huge, "danger")  # wider limits, not none


async def _locate(qty: int = 1000) -> str:
    async with db.session() as s:
        await market.upsert_availability(
            s,
            "VENUE_A",
            [
                {
                    "instrumentId": "I00039",
                    "quantity": 1_000_000,
                    "lenders": 3,
                    "bestFeeBps": 20.0,
                    "asOf": "2026-10-27T13:59:00Z",
                }
            ],
        )
        await s.commit()
    from loandesk.services import locates

    async with db.session() as s:
        [loc] = await locates.intake(
            s, counterparty="PBBRAVO", channel="API", request_ref="ap-test", lines=[locates.LineIn("DELL US", qty)]
        )
        await s.commit()
        return loc.id


async def test_autopilot_approves_an_agent_locate_card_end_to_end(setup: None) -> None:
    loc = await _locate()
    card = await _card(
        "locate_decision",
        {"locateId": loc, "decision": "APPROVED", "quantity": 1000, "feeBps": None, "confidence": 0.93},
        by="agent:muse-spark-1.3",
    )
    _on("autopilot")
    a = await autopilot.consider(card)
    assert a is not None and (a.status, a.approver) == ("EXECUTED", "policy:AUTOPILOT")
    async with db.session() as s:
        assert (await s.get(Locate, loc)).status == "APPROVED"
        ev = (await s.execute(select(Event).where(Event.entity == "autopilot", Event.entity_id == card))).scalar_one()
    assert ev.type == "approved" and ev.after["notionalUsd"] > 0 and ev.actor == "policy:AUTOPILOT"


async def test_off_means_off_and_a_passed_over_card_says_why(setup: None) -> None:
    card = await _card(flags=["warning: thin supply"])
    assert await autopilot.consider(card) is None  # off: nothing happens, nothing recorded
    _on("autopilot")
    assert await autopilot.consider(card) is None
    async with db.session() as s:
        a = await s.get(Action, card)
        ev = (await s.execute(select(Event).where(Event.entity == "autopilot", Event.entity_id == card))).scalar_one()
    assert a.status == "PENDING" and ev.type == "passed_over" and "risk flag" in ev.after["reason"]


async def test_the_days_count_limit(setup: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async with db.session() as s:
        n, _ = await autopilot.used_today(s)
    monkeypatch.setattr(get_settings(), "autopilot_day_count", n + 1)
    _on("autopilot")
    first, second = await _card(), await _card()
    assert (await autopilot.consider(first)) is not None
    assert await autopilot.consider(second) is None
    assert "auto-approvals are used" in await _why(second, "autopilot")


async def test_the_kill_switch_still_stops_it(setup: None) -> None:
    _on("danger")
    card = await _card()
    gate.set_kill_switch(True, "test")
    assert await autopilot.consider(card) is None
    async with db.session() as s:
        assert (await s.get(Action, card)).status == "PENDING"
    assert autopilot._failures == 0  # waiting on the switch isn't a failure


async def test_failures_in_a_row_trip_the_breaker(setup: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "autopilot_trip_after", 2)
    _on("autopilot")
    for _ in range(2):  # a card for a locate that isn't there: the executor fails it
        card = await _card("locate_decision", {"locateId": "loc_missing", "decision": "APPROVED", "quantity": 1})
        a = await autopilot.consider(card)
        assert a is not None and a.status == "FAILED"
    st = autopilot.state()
    assert st["mode"] == "off" and st["by"] == "policy:BREAKER" and "2 auto-approved cards failed" in st["tripped"]


async def test_switching_modes(client: httpx.AsyncClient) -> None:
    r = await client.put("/autopilot", json={"mode": "danger"})
    assert r.status_code == 422 and "DANGER" in r.json()["detail"]
    r = (
        await client.put("/autopilot", json={"mode": "danger", "confirm": "DANGER"}, headers={"x-trader": "sam"})
    ).json()
    assert r["mode"] == "danger" and r["by"] == "trader:sam" and r["limits"]["kinds"] == "all"
    got = (await client.get("/autopilot")).json()
    assert got["mode"] == "danger" and "count" in got["today"]
    assert (await client.put("/autopilot", json={"mode": "autopilot"})).json()["limits"]["hot"] is False
    assert (await client.get("/status")).json()["autopilot"]["mode"] == "autopilot"
    await client.put("/autopilot", json={"mode": "off"})


def test_danger_takes_a_counter_only_within_tolerance() -> None:
    def o(side: str, fee: float, counter: float | None, status: str = "COUNTERED") -> Order:
        return Order(side=side, fee_bps=fee, counter_fee_bps=counter, status=status)

    assert autopilot.counter_ok(o("BORROW", 30, 33))  # 10% over our bid: at the edge
    assert not autopilot.counter_ok(o("BORROW", 30, 34))
    assert autopilot.counter_ok(o("LEND", 30, 27)) and not autopilot.counter_ok(o("LEND", 30, 26))
    assert not autopilot.counter_ok(o("BORROW", 30, 31, status="PENDING"))


async def test_the_worker_hears_new_cards_and_locates(setup: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the desk's own stream: a committed card is approved, a Warm locate queues for the agent, and in
    danger a Hot-name (HOLD) line and a lender's counter are taken up too."""
    import asyncio

    autopilot.start()
    try:
        _on("autopilot")
        card = await _card()
        for _ in range(50):
            await asyncio.sleep(0.05)
            async with db.session() as s:
                if (await s.get(Action, card)).status != "PENDING":
                    break
        async with db.session() as s:
            assert (await s.get(Action, card)).approver == "policy:AUTOPILOT"
        w = autopilot.worker
        await w._handle(
            {
                "topic": "locate",
                "data": {"type": "received", "id": "loc_w", "data": {"status": "RECEIVED", "route": "REVIEW"}},
            }
        )
        await w._handle(
            {
                "topic": "locate",
                "data": {"type": "received", "id": "loc_h", "data": {"status": "RECEIVED", "route": "HOLD"}},
            }
        )
        assert list(w._reviews) == ["loc_w"]  # HOLD lines stay with the trader in autopilot
        taken: list[str] = []

        async def take(order_id: str) -> None:
            taken.append(order_id)

        monkeypatch.setattr(autopilot, "take_counter", take)
        await w._handle(
            {"topic": "order", "data": {"type": "countered", "id": "ord_c", "data": {"status": "COUNTERED"}}}
        )
        assert not taken
        _on("danger")
        await w._handle(
            {
                "topic": "locate",
                "data": {"type": "received", "id": "loc_h2", "data": {"status": "RECEIVED", "route": "HOLD"}},
            }
        )
        await w._handle(
            {"topic": "order", "data": {"type": "countered", "id": "ord_c", "data": {"status": "COUNTERED"}}}
        )
        assert "loc_h2" in w._reviews and taken == ["ord_c"]
    finally:
        await autopilot.stop()
