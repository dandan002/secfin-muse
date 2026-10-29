"""Scenario files: timed steps that drive the mocks.

```yaml
name: demo
start: "2026-10-27T08:55:00-04:00"   # sim time at step 0
speed: 10                            # live-mode default
steps:
  - at: "09:00:00"                   # local (America/New_York) time on the start date ...
    do: ib.persona                   # <service>.<verb> -> POST {service}/_admin/scenario/<verb>
    args: {persona: kestrel-hf, action: rfq, qty: 500000}
  - at: "+00:30"                     # ... or an offset (mm:ss / hh:mm:ss) from the previous step
    do: gateway.burst
    args: {counterparty: KESTRELHF, lines: 25}
  - at: "09:05:00"
    do: http                         # raw call
    args: {service: sfp, method: PUT, path: /_admin/faults, json: {latency_ms: 50}}
  - at: "16:00:00"
    do: wait                         # just advance the clock
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from mockcore.clock import NEW_YORK, parse_ts


@dataclass
class Step:
    at: datetime
    do: str
    args: dict[str, Any] = field(default_factory=dict)
    label: str = ""


@dataclass
class Scenario:
    name: str
    description: str
    start: datetime
    speed: float
    seed: int | None
    steps: list[Step]
    end: datetime | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "start": self.start.isoformat(),
            "speed": self.speed,
            "seed": self.seed,
            "steps": len(self.steps),
            "end": (self.end or (self.steps[-1].at if self.steps else self.start)).isoformat(),
        }


def _parse_offset(s: str) -> timedelta:
    parts = [float(p) for p in s.lstrip("+").split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, sec = parts
    return timedelta(hours=h, minutes=m, seconds=sec)


def _resolve_at(raw: Any, start: datetime, prev: datetime) -> datetime:
    s = str(raw)
    if s.startswith("+"):
        return prev + _parse_offset(s)
    if "T" in s:
        return parse_ts(s)
    hh, mm, *rest = (int(float(x)) for x in s.split(":"))
    local_day = start.astimezone(NEW_YORK).date()
    t = time(hh, mm, rest[0] if rest else 0)
    return datetime.combine(local_day, t, tzinfo=NEW_YORK)


def load(path: Path) -> Scenario:
    doc = yaml.safe_load(path.read_text())
    start = parse_ts(str(doc["start"]))
    steps: list[Step] = []
    prev = start
    for raw in doc.get("steps", []):
        at = _resolve_at(raw.get("at", "+0"), start, prev)
        steps.append(Step(at=at, do=raw["do"], args=raw.get("args") or {}, label=raw.get("label", "")))
        prev = at
    steps.sort(key=lambda s: s.at)  # stable: equal times keep file order
    end = _resolve_at(doc["end"], start, prev) if doc.get("end") else None
    return Scenario(
        name=doc.get("name", path.stem),
        description=doc.get("description", ""),
        start=start,
        speed=float(doc.get("speed", 10)),
        seed=doc.get("seed"),
        steps=steps,
        end=end,
    )


def discover(directory: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted(directory.glob("*.y*ml"))}
