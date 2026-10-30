"""Persona generation, the eval corpus, and the LLM hook's replay/budget behaviour (no network)."""

from __future__ import annotations

import asyncio
import json
import random

from mock_ib import corpus
from mock_ib import personas as P
from mock_ib.llm import LLM, LLMSettings
from mockcore import rng
from refdata.universe import resolve


def test_labels_match_text_for_every_generator():
    rng.set_seed(20261027)
    r = random.Random(1)
    for p in P.PERSONAS.values():
        drafts = P.locate_blast(r, p, 12) + [P.ioi(r, p), P.rfq(r, p)[0], P.injection(r, p), P.borrow_list(r, p),
                                             P.recall(r, p), P.return_shares(r, p), P.any_of_these(r, p)[0],
                                             P.offer(r, p)]
        for d in drafts:
            for ln in d.label["lines"]:
                assert ln["identifier"] in d.text
                inst = resolve(ln["identifier"].removesuffix(" Equity")) or resolve(f"{ln['identifier']} US")
                assert inst is not None and inst.id == ln["instrumentId"], ln
            for chip in d.chips:
                assert d.text[chip["start"]: chip["end"]] in (chip["composite"], f"{chip['composite']} Equity")


def test_exact_tier_counts():
    rng.set_seed(20261027)
    from refdata.universe import instrument_by_id

    drafts = P.locate_blast(random.Random(3), P.PERSONAS["pb-alpha"], 40, tier_counts={"GC": 34, "WARM": 6})
    tiers = [instrument_by_id(ln["instrumentId"]).tier for d in drafts for ln in d.label["lines"]]
    assert sorted(tiers).count("GC") == 34 and tiers.count("WARM") == 6


def test_pb_trader_bids_and_negotiates():
    rng.set_seed(20261027)
    p = P.PERSONAS["pb-alpha"]
    d, hidden = P.rfq(random.Random(5), p)
    assert d.label["intent"] == "RFQ" and d.label["side"] == "BORROW"
    st = {**hidden, "counters": 0}
    assert P.react(random.Random(0), p, f"can show {hidden['maxFeeBps'] - 5}bps", st)[1].label["intent"] == \
        "ACCEPT"


def test_any_of_these_then_send():
    rng.set_seed(20261027)
    p = P.PERSONAS["pb-alpha"]
    insts = [resolve("META US"), resolve("CRWV US"), resolve("SPCX US")]
    d, hidden = P.any_of_these(random.Random(1), p, insts, [24000, 300000, 24000])
    assert d.text.splitlines()[1:] == ["META 24000", "CRWV 300000", "SPCX 24000"]
    for ln in hidden["lines"]:
        ln["maxFeeBps"] = 40
    out = P.answer_quote(random.Random(0), p, "Maybe these\nMETA 0.15%\nSPCX 0.3%", hidden)[0]
    assert out.label["intent"] == "ACCEPT" and out.text.lower().startswith("send")
    assert [(ln["identifier"], ln["feeBps"]) for ln in out.label["lines"]] == [("META", 15.0), ("SPCX", 30.0)]
    part = P.answer_quote(random.Random(0), p, "META 0.15%\nSPCX 0.6%", hidden)[0]
    assert part.text.startswith("Send META") and [ln["identifier"] for ln in part.label["lines"]] == ["META"]
    assert P.answer_quote(random.Random(0), p, "META 1%\nSPCX 0.9%", hidden)[0].label["intent"] == "DECLINE"
    offer = P.offer(random.Random(2), P.PERSONAS["harbor-lending"], resolve("ASML NA"), 1500, gc=True)
    assert offer.label["intent"] == "OFFER" and offer.label["lines"][0]["rate"] == "GC" and "GC" in offer.text


def test_react_to_quotes():
    p = P.PERSONAS["kestrel-hf"]
    st = {"instrumentId": "I00005", "composite": "OESV US", "qty": 500000, "maxFeeBps": 700, "counters": 0}
    r = random.Random(0)
    assert P.react(r, p, "can show 650bps", st)[1].label["intent"] == "ACCEPT"
    delay, d, new = P.react(r, p, "best is 800", st) or (None, None, None)
    assert d is None or d.label["intent"] in ("COUNTER", "CHATTER")
    _, d, new = P.react(r, p, "@ 780", st)
    assert d.label["intent"] == "COUNTER" and new["counters"] == 1
    assert P.react(r, p, "sorry none available", st)[2] is None
    assert P.parse_fee("indic 6.5%") == 650 and P.parse_fee("@ 700") == 700


def test_an_agent_lender_answers_our_borrow_bid():
    """Our cover bid in a lender's room: "done" (all or part) at our fee when it meets the lender's ask, else its
    ask. Everything else to a lender still gets the old "I'll check with the book"."""
    from datetime import UTC, datetime

    from refdata import market

    harbor, at = P.PERSONAS["harbor-lending"], datetime(2026, 10, 27, 14, tzinfo=UTC)
    nvda = resolve("NVDA US")
    fee = market.fee_bps(nvda, at)
    assert P.parse_bid("Bid 120k NVDA US @ 35bps open?") == (120_000, "NVDA US", 35.0)
    assert P.parse_bid("Bid 24,200 AAP US @ 2627.9bps open?") == (24_200, "AAP US", 2627.9)
    seen = set()
    for seed in range(30):
        delay, draft, state = P.react(random.Random(seed), harbor, f"Bid 120k NVDA US @ {fee * 1.25:.1f}bps open?",
                                      None, now=at)
        assert draft.label["intent"] == "ACCEPT" and draft.label["side"] == "LEND" and state is None
        assert draft.text.startswith("done") and "ref" in draft.text  # the desk's rules read "done ... ref"
        [line] = draft.label["lines"]
        assert line["instrumentId"] == nvda.id and line["quantity"] in (120_000, 60_000)
        seen.add(line["quantity"])
    assert seen == {120_000, 60_000}  # sometimes only part
    _, low, _ = P.react(random.Random(1), harbor, f"Bid 120k NVDA US @ {fee * 0.5:.1f}bps open?", None, now=at)
    assert low.label["intent"] == "COUNTER" and low.label["lines"][0]["feeBps"] > fee * 0.5
    assert low.text.startswith("best I can do")
    _, other, _ = P.react(random.Random(1), harbor, "we'd like to borrow some", None, now=at)
    assert other.label["intent"] == "CHATTER"


def test_corpus_is_deterministic_and_labelled():
    a = corpus.generate(300, 20261027)
    assert a == corpus.generate(300, 20261027)
    assert len(a) == 300
    labelled = [m for c in a for m in c["messages"] if "label" in m]
    intents = {m["label"]["intent"] for m in labelled}
    assert {"IOI", "RFQ", "OFFER", "QUOTE", "ACCEPT", "DECLINE", "RECALL", "RETURN", "INTERNAL_BORROW", "INJECTION",
            "CHATTER"} <= intents
    rfqs = [m for m in labelled if m["label"]["intent"] == "RFQ"]
    assert sum(1 for m in labelled if m["label"]["intent"] == "LOCATE") < len(rfqs) // 4  # the odd stray


def test_llm_off_replay_and_budget(tmp_path):
    cassette = tmp_path / "c.jsonl"
    msgs = [{"role": "user", "content": "hi"}]
    key = LLM.key("muse-spark-1.3", msgs, {"temperature": 0.7})
    cassette.write_text(json.dumps({"key": key, "output": "hello from tape"}) + "\n")

    off = LLM(LLMSettings(MOCK_LLM_MODE="off", MOCK_LLM_CASSETTE=cassette))
    assert asyncio.run(off.complete(msgs, temperature=0.7)) is None

    rep = LLM(LLMSettings(MOCK_LLM_MODE="replay", MOCK_LLM_CASSETTE=cassette))
    assert asyncio.run(rep.complete(msgs, temperature=0.7)) == "hello from tape"
    assert asyncio.run(rep.complete(msgs, temperature=0.1)) is None and rep.misses == 1

    live = LLM(LLMSettings(MOCK_LLM_MODE="live", MOCK_LLM_CASSETTE=cassette, META_AI_API="x",
                           MOCK_LLM_BUDGET_USD=0.0))
    assert asyncio.run(live.complete(msgs)) is None  # budget exhausted: never calls the API
    assert "x" not in json.dumps(live.stats())  # key never exposed
