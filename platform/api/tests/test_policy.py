"""Desk policy set at run time: the autopilot's limits, and counterparty priority for returns, recalls and margin
calls (with the day's collateral budget)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from loandesk import autopilot, db, policy, secmaster
from loandesk.adapters import systems
from loandesk.models import Action, Event, Instrument, Trade
from loandesk.services import margin, shortlist


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    policy.reset_cache()
    monkeypatch.setattr(autopilot, "_state", {"mode": "off", "by": None, "at": None, "tripped": None})
    yield
    policy.reset_cache()


async def _name(tier: str, skip: int = 0) -> Instrument:
    async with db.session() as s:
        insts = (await s.execute(select(Instrument).where(Instrument.ccy == "USD").order_by(Instrument.id.desc()))
                 ).scalars().all()
    return [i for i in insts if secmaster.live(i.id)["tier"] == tier and (secmaster.live(i.id)["lastPx"] or 0) > 5
            ][skip]


def _trade(tid: str, inst: Instrument, side: str, cp: str, qty: int, fee: float) -> Trade:
    at = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    return Trade(id=tid, venue="VENUE_A", venue_trade_id=f"NGT-{tid}", uti=f"UTI{tid}", side=side,
                 instrument_id=inst.id, cp_code=cp, qty=qty, open_qty=qty, fee_bps=fee, price=50.0, ccy="USD",
                 settle_date=date(2026, 10, 28), venue_json={}, executed_at=at, sfp_contract_id=f"SFP-{tid}",
                 booked_at=at, loan_status="OPEN", settle_status="SETTLED", lifecycle=[])


async def test_the_desk_sets_limits_and_they_stick(client: httpx.AsyncClient) -> None:
    got = (await client.get("/policy")).json()
    assert got["autopilot"]["set"] is False and got["autopilot"]["value"]["maxCardUsd"] == 5_000_000
    assert "margin" in got["kinds"] and got["priority"]["value"]["returns"] == []
    lim = {**got["autopilot"]["value"], "maxCardUsd": 1_000_000, "kinds": ["order", "margin"]}
    r = await client.put("/policy/autopilot", json=lim, headers={"x-trader": "sam"})
    assert r.status_code == 200 and r.json()["by"] == "trader:sam" and r.json()["value"]["maxCardUsd"] == 1_000_000
    assert autopilot.envelope("autopilot").max_card_usd == 1_000_000
    assert autopilot.envelope("autopilot").kinds == frozenset({"order", "margin"})

    policy.reset_cache()  # a restart: what the desk set comes back from the database
    async with db.session() as s:
        await policy.load(s)
        ev = (await s.execute(select(Event).where(Event.entity == "policy", Event.entity_id == "autopilot"))
              ).scalars().all()[-1]
    assert policy.limits("autopilot").maxCardUsd == 1_000_000
    assert ev.actor == "trader:sam" and ev.before["value"]["maxCardUsd"] == 5_000_000

    assert (await client.put("/policy/autopilot", json={**lim, "kinds": ["teleport"]})).status_code == 422
    assert (await client.put("/policy/danger", json={**lim, "dayUsd": -1})).status_code == 422
    assert (await client.put("/policy/breaker", json={"tripAfter": 0})).status_code == 422
    assert (await client.put("/policy/breaker", json={"tripAfter": 5})).json()["value"] == {"tripAfter": 5}
    assert policy.trip_after() == 5
    assert (await client.delete("/policy/autopilot")).json()["set"] is False
    assert policy.limits("autopilot").maxCardUsd == 5_000_000


async def test_counterparty_priority_is_checked(client: httpx.AsyncClient) -> None:
    bad = await client.put("/policy/priority", json={"returns": ["NOSUCHCP"]})
    assert bad.status_code == 422 and "NOSUCHCP" in bad.json()["detail"]
    dup = await client.put("/policy/priority", json={"margin": ["HARBORAL", "HARBORAL"]})
    assert dup.status_code == 422 and "twice" in dup.json()["detail"]
    ok = await client.put("/policy/priority", json={"returns": ["NORTHAL", "HARBORAL"], "marginBudgetUsd": 1e6})
    assert ok.json()["value"]["returns"] == ["NORTHAL", "HARBORAL"]
    assert policy.rank("returns", "NORTHAL") == 0 and policy.rank("returns", "MERIDIAL") == 2


async def test_returns_go_to_the_desks_lenders_first(setup: None) -> None:
    inst = await _name("GC")
    async with db.session() as s:
        s.add_all([_trade("trd_pr_dear", inst, "BORROW", "HARBORAL", 20_000, 60.0),
                   _trade("trd_pr_north", inst, "BORROW", "NORTHAL", 20_000, 10.0)])
        await s.commit()
    ln = {"instrumentId": inst.id, "composite": inst.composite, "toReturn": 20_000, "returning": [],
          "clientShort": 0, "box": 40_000, "unbooked": 0, "returnedSinceBox": 0, "fails": 0, "recalled": 0,
          "excess": 20_000, "working": 0, "lastPx": 50.0, "ccy": "USD"}
    sl = {"tradeDate": "2026-10-27", "settlementDate": "2026-10-28", "lines": [ln]}

    async def returned() -> list[str]:
        async with db.session() as s:
            ids = await shortlist._draft_returns(s, sl, by="rules:test", notes=[])
            out = [(await s.get(Action, i)).payload["tradeId"] for i in ids]
            await s.rollback()
        return out

    assert await returned() == ["trd_pr_dear"]  # no order set: the dearest goes back
    policy._set["priority"] = policy.Priority(returns=["NORTHAL"]).model_dump()
    assert await returned() == ["trd_pr_north"]  # the desk's lender first, cheap as it is


async def test_recalls_come_from_the_desks_borrowers_first(setup: None) -> None:
    inst = await _name("WARM")
    async with db.session() as s:
        s.add_all([_trade("trd_rc_cheap", inst, "LEND", "KESTRELHF", 30_000, 5.0),
                   _trade("trd_rc_cedar", inst, "LEND", "CEDARHF", 30_000, 12.0),
                   _trade("trd_rc_dear", inst, "LEND", "BASALTHF", 30_000, 90.0)])
        await s.commit()
    ln = {"instrumentId": inst.id, "composite": inst.composite, "tier": "WARM", "lastPx": 50.0, "ccy": "USD",
          "clientShort": 20_000, "uncovered": 20_000, "closeOutBy": None}

    async def recalled() -> list[str]:
        async with db.session() as s:
            _, ids = await shortlist._recall_loans(s, ln, 20_000, 40.0, by="rules:test", notes=[])
            out = [(await s.get(Action, i)).payload["tradeId"] for i in ids]
            await s.rollback()
        return out

    assert await recalled() == ["trd_rc_cheap"]
    policy._set["priority"] = policy.Priority(recalls=["BASALTHF", "CEDARHF"]).model_dump()
    assert await recalled() == ["trd_rc_cedar"]  # Basalt's loan earns more than a borrow costs: kept out


async def test_margin_calls_are_drafted_in_the_desks_order_within_the_budget(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = await _name("GC", 1)
    async with db.session() as s:
        s.add_all([_trade("trd_mc_harbor", inst, "BORROW", "HARBORAL", 10_000, 20.0),
                   _trade("trd_mc_north", inst, "BORROW", "NORTHAL", 10_000, 20.0),
                   _trade("trd_mc_kestrel", inst, "LEND", "KESTRELHF", 10_000, 20.0)])
        await s.commit()
    policy._set["priority"] = policy.Priority(margin=["NORTHAL"], marginBudgetUsd=60_000).model_dump()
    calls = [  # as /marks/run gives them: Harbor's call is the bigger, but Northgate is first in the desk's order
        {"contractId": "SFP-trd_mc_harbor", "markDate": "2026-10-27", "callAmount": 50_000.0, "ccy": "USD",
         "loanValue": 1e6, "requiredCollateral": 1.02e6, "postedCollateral": 0.97e6,
         "marginCall": {"direction": "WE_ARE_CALLED"}},
        {"contractId": "SFP-trd_mc_north", "markDate": "2026-10-27", "callAmount": 40_000.0, "ccy": "USD",
         "loanValue": 1e6, "requiredCollateral": 1.02e6, "postedCollateral": 0.98e6,
         "marginCall": {"direction": "WE_ARE_CALLED"}},
        {"contractId": "SFP-trd_mc_kestrel", "markDate": "2026-10-27", "callAmount": 30_000.0, "ccy": "USD",
         "marginCall": {"direction": "WE_CALL"}},
        {"contractId": "SFP-not-ours", "markDate": "2026-10-27", "callAmount": 9.0, "ccy": "USD",
         "marginCall": {"direction": "WE_CALL"}}]
    async with db.session() as s:
        ids = await margin.draft(s, calls, by="rules:margin")
        await s.commit()
        cards = [await s.get(Action, i) for i in ids]
        assert await margin.draft(s, calls, by="rules:margin") == []  # one card per call
    assert [a.payload["tradeId"] for a in cards] == ["trd_mc_north", "trd_mc_harbor", "trd_mc_kestrel"]
    north, harbor, kestrel = cards
    assert north.payload["direction"] == "PAY" and not north.risk_flags and "#1 in the desk's margin order" in \
        north.rationale
    assert harbor.risk_flags == [margin.OVER] and "waits for a trader" in harbor.rationale  # 40k + 50k > 60k
    assert kestrel.payload["direction"] == "RECEIVE" and not kestrel.risk_flags  # owed to us: no budget

    async with db.session() as s:
        (await s.get(Action, kestrel.id)).status = "FAILED"
        again = await margin.draft(s, calls, by="rules:margin")  # a failed one comes back on the next mark
        assert [(await s.get(Action, i)).payload["tradeId"] for i in again] == ["trd_mc_kestrel"]
        await s.rollback()

    paid: list[dict[str, Any]] = []

    async def pay(p: dict[str, Any]) -> dict[str, Any]:
        paid.append(p)
        return {"paymentId": f"CUS-P-{len(paid)}", "status": "ACCEPTED"}

    monkeypatch.setattr(systems().custodian, "pay", pay)
    r = (await client.post(f"/actions/{north.id}/approve", headers={"x-trader": "sam"})).json()
    assert r["status"] == "EXECUTED" and r["result"]["paymentId"] == "CUS-P-1"
    assert paid[0]["direction"] == "PAY" and paid[0]["amount"] == 40_000 and paid[0]["counterpartyBic"]
    async with db.session() as s:
        t = await s.get(Trade, "trd_mc_north")
        assert t.lifecycle[-1]["event"] == "MARGIN_PAID"
        over = await s.get(Action, harbor.id)
        assert (await autopilot.verdict(s, over, "danger"))[0] == "over today's collateral budget"  # every mode
        within = await s.get(Action, kestrel.id)
        assert "margin cards wait" in (await autopilot.verdict(s, within, "autopilot"))[0]  # not a default kind
        assert await autopilot.verdict(s, within, "danger") == (None, 30_000)
