"""Optional LLM hook for the chat personas, with record/replay and a spend cap.

Modes (``MOCK_LLM_MODE``):
* ``off`` (default): never called; personas stay scripted.
* ``replay``: answers only from the cassette; a miss returns None (caller falls back).
* ``record``: cassette first, otherwise call the API and append the answer to the cassette.
* ``live``: always call the API (still capped by the budget).

The key is read from ``META_AI_API`` (the repo's .env) and is never logged. Cassettes are
JSONL keyed by a hash of (model, messages, params), so replays are exact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# USD per million tokens (input, output), from the project spec's model table.
PRICES = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-spark-1.3-contributor": (0.10, 4.25),
    "muse-wonder-1.0": (4.0, 20.0),
}


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(str(_REPO_ROOT / ".env"), ".env"), extra="ignore")

    mode: str = Field("off", validation_alias="MOCK_LLM_MODE")
    model: str = Field("muse-spark-1.3", validation_alias="MOCK_LLM_MODEL")
    base_url: str = Field("https://api.meta.ai/v1", validation_alias="MOCK_LLM_BASE_URL")
    api_key: str = Field("", validation_alias="META_AI_API", repr=False)
    cassette: Path = Field(_REPO_ROOT / "mocks" / "cassettes" / "personas.jsonl", validation_alias="MOCK_LLM_CASSETTE")
    budget_usd: float = Field(0.50, validation_alias="MOCK_LLM_BUDGET_USD")
    timeout_s: float = Field(20.0, validation_alias="MOCK_LLM_TIMEOUT_S")


class LLM:
    def __init__(self, cfg: LLMSettings | None = None) -> None:
        self.cfg = cfg or LLMSettings()
        self.spent_usd = 0.0
        self.calls = 0
        self.misses = 0
        self._cache: dict[str, str] | None = None
        self._lock = asyncio.Lock()
        self._client: Any = None

    @property
    def mode(self) -> str:
        return self.cfg.mode

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            self._cache = {}
            if self.cfg.cassette.exists():
                for line in self.cfg.cassette.read_text().splitlines():
                    if line.strip():
                        rec = json.loads(line)
                        self._cache[rec["key"]] = rec["output"]
        return self._cache

    @staticmethod
    def key(model: str, messages: list[dict[str, str]], params: dict[str, Any]) -> str:
        blob = json.dumps({"model": model, "messages": messages, "params": params}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    async def complete(self, messages: list[dict[str, str]], **params: Any) -> str | None:
        if self.mode == "off":
            return None
        model = params.pop("model", self.cfg.model)
        key = self.key(model, messages, params)
        cache = self._load()
        if self.mode in ("replay", "record") and key in cache:
            return cache[key]
        if self.mode == "replay":
            self.misses += 1
            return None
        if not self.cfg.api_key:
            log.warning("LLM mode %s but META_AI_API is not set; falling back to scripted", self.mode)
            return None
        if self.spent_usd >= self.cfg.budget_usd:
            log.warning("LLM budget of $%.2f reached; falling back to scripted", self.cfg.budget_usd)
            return None
        async with self._lock:
            if self._client is None:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(base_url=self.cfg.base_url, api_key=self.cfg.api_key,
                                           timeout=self.cfg.timeout_s)
        try:
            resp = await self._client.chat.completions.create(model=model, messages=messages, **params)
        except Exception as e:  # network/API failure must never break the chat
            log.warning("LLM call failed: %s", type(e).__name__)
            return None
        text = (resp.choices[0].message.content or "").strip()
        self.calls += 1
        if resp.usage:
            pin, pout = PRICES.get(model, (4.0, 20.0))
            self.spent_usd += (resp.usage.prompt_tokens * pin + resp.usage.completion_tokens * pout) / 1e6
        if self.mode == "record":
            cache[key] = text
            self.cfg.cassette.parent.mkdir(parents=True, exist_ok=True)
            with self.cfg.cassette.open("a") as f:
                f.write(json.dumps({"key": key, "model": model, "messages": messages, "output": text}) + "\n")
        return text

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "model": self.cfg.model,
            "calls": self.calls,
            "replayMisses": self.misses,
            "spentUsd": round(self.spent_usd, 4),
            "budgetUsd": self.cfg.budget_usd,
            "keyConfigured": bool(self.cfg.api_key),
        }
