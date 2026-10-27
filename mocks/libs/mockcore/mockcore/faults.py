"""Fault injection: latency, outages, and dropped/duplicated/reordered outbound messages.

HTTP faults (latency, outage) are applied by middleware in ``app.py``. Message faults
(drop, dup, reorder) apply to everything a service pushes out: WebSocket events, FIX
messages and webhooks. Decisions come from a seeded stream, so a chaos drill replays.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from . import rng


class FaultConfig(BaseModel):
    latency_ms: int = Field(0, ge=0, le=60_000, description="Added latency on every non-admin request")
    jitter_ms: int = Field(0, ge=0, le=60_000)
    drop_pct: float = Field(0.0, ge=0, le=100, description="Outbound messages silently dropped")
    dup_pct: float = Field(0.0, ge=0, le=100, description="Outbound messages sent twice")
    reorder_pct: float = Field(0.0, ge=0, le=100, description="Outbound messages held and sent after the next")
    outage: bool = Field(False, description="All non-admin endpoints return 503; sessions refuse logon")
    error_pct: float = Field(0.0, ge=0, le=100, description="Non-admin requests fail with 500")


@dataclass
class Faults:
    service: str
    config: FaultConfig = field(default_factory=FaultConfig)
    _held: list[Any] = field(default_factory=list)

    @property
    def _rng(self) -> random.Random:
        return rng.stream("faults", self.service)

    def reset(self) -> None:
        self.config = FaultConfig()
        self._held.clear()

    def http_delay_s(self) -> float:
        c = self.config
        if not c.latency_ms and not c.jitter_ms:
            return 0.0
        return (c.latency_ms + self._rng.uniform(0, c.jitter_ms)) / 1000.0

    def http_error(self) -> bool:
        return self.config.error_pct > 0 and self._rng.random() * 100 < self.config.error_pct

    def outbound(self, msg: Any) -> list[Any]:
        """Return the messages to actually send in place of ``msg`` (0, 1, 2 or more)."""
        c = self.config
        if not (c.drop_pct or c.dup_pct or c.reorder_pct):
            out, self._held = [*self._held, msg], []
            return out
        r = self._rng
        if c.drop_pct and r.random() * 100 < c.drop_pct:
            return []
        if c.reorder_pct and r.random() * 100 < c.reorder_pct and not self._held:
            self._held.append(msg)
            return []
        out = [msg]
        if c.dup_pct and r.random() * 100 < c.dup_pct:
            out.append(msg)
        out.extend(self._held)
        self._held = []
        return out

    def to_json(self) -> dict:
        return {"service": self.service, **self.config.model_dump(), "held": len(self._held)}
