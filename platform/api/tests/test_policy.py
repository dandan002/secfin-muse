"""Desk policy set at run time: the autopilot's limits, and counterparty priority for returns and recalls."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from loandesk import autopilot, db, policy, secmaster
from loandesk.models import Action, Event, Instrument, Trade
from loandesk.services import shortlist


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
    assert "order" in got["kinds"] and got["priority"]["value"]["returns"] == []
    lim = {**got["autopilot"]["value"], "maxCardUsd": 1_000_000, "kinds": ["order", "lifecycle"]}
    r = await client.put("/policy/autopilot", json=lim, headers={"x-trader": "sam"})
    assert r.status_code == 200 and r.json()["by"] == "trader:sam" and r.json()["value"]["maxCardUsd"] == 1_000_000
    assert autopilot.envelope("autopilot").max_card_usd == 1_000_000
    assert autopilot.envelope("autopilot").kinds == frozenset({"order", "lifecycle"})

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
    dup = await client.put("/policy/priority", json={"recalls": ["HARBORAL", "HARBORAL"]})
    assert dup.status_code == 422 and "twice" in dup.json()["detail"]
    ok = await client.put("/policy/priority", json={"returns": ["NORTHAL", "HARBORAL"]})
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
