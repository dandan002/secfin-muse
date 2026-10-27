"""A scheduler that runs on simulated time.

Jobs are ordered by (due time, insertion order) and run with ``clock.now()`` pinned to
their due time, so their timestamps don't depend on wall-clock jitter. In ``live`` mode a
background loop runs due jobs; in ``stepped`` mode they only run on ``run_due(until)``.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import clock

log = logging.getLogger(__name__)

JobFn = Callable[..., Awaitable[Any]]
_EPOCH = datetime(2026, 1, 1, tzinfo=clock.UTC)


@dataclass(order=True)
class _Job:
    due: datetime
    seq: int
    name: str = field(compare=False)
    fn: JobFn = field(compare=False)
    args: tuple = field(compare=False, default=())
    every: float | None = field(compare=False, default=None)
    cancelled: bool = field(compare=False, default=False)


def _align(ts: datetime, seconds: float) -> datetime:
    """Next multiple of ``seconds`` strictly after ``ts`` (aligned to a fixed epoch)."""
    elapsed = (ts - _EPOCH).total_seconds()
    n = int(elapsed // seconds) + 1
    return _EPOCH + timedelta(seconds=n * seconds)


class Scheduler:
    def __init__(self) -> None:
        self._heap: list[_Job] = []
        self._seq = itertools.count()
        self._periodic: list[tuple[str, float, JobFn]] = []
        self._lock = asyncio.Lock()
        self._by_name: dict[str, _Job] = {}

    def at(self, due: datetime, name: str, fn: JobFn, *args: Any) -> _Job:
        job = _Job(due, next(self._seq), name, fn, args)
        heapq.heappush(self._heap, job)
        self._by_name[name] = job
        return job

    def after(self, seconds: float, name: str, fn: JobFn, *args: Any) -> _Job:
        return self.at(clock.now() + timedelta(seconds=seconds), name, fn, *args)

    def cancel(self, name: str) -> bool:
        job = self._by_name.pop(name, None)
        if job is None:
            return False
        job.cancelled = True
        return True

    def every(self, seconds: float, name: str, fn: JobFn) -> None:
        """Register a periodic job. It is (re-)armed by ``arm_periodic``/``reset``."""
        self._periodic.append((name, seconds, fn))

    def arm_periodic(self) -> None:
        now = clock.now()
        for name, seconds, fn in self._periodic:
            job = _Job(_align(now, seconds), next(self._seq), name, fn, (), every=seconds)
            heapq.heappush(self._heap, job)

    def reset(self) -> None:
        self._heap.clear()
        self._by_name.clear()
        self._seq = itertools.count()
        self.arm_periodic()

    def pending(self) -> list[dict[str, Any]]:
        return [
            {"name": j.name, "due": clock.iso(j.due), "every": j.every}
            for j in sorted(self._heap)
            if not j.cancelled
        ]

    async def run_due(self, until: datetime) -> int:
        ran = 0
        async with self._lock:
            while self._heap and self._heap[0].due <= until:
                job = heapq.heappop(self._heap)
                if job.cancelled:
                    continue
                if self._by_name.get(job.name) is job:
                    del self._by_name[job.name]
                with clock.at(job.due):
                    try:
                        await job.fn(*job.args)
                    except Exception:
                        log.exception("scheduled job %s failed", job.name)
                ran += 1
                if job.every:
                    nxt = job.due + timedelta(seconds=job.every)
                    if nxt <= until - timedelta(seconds=job.every):
                        # Coalesce after a big clock jump instead of replaying every missed tick.
                        nxt = _align(until - timedelta(seconds=job.every), job.every)
                    heapq.heappush(self._heap, _Job(nxt, next(self._seq), job.name, job.fn, (), job.every))
        return ran

    async def loop(self, poll_s: float) -> None:
        while True:
            state = clock.clock.state
            if state.mode == "live" and not state.paused:
                await self.run_due(clock.clock.live_now())
            await asyncio.sleep(poll_s)
