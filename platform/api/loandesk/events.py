"""Append-only event log plus an in-process fan-out to the UI's WebSocket.

``record()`` adds an ``events`` row to the caller's session; the matching push goes out only
after that session commits, so the UI never sees a change that was rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from . import clock
from .models import Event


class Hub:
    """Two kinds of subscriber. A UI socket gets everything into a bounded queue: a stalled browser loses its oldest
    message rather than holding the desk's memory (it refetches anyway). The desk's own workers (the autopilot, the
    agent team) act on what they hear, so they name the topics they need and lose nothing: a dropped "card proposed"
    would be a card the autopilot never weighs, or one that waits forever for a review nobody started."""

    def __init__(self) -> None:
        self._subs: dict[asyncio.Queue[dict[str, Any]], frozenset[str] | None] = {}

    def subscribe(self, topics: set[str] | None = None) -> asyncio.Queue[dict[str, Any]]:
        """``topics`` given: a worker's subscription (those topics only, unbounded). None: a UI socket's."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=0 if topics else 1000)
        self._subs[q] = frozenset(topics) if topics else None
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.pop(q, None)

    def publish(self, topic: str, data: Any) -> None:
        msg = {"topic": topic, "ts": clock.iso(clock.now()), "data": data}
        for q, topics in list(self._subs.items()):
            if topics is not None and topic not in topics:
                continue
            if q.full():  # only a UI socket's queue is bounded
                q.get_nowait()
            q.put_nowait(msg)


hub = Hub()


def record(s: AsyncSession, entity: str, entity_id: str, type_: str, after: dict[str, Any] | None = None,
           before: dict[str, Any] | None = None, actor: str = "system", topic: str | None = None) -> None:
    s.add(Event(ts=clock.now(), entity=entity, entity_id=entity_id, type=type_, before=before, after=after,
                actor=actor))
    s.sync_session.info.setdefault("outbox", []).append(
        (topic or entity, {"type": type_, "entity": entity, "id": entity_id, "actor": actor, "data": after}))


def publish_later(s: AsyncSession, topic: str, data: Any) -> None:
    """Push to the UI after commit without writing an event row (e.g. market ticks)."""
    s.sync_session.info.setdefault("outbox", []).append((topic, data))


@sa_event.listens_for(Session, "after_commit")
def _flush_outbox(session: Session) -> None:
    for topic, data in session.info.pop("outbox", []):
        hub.publish(topic, data)


@sa_event.listens_for(Session, "after_rollback")
def _drop_outbox(session: Session) -> None:
    session.info.pop("outbox", None)


def new_id(prefix: str) -> str:
    """Short, collision-safe ids: ``loc_20261027_3f9a1c2b``."""
    return f"{prefix}_{clock.local_date():%Y%m%d}_{uuid.uuid4().hex[:8]}"
