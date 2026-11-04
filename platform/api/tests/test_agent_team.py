"""The agent team: the queue planner reading the whole queue and dispatching sub-agents, the trade reviewer's read
on rule-drafted cards, and lanes that run each kind concurrently, within limits."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from loandesk import agent, autopilot, db, gate, secmaster
from loandesk.agent import queue, trade
from loandesk.agent.runner import Lane
from loandesk.models import Action, Instrument
from loandesk.settings import get_settings

SPARK, WONDER = "muse-spark-1.3", "muse-wonder-1.0"


class Scripted:
    """Answers per model, in order; a model with nothing left raises, the way the gateway refuses."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {m: list(v) for m, v in script.items()}
        self.seen: list[tuple[str, list[dict[str, Any]]]] = []

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        self.seen.append((model, json.loads(json.dumps(messages))))
        if not self.script.get(model):
            raise RuntimeError(f"no answer scripted for {model}")
        x = self.script[model].pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": x if isinstance(x, str) else json.dumps(x)}}]}

    @property
    def mode(self) -> str:
        return "replay"


@pytest.fixture
async def team(setup: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    cfg = get_settings()
    monkeypatch.setattr(cfg, "agent_mode", "replay")
    monkeypatch.setattr(cfg, "agent_checkpoint_url", "memory")
    monkeypatch.setattr(cfg, "agent_plan_every_s", 0)  # plans on demand only, here
    monkeypatch.setattr(autopilot, "_state", {"mode": "off", "by": None, "at": None, "tripped": None})
    r = await agent.start()
    assert r is not None
    yield r
    await agent.stop()


def script(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[Any]]) -> Scripted:
    m = Scripted(answers)
    for mod in (trade, queue):
        monkeypatch.setattr(mod, "gateway", lambda: m)
    return m


async def _clear_queue() -> None:
    """Earlier tests leave cards pending; the planner reads the whole queue, so start these from empty."""
    async with db.session() as s:
        for a in (await s.execute(select(Action).where(Action.status == "PENDING"))).scalars():
            a.status = "SUPERSEDED"
        await s.commit()


async def _name(skip: int = 0) -> Instrument:
    async with db.session() as s:
        insts = (await s.execute(select(Instrument).where(Instrument.ccy == "USD").order_by(Instrument.id.desc()))
                 ).scalars().all()
    return [i for i in insts if secmaster.live(i.id)["tier"] == "GC" and (secmaster.live(i.id)["lastPx"] or 0) > 5
            ][skip]


async def _card(inst: Instrument, *, qty: int = 10_000, fee: float | None = None, by: str = "rules:short-list",
                kind: str = "order", payload: dict[str, Any] | None = None, flags: list[str] | None = None) -> str:
    fee = fee if fee is not None else round(secmaster.live(inst.id)["feeBps"] * 1.1, 1)
    async with db.session() as s:
        a = await gate.propose(s, kind=kind, proposed_by=by, title=f"cover short: borrow {qty:,} {inst.composite}",
                               payload=payload or {"side": "BORROW", "instrument": inst.id, "quantity": qty,
                                                   "feeBps": fee, "override": True},
                               risk_flags=flags or [])
        await s.commit()
        return a.id


async def settle(team: Any) -> None:
    """Let the watch pick up what was just proposed, then wait for every run it started."""
    for _ in range(3):
        await asyncio.sleep(0.05)
        await team.idle()


async def _card_now(i: str) -> Action:
    async with db.session() as s:
        return await s.get(Action, i)


async def test_a_lane_runs_its_size_at_once() -> None:
    lane, peak = Lane(2), 0

    async def job() -> None:
        nonlocal peak
        async with lane.slot():
            peak = max(peak, lane.running)
            await asyncio.sleep(0.01)

    await asyncio.gather(*(job() for _ in range(6)))
    assert peak == 2 and lane.json() == {"size": 2, "running": 0, "waiting": 0}


async def test_the_trade_reviewer_reads_each_rule_drafted_card(team: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    await _clear_queue()
    inst = await _name()
    last = secmaster.live(inst.id)["feeBps"]
    m = script(monkeypatch, {SPARK: [
        {"verdict": "AMEND", "changes": {"feeBps": round(last * 1.2, 1)}, "notes": "the name is tightening",
         "confidence": 0.8, "riskFlags": []},
        {"verdict": "AMEND", "changes": {"feeBps": last * 50}, "notes": "pay up", "confidence": 0.9}]})
    sensible, wild = await _card(inst), await _card(inst, qty=20_000)  # each is read as it's proposed
    await settle(team)
    a, b = await _card_now(sensible), await _card_now(wild)
    reviews = sorted([a.review, b.review], key=lambda r: r["verdict"])
    assert [r["verdict"] for r in reviews] == ["AMEND", "HOLD"]
    assert reviews[0]["changes"] == {"feeBps": round(last * 1.2, 1)} and reviews[0]["model"] == SPARK
    assert "fat-finger band" in reviews[1]["notes"] and reviews[1]["changes"] == {}  # a wild suggestion: a look
    prompt = m.seen[0][1][1]["content"]
    assert inst.composite in prompt and "feeHistory" in prompt
    trader_card = await _card(inst, by="trader:sam")
    await settle(team)
    assert (await _card_now(trader_card)).review is None  # only what the rules drafted


async def test_the_autopilot_waits_for_the_read_and_respects_it(team: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    await _clear_queue()
    inst = await _name(1)
    script(monkeypatch, {})  # no answers yet: the reviews fail
    card = await _card(inst)
    async with db.session() as s:
        a = await s.get(Action, card)
        a.review = None  # as if the reviewer hadn't finished
        await s.commit()
        assert (await autopilot.verdict(s, a, "autopilot"))[0] == autopilot.READING
        a.review = {"verdict": "HOLD", "notes": "fee is stale"}
        assert "would hold it: fee is stale" in (await autopilot.verdict(s, a, "danger"))[0]  # every mode
        a.review = {"verdict": "AGREE", "notes": ""}
        assert (await autopilot.verdict(s, a, "autopilot"))[0] is None
    await settle(team)
    unread = await _card(inst, qty=30_000)
    await settle(team)  # the review fails (no model answer): the card says so, and nothing waits on it
    r = (await _card_now(unread)).review
    assert r["verdict"] == "UNREAD" and "couldn't read it" in r["notes"]


async def test_the_planner_reads_the_queue_dispatches_and_ranks(
        client: httpx.AsyncClient, team: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_review_trades", False)  # its sub-agents are dispatched by the plan
    await _clear_queue()
    x, y = await _name(2), await _name(3)
    big, dup, other = await _card(x, qty=90_000), await _card(x, qty=10_000), await _card(y, qty=5_000)
    flagged = await _card(y, qty=7_000, flags=["warning: thin supply"])
    monkeypatch.setattr(get_settings(), "agent_review_trades", True)
    agree = {"verdict": "AGREE", "notes": "fine", "confidence": 0.9}
    m = script(monkeypatch, {SPARK: [agree] * 4, WONDER: [{
        "order": [{"id": other, "why": "smallest first"}, {"id": "act_not_real", "why": "?"}, {"id": big, "why": "x"}],
        "batches": [{"title": "covers", "ids": [other, flagged, big], "why": "same shape"}],
        "notes": ["two cards in the same name"]}]})
    r = await client.post("/agent/plan")
    assert r.status_code == 202
    await settle(team)
    p = (await client.get("/agent/plan")).json()
    ids = [o["id"] for o in p["order"]]
    assert ids[:2] == [other, big] and set(ids) == {big, dup, other, flagged}  # bogus id out; the rest after
    assert p["by"] == WONDER and p["batches"] == [{"title": "covers", "ids": [other, big], "why": "same shape"}]
    assert any(c["type"] == "duplicate" and set(c["ids"]) == {big, dup} for c in p["conflicts"])
    assert p["dispatched"]["trades"] == 4  # (and a locate review for any warm line left waiting)
    assert [(await _card_now(i)).review["verdict"] for i in ids] == ["AGREE"] * 4  # the sub-agents ran
    run = (await client.get(f"/agent/runs/{r.json()['id']}")).json()
    assert [t["step"] for t in run["transcript"]] == ["gather", "dispatch", "collect", "plan", "check", "saved"]
    assert '"review": "AGREE"' in next(msgs for model, msgs in m.seen if model == WONDER)[1]["content"]  # waited
    assert [model for model, _ in m.seen].count(SPARK) == 4
    digest = next(msgs for model, msgs in m.seen if model == WONDER)[1]["content"]
    assert '"usd":' in digest and "Conflicts the code found" in digest  # the code did the sums, not the model

    assert (await client.get("/agent/status")).json()["lanes"]["review_trade"]["size"] == 4


async def test_with_no_model_the_plan_is_the_rules_order(
        client: httpx.AsyncClient, team: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_review_trades", False)
    await _clear_queue()
    x = await _name(4)
    small, large = await _card(x, qty=1_000), await _card(x, qty=80_000, flags=["warning: Rule 204 close-out due"])
    script(monkeypatch, {})
    await client.post("/agent/plan")
    await settle(team)
    p = (await client.get("/agent/plan")).json()
    assert p["by"] == "rules" and [o["id"] for o in p["order"]] == [large, small]  # the close-out first
    assert p["order"][0]["why"] == "Rule 204 close-out by the open"


async def test_a_batch_never_holds_a_card_the_reviewer_would_change() -> None:
    def card(i: str, review: str | None = None, flags: int = 0) -> dict[str, Any]:
        return {"id": i, "kind": "order", "flags": flags, "review": review, "deadline": None, "due": 9, "usd": 1,
                "ageMin": 1}

    cards = [card("a", "AGREE"), card("b", "AMEND"), card("c", "HOLD"), card("d"), card("e", flags=1)]
    out = await queue.check({"digest": cards, "conflicts": [], "overflow": 0, "model": WONDER, "answer": {
        "order": [{"id": "a"}], "batches": [{"title": "all", "ids": ["a", "b", "c", "d", "e"], "why": ""}]}})
    assert out["plan"]["batches"][0]["ids"] == ["a", "d"]  # approved as drafted: only what nobody would change
