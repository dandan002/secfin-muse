"""Langfuse tracing: on only with keys and real model calls, and what each trace carries. Spans go to an in-memory
exporter, so nothing leaves the test."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, TypedDict

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from loandesk import observe
from loandesk.agent import models
from loandesk.settings import get_settings


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[InMemorySpanExporter]:
    from langfuse import Langfuse

    key = f"pk-lf-{request.node.name}"  # Langfuse keeps one client per public key: a fresh key, a fresh exporter
    cfg = get_settings()
    monkeypatch.setattr(cfg, "langfuse_public_key", key)
    monkeypatch.setattr(cfg, "langfuse_secret_key", "sk-lf-test")
    monkeypatch.setattr(cfg, "agent_mode", "live")
    exp = InMemorySpanExporter()
    lf = Langfuse(public_key=key, secret_key="sk-lf-test", host="http://127.0.0.1:9", span_exporter=exp,
                  tracer_provider=TracerProvider())
    monkeypatch.setattr(observe, "_client", lf)
    yield exp
    lf.shutdown()


def _attrs(exp: InMemorySpanExporter, name: str) -> dict[str, Any]:
    observe.client().flush()
    return next(dict(s.attributes) for s in exp.get_finished_spans() if s.name == name)


class _Resp:
    def __init__(self, d: dict[str, Any]) -> None:
        self.d = d

    def model_dump(self) -> dict[str, Any]:
        return self.d


class _SDK:
    """Stands in for the OpenAI client: answers, or raises like a down API."""

    def __init__(self, answer: dict[str, Any] | Exception) -> None:
        self.answer = answer
        self.chat = self
        self.completions = self

    async def create(self, **req: Any) -> _Resp:
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Resp(self.answer)


def _gateway(answer: dict[str, Any] | Exception) -> models.ModelGateway:
    g = models.ModelGateway()
    g._client = _SDK(answer)
    return g


def test_off_without_keys_or_with_recorded_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = get_settings()
    monkeypatch.setattr(cfg, "agent_mode", "live")
    assert not observe.enabled()  # no keys
    monkeypatch.setattr(cfg, "langfuse_public_key", "pk")
    monkeypatch.setattr(cfg, "langfuse_secret_key", "sk")
    for mode in ("off", "replay"):  # CI and the free demo replay never trace
        monkeypatch.setattr(cfg, "agent_mode", mode)
        assert not observe.enabled()
    with observe.generation({"model": "m", "messages": []}) as g:  # off: a no-op handle
        g.done({"usage": {}}, 0.0)


async def test_a_model_call_is_a_generation_with_tokens_and_cost(spans: InMemorySpanExporter) -> None:
    answer = {"choices": [{"message": {"role": "assistant", "content": "{\"intent\": \"RFQ\"}"}}],
              "usage": {"prompt_tokens": 1200, "completion_tokens": 40,
                        "prompt_tokens_details": {"cached_tokens": 1000}}}
    resp = await _gateway(answer).chat(model="muse-spark-1.3", reasoning_effort="low",
                                       messages=[{"role": "user", "content": "bid 250k ABC 35 open"}])
    assert resp["choices"][0]["message"]["content"].startswith("{")
    a = _attrs(spans, "chat muse-spark-1.3")
    assert a["langfuse.observation.type"] == "generation" and a["langfuse.observation.model.name"] == "muse-spark-1.3"
    assert json.loads(a["langfuse.observation.usage_details"]) == {"input": 200, "input_cached": 1000, "output": 40}
    cost = json.loads(a["langfuse.observation.cost_details"])["total"]
    assert cost == pytest.approx((200 * 1.25 + 1000 * 0.15 + 40 * 4.25) / 1e6)  # the desk's own price table
    assert "bid 250k ABC" in a["langfuse.observation.input"]
    assert "reasoning_effort" in a["langfuse.observation.model.parameters"]


async def test_a_failed_call_is_marked_as_an_error(spans: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError):
        await _gateway(RuntimeError("503 from the model API")).chat(model="muse-wonder-1.0", messages=[])
    a = _attrs(spans, "chat muse-wonder-1.0")
    assert a["langfuse.observation.level"] == "ERROR" and "503" in a["langfuse.observation.status_message"]


async def test_a_run_is_a_session_with_the_desks_ids_and_graph_nodes(spans: InMemorySpanExporter) -> None:
    from langgraph.graph import END, StateGraph

    class S(TypedDict):
        n: int

    g = StateGraph(S)
    g.add_node("gather", lambda s: {"n": s["n"] + 1})
    g.add_node("propose", lambda s: {"n": s["n"] + 1})
    g.set_entry_point("gather")
    g.add_edge("gather", "propose")
    g.add_edge("propose", END)
    graph = g.compile()
    async with observe.run("review_locate", "run_20261027_ab12cd34", subject="loc_1", actionId=None) as callbacks:
        assert callbacks
        assert (await graph.ainvoke({"n": 0}, {"callbacks": callbacks}))["n"] == 2
    root = _attrs(spans, "review_locate")
    assert root["session.id"] == "run_20261027_ab12cd34" and root["langfuse.observation.type"] == "agent"
    assert json.loads(root["langfuse.observation.metadata.subject"]) == "loc_1"
    assert "langfuse.observation.metadata.actionId" not in root  # unset ids are left out
    names = {s.name for s in spans.get_finished_spans()}
    assert {"gather", "propose"} <= names  # LangGraph's nodes, through the callback
    assert all(dict(s.attributes).get("session.id") == "run_20261027_ab12cd34" for s in spans.get_finished_spans())


def test_a_chat_read_is_a_session_per_room(spans: InMemorySpanExporter) -> None:
    with observe.read("room-harbor", "msg-1", "HARBORAL"):
        pass
    a = _attrs(spans, "read_chat")
    assert a["session.id"] == "room:room-harbor" and json.loads(a["langfuse.observation.metadata.messageId"]) == "msg-1"
