"""The 2026-11 audit: each hole found, held shut. Races at the gate, what survives a restart, what a counterparty's
"done" can commit us to, and what the desk's own workers may never miss."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from sqlalchemy import select

from loandesk import autopilot, clock, db, events, gate, secmaster
from loandesk.adapters import systems
from loandesk.agent import models as gw
from loandesk.ingest import chat
from loandesk.models import Action, Instrument
from loandesk.services import market
from loandesk.settings import get_settings

# A prime broker, not an agent lender: other tests bid in "the latest agent lender's room".
PB = {"handle": "jkowalski.pba", "displayName": "Jan Kowalski", "kind": "persona", "cpCode": "PBALPHA"}
US = {"handle": "srivera", "displayName": "Sam Rivera", "kind": "human"}


async def test_two_deciders_at_once_send_it_once(setup: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A trader's click and the autopilot's sweep on the same card: one sends, the other is refused."""
    sent: list[str] = []

    async def send(room: str, text: str, client_msg_id: str | None = None) -> dict:
        sent.append(text)
        await asyncio.sleep(0.05)  # a venue round trip, while the other decider tries
        return {"id": "m1"}

    monkeypatch.setattr(systems().chat, "send", send)
    async with db.session() as s:
        a = await gate.propose(s, kind="chat_send", title="t", payload={"roomId": "r", "text": "Bid 100k X"},
                               proposed_by="rules:test")
        await s.commit()

    async def decide(who: str, verb: str) -> str:
        async with db.session() as s:
            try:
                x = await (gate.approve(s, await s.get(Action, a.id), who) if verb == "approve"
                           else gate.reject(s, await s.get(Action, a.id), who))
                await s.commit()
                return x.status
            except gate.GateError:
                return "refused"

    out = await asyncio.gather(decide("trader:sam", "approve"), decide("policy:AUTOPILOT", "approve"),
                               decide("trader:kli", "reject"))
    assert sent == ["Bid 100k X"] and sorted(out).count("refused") == 2


async def test_the_kill_switch_survives_a_restart(setup: None) -> None:
    async with db.session() as s:
        await gate.save_kill_switch(s, True, "trader:sam")
        await s.commit()
    gate.set_kill_switch(False, "process restart")  # what a fresh process starts with
    async with db.session() as s:
        await gate.load_kill_switch(s)
        ev = (await s.execute(select(events.Event).where(events.Event.entity == "gate")
                              .order_by(events.Event.id.desc()))).scalars().first()
        assert gate.kill_switch()["on"] and ev.type == "on" and ev.actor == "trader:sam"
        await gate.save_kill_switch(s, False, "trader:sam")
        await s.commit()


async def test_the_model_budget_survives_a_restart(setup: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "meter", gw.Meter())
    gw.meter.add("muse-spark-1.3", {"prompt_tokens": 1_000_000})  # $1.25 of today's budget
    await gw.save_meter()
    monkeypatch.setattr(gw, "meter", gw.Meter())  # a fresh process
    await gw.load_meter()
    assert round(gw.meter.total, 2) == 1.25


async def test_a_counterpartys_done_cant_set_the_terms(setup: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """We bid 100k; their "done 900k at 2.5x the fee" drafts a card, but one that waits for a trader in every mode."""
    async with db.session() as s:
        insts = (await s.execute(select(Instrument).where(Instrument.country == "US").order_by(Instrument.id)
                                 )).scalars().all()
    inst = [i for i in insts if secmaster.live(i.id)["tier"] == "GC" and not i.restricted][7]
    fee = round(secmaster.live(inst.id)["feeBps"])
    answers = iter([
        {"intent": "RFQ", "confidence": 0.95, "ourSide": "BORROW",
         "lines": [{"identifier": inst.composite, "quantity": 100_000, "feeBps": fee}]},
        {"intent": "ACCEPT", "confidence": 0.95, "ourSide": "BORROW",
         "lines": [{"identifier": inst.composite, "quantity": 900_000, "feeBps": round(fee * 2.5)}]}])

    class Parser:
        async def chat(self, **kw: Any) -> dict:
            return {"choices": [{"message": {"role": "assistant", "content": json.dumps(next(answers))}}]}

    monkeypatch.setattr(get_settings(), "chat_parser", "model")
    monkeypatch.setattr(gw, "gateway", lambda: Parser())
    for i, (who, text) in enumerate([(US, f"Bid 100k {inst.composite} @ {fee}bps open?"),
                                     (PB, f"done 900k {inst.composite} @ {round(fee * 2.5)}bps")], start=1):
        async with db.session() as s:
            m = await chat.ingest(s, {"messageId": 88_000 + i, "streamId": "pbalpha-audit", "seq": 88_000 + i,
                                      "sender": who, "text": text, "ts": "2026-10-27T13:59:30.000Z"})
            await s.commit()
    async with db.session() as s:
        [card] = (await s.execute(select(Action).where(Action.source_ref == m.id))).scalars().all()
        assert "HOLD: they agreed 900,000; we said 100,000" in card.risk_flags
        assert (await autopilot.verdict(s, card, "danger"))[0] is not None  # not even danger takes it


def test_a_workers_subscription_loses_nothing() -> None:
    """A burst bigger than a browser's queue: the autopilot and the agent team still hear every card."""
    worker, browser = events.hub.subscribe({"action"}), events.hub.subscribe()
    try:
        for i in range(2_500):
            events.hub.publish("market", {"tick": i})
            events.hub.publish("action", {"type": "proposed", "id": f"act_{i}"})
        assert worker.qsize() == 2_500 and browser.qsize() == 1_000
    finally:
        events.hub.unsubscribe(worker)
        events.hub.unsubscribe(browser)


async def test_the_autopilot_wont_approve_a_locate_whose_supply_has_gone(setup: None) -> None:
    from loandesk.services import locates

    async with db.session() as s:
        inst = next(i for i in (await s.execute(select(Instrument).order_by(Instrument.id.desc()))).scalars()
                    if secmaster.live(i.id)["tier"] == "WARM" and not i.restricted and not i.threshold_flag)
        await market.upsert_availability(s, "VENUE_A", [{"instrumentId": inst.id, "quantity": 50_000, "lenders": 2,
                                                         "bestFeeBps": 30.0, "asOf": clock.iso(clock.now())}])
        [loc] = await locates.intake(s, counterparty="PBBRAVO", channel="API", request_ref="audit-stale",
                                     lines=[locates.LineIn(inst.isin, 40_000)])
        a = await gate.propose(s, kind="locate_decision", title="t", proposed_by="agent:muse-spark-1.3",
                               payload={"locateId": loc.id, "decision": "APPROVED", "quantity": 40_000,
                                        "feeBps": None, "confidence": 0.95})
        await market.upsert_availability(s, "VENUE_A", [{"instrumentId": inst.id, "quantity": 5_000, "lenders": 1,
                                                         "bestFeeBps": 30.0, "asOf": clock.iso(clock.now())}])
        await s.commit()
        why, _ = await autopilot.verdict(s, a, "danger")
        assert why is not None and "available now" in why  # passed over, not failed (the breaker stays quiet)
        a = await gate.approve(s, a, "policy:DANGER")  # and the executor holds the line even if asked
        assert a.status == "FAILED" and "available now" in a.result["error"]
        await s.rollback()
