"""The desk's view of time: it follows the simulation clock that mock-control publishes.

The platform reads ``GET /clock`` on a heartbeat and extrapolates in between, so every timestamp
the desk writes (decisions, expiries, Reg SHO basis) is in sim time, like the venues'. Until the
first read succeeds it runs from the default demo epoch at 1x. Stored in UTC, shown in New York time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_EPOCH = datetime(2026, 10, 27, 12, 55, tzinfo=UTC)  # 08:55 New York


@dataclass(frozen=True)
class ClockState:
    sim_epoch: datetime = DEFAULT_EPOCH
    wall_anchor: float = 0.0
    speed: float = 1.0
    paused: bool = False
    mode: str = "live"
    seed: int = 20261027
    synced: bool = False  # has the platform heard from mock-control yet?

    def now(self) -> datetime:
        if self.paused or self.mode == "stepped":
            return self.sim_epoch
        return self.sim_epoch + timedelta(seconds=(time.time() - self.wall_anchor) * self.speed)


_state = ClockState(wall_anchor=time.time())


def now() -> datetime:
    return _state.now()


def state() -> ClockState:
    return _state


def apply(payload: dict) -> ClockState:
    """Adopt a ``GET /clock`` body from mock-control."""
    global _state
    _state = ClockState(
        sim_epoch=parse_ts(payload["sim_epoch"]), wall_anchor=float(payload["wall_anchor"]),
        speed=float(payload["speed"]), paused=bool(payload["paused"]), mode=str(payload["mode"]),
        seed=int(payload["seed"]), synced=True,
    )
    return _state


def pin(at: datetime) -> None:
    """Freeze the desk clock at ``at`` (tests)."""
    global _state
    _state = replace(_state, sim_epoch=at, paused=True)


def iso(ts: datetime | None) -> str | None:
    if ts is not None and ts.tzinfo is None:  # SQLite drops the zone; everything is stored in UTC
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") if ts else None


def parse_ts(s: str) -> datetime:
    ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def local_date(ts: datetime | None = None) -> date:
    ts = ts or now()
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).astimezone(NEW_YORK).date()


def end_of_day(ts: datetime | None = None) -> datetime:
    d = local_date(ts)
    return datetime(d.year, d.month, d.day, 23, 59, tzinfo=NEW_YORK).astimezone(UTC)


def to_json() -> dict:
    n = now()
    return {"now": iso(n), "nowLocal": n.astimezone(NEW_YORK).isoformat(timespec="seconds"), "mode": _state.mode,
            "speed": _state.speed, "paused": _state.paused, "seed": _state.seed, "synced": _state.synced}
