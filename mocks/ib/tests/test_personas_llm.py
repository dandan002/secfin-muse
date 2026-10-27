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
        drafts = P.locate_blast(r, p, 12) + [P.ioi(r, p), P.rfq(r, p)[0], P.injection(r, p)]
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


def test_react_to_quotes():
    p = P.PERSONAS["kestrel-hf"]
    st = {"instrumentId": "I00005", "composite": "OESV US", "qty": 500000, "maxFeeBps": 700, "counters": 0}
    r = random.Random(0)
    assert P.react(r, p, "can show 650bps", st)[1].label["intent"] == "TRADE_CONFIRM"
    delay, d, new = P.react(r, p, "best is 800", st) or (None, None, None)
    assert d is None or d.label["intent"] in ("COUNTER", "CHATTER")
    _, d, new = P.react(r, p, "@ 780", st)
    assert d.label["intent"] == "COUNTER" and new["counters"] == 1
    assert P.react(r, p, "sorry none available", st)[2] is None
    assert P.parse_fee("indic 6.5%") == 650 and P.parse_fee("@ 700") == 700


def test_corpus_is_deterministic_and_labelled():
    a = corpus.generate(300, 20261027)
    assert a == corpus.generate(300, 20261027)
    assert len(a) == 300
    labelled = [m for c in a for m in c["messages"] if "label" in m]
    intents = {m["label"]["intent"] for m in labelled}
    assert {"LOCATE", "IOI", "RFQ", "INTERNAL_BORROW", "INJECTION", "CHATTER", "TRADE_CONFIRM"} <= intents


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
