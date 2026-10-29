"""Model gateway: every model call goes through here (spec: *$150 credit budget*).

* ``off``: refuse every call (the desk runs on rules and the trader).
* ``replay``: answer from the cassette only; a miss is an error. CI and load tests use this.
* ``record``: call the Meta Model API and append each request/response to the cassette.
* ``live``: call the API without recording.

Spend is metered per model from token usage and refused past ``LOANDESK_BUDGET_USD``.
Calls go through the OpenAI SDK against ``api.meta.ai/v1`` (installed with the ``agent`` extra).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .. import clock
from ..settings import get_settings

# USD per million tokens: (input, cached input, output). From the spec's model table.
PRICES = {"muse-wonder-1.0": (4.00, 0.20, 20.00), "muse-spark-1.3": (1.25, 0.15, 4.25),
          "muse-spark-1.3-contributor": (0.10, 0.01, 0.35)}


class ModelUnavailable(RuntimeError):
    pass


@dataclass
class Meter:
    spent: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def total(self) -> float:
        return sum(self.spent.values())

    def add(self, model: str, usage: dict[str, Any]) -> float:
        p_in, p_cached, p_out = PRICES.get(model, PRICES["muse-wonder-1.0"])
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        fresh = (usage.get("prompt_tokens") or 0) - cached
        cost = (fresh * p_in + cached * p_cached + (usage.get("completion_tokens") or 0) * p_out) / 1e6
        self.spent[model] += cost
        self.calls[model] += 1
        return cost


meter = Meter()


# Timestamps and generated ids change on every run; the recording key ignores them so a replay of the same
# conversation shape (seeded mocks, same inputs) finds its recording.
_VOLATILE = [(re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
             (re.compile(r"\b[a-z]{2,6}_\d{8}_[0-9a-f]{8}\b"), "<id>"),
             (re.compile(r"\bcall_[A-Za-z0-9]+\b"), "<call>")]


def _key(req: dict[str, Any]) -> str:
    text = json.dumps(req, sort_keys=True, default=str)
    for pattern, repl in _VOLATILE:
        text = pattern.sub(repl, text)
    return hashlib.sha256(text.encode()).hexdigest()


class ModelGateway:
    def __init__(self) -> None:
        self.cfg = get_settings()
        self._cassette: dict[str, dict[str, Any]] | None = None
        self._client: Any = None

    @property
    def mode(self) -> str:
        return self.cfg.agent_mode

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "baseUrl": self.cfg.model_base_url, "keyConfigured": bool(self.cfg.model_api_key),
                "budgetUsd": self.cfg.budget_usd, "spentUsd": round(meter.total, 4),
                "byModel": {m: {"usd": round(v, 4), "calls": meter.calls[m]} for m, v in meter.spent.items()},
                "models": {"orchestrator": self.cfg.orchestrator_model, "reviewer": self.cfg.reviewer_model,
                           "parser": self.cfg.parser_model}}

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._cassette is None:
            self._cassette = {}
            path = self.cfg.cassette_path
            if path.exists():
                for line in path.read_text().splitlines():
                    if line.strip():
                        rec = json.loads(line)
                        self._cassette[rec["key"]] = rec["response"]
        return self._cassette

    def _sdk(self) -> Any:
        if self._client is None:
            if not self.cfg.model_api_key:
                raise ModelUnavailable("no model API key (set META_AI_API or LOANDESK_MODEL_API_KEY)")
            try:
                from openai import AsyncOpenAI
            except ImportError as e:
                raise ModelUnavailable("install the agent extra: uv sync --extra agent") from e
            self._client = AsyncOpenAI(base_url=self.cfg.model_base_url, api_key=self.cfg.model_api_key)
        return self._client

    async def chat(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   response_format: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        """One chat completion, as a plain dict (``choices[0].message`` etc.)."""
        req = {"model": model, "messages": messages, "tools": tools, "response_format": response_format, **kw}
        req = {k: v for k, v in req.items() if v is not None}
        key = _key(req)
        if self.mode == "off":
            raise ModelUnavailable("agent is off (LOANDESK_AGENT_MODE=off)")
        if self.mode == "replay":
            hit = self._load().get(key)
            if hit is None:
                raise ModelUnavailable(f"no recording for this request ({key[:12]}) in {self.cfg.cassette_path}")
            return hit
        if meter.total >= self.cfg.budget_usd:
            raise ModelUnavailable(f"budget of ${self.cfg.budget_usd:.2f} spent")
        resp = (await self._sdk().chat.completions.create(**req)).model_dump()
        meter.add(model, resp.get("usage") or {})
        if self.mode == "record":
            path = self.cfg.cassette_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps({"key": key, "ts": clock.iso(clock.now()), "model": model, "response": resp}) + "\n")
            self._load()[key] = resp
        return resp


_gateway: ModelGateway | None = None


def gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
