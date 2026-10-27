"""Simulated clock shared by every mock.

The authoritative state lives in one row of ``control.clock``. Each service caches it and
re-reads it every ``clock_poll_s``. Two modes:

* ``live``: sim time advances at ``speed`` x wall time from an anchor (paused freezes it).
* ``stepped``: sim time only moves when mock-control advances it; services run their
  due jobs when told to via ``/_admin/tick``. This makes a scenario replay bit-for-bit.

A request (or scheduled job) can pin "now" with the ``X-Sim-Time`` header / ``at()``
context, so causally-driven events carry deterministic timestamps.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine

# 2026-10-27 08:55 America/New_York (EDT, UTC-4) = 12:55Z.
DEFAULT_EPOCH = datetime(2026, 10, 27, 12, 55, tzinfo=UTC)
NEW_YORK = ZoneInfo("America/New_York")

control_metadata = MetaData(schema="control")
clock_table = Table(
    "clock",
    control_metadata,
    Column("id", Integer, primary_key=True),
    Column("sim_epoch", DateTime(timezone=True), nullable=False),
    Column("wall_anchor", Float, nullable=False),
    Column("speed", Float, nullable=False),
    Column("paused", Boolean, nullable=False),
    Column("mode", String(16), nullable=False),
    Column("seed", Integer, nullable=False),
)

_pinned: contextvars.ContextVar[datetime | None] = contextvars.ContextVar("sim_pinned", default=None)


@dataclass(frozen=True)
class ClockState:
    sim_epoch: datetime = DEFAULT_EPOCH
    wall_anchor: float = 0.0
    speed: float = 1.0
    paused: bool = True
    mode: str = "live"
    seed: int = 20261027

    def now(self, wall: float | None = None) -> datetime:
        if self.paused or self.mode == "stepped":
            return self.sim_epoch
        wall = time.time() if wall is None else wall
        return self.sim_epoch + timedelta(seconds=(wall - self.wall_anchor) * self.speed)

    def to_json(self) -> dict:
        d = asdict(self)
        d["sim_epoch"] = self.sim_epoch.isoformat()
        d["now"] = iso(self.now())
        d["now_local"] = self.now().astimezone(NEW_YORK).isoformat(timespec="milliseconds")
        return d


class SimClock:
    """Process-local view of the shared clock."""

    def __init__(self, state: ClockState | None = None) -> None:
        self.state = state or ClockState()

    def now(self) -> datetime:
        pinned = _pinned.get()
        return pinned if pinned is not None else self.state.now()

    def live_now(self) -> datetime:
        return self.state.now()

    # -- mutations (used by mock-control and by tests) --------------------------------
    def set(self, **changes) -> ClockState:
        # Re-anchor so a speed/pause change doesn't make time jump.
        now = time.time()
        current = self.state.now(now)
        self.state = replace(self.state, sim_epoch=current, wall_anchor=now, **changes)
        return self.state

    def jump(self, to: datetime) -> ClockState:
        self.state = replace(self.state, sim_epoch=to, wall_anchor=time.time())
        return self.state


clock = SimClock()


def now() -> datetime:
    return clock.now()


@contextlib.contextmanager
def at(ts: datetime) -> Iterator[None]:
    """Pin ``now()`` for the current task (request handlers and scheduled jobs)."""
    token = _pinned.set(ts)
    try:
        yield
    finally:
        _pinned.reset(token)


def pin(ts: datetime | None) -> contextvars.Token:
    return _pinned.set(ts)


def unpin(token: contextvars.Token) -> None:
    _pinned.reset(token)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def sim_date_local() -> str:
    return now().astimezone(NEW_YORK).date().isoformat()


# -- persistence --------------------------------------------------------------------------
async def ensure_clock_table(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS control")
        await conn.run_sync(control_metadata.create_all)
        row = (await conn.execute(select(clock_table).where(clock_table.c.id == 1))).first()
        if row is None:
            s = ClockState(wall_anchor=time.time())
            await conn.execute(
                clock_table.insert().values(
                    id=1,
                    sim_epoch=s.sim_epoch,
                    wall_anchor=s.wall_anchor,
                    speed=s.speed,
                    paused=s.paused,
                    mode=s.mode,
                    seed=s.seed,
                )
            )


async def load_clock(engine: AsyncEngine) -> ClockState:
    async with engine.connect() as conn:
        row = (await conn.execute(select(clock_table).where(clock_table.c.id == 1))).mappings().first()
    if row is None:
        return ClockState()
    return ClockState(
        sim_epoch=row["sim_epoch"],
        wall_anchor=row["wall_anchor"],
        speed=row["speed"],
        paused=row["paused"],
        mode=row["mode"],
        seed=row["seed"],
    )


async def save_clock(engine: AsyncEngine, state: ClockState) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            clock_table.update()
            .where(clock_table.c.id == 1)
            .values(
                sim_epoch=state.sim_epoch,
                wall_anchor=state.wall_anchor,
                speed=state.speed,
                paused=state.paused,
                mode=state.mode,
                seed=state.seed,
            )
        )
