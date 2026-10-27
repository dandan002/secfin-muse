"""Append-only event log per service plus an in-process pub/sub hub for WebSocket fan-out.

Events are written in the caller's transaction and published to subscribers only after
that transaction commits, so a subscriber never sees an event that was rolled back.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Column, DateTime, Identity, MetaData, String, Table, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from . import clock
from .db import JsonType
from .faults import Faults

log = logging.getLogger(__name__)


def make_event_table(metadata: MetaData) -> Table:
    return Table(
        "events",
        metadata,
        Column("id", BigInteger, Identity(), primary_key=True),
        Column("sim_ts", DateTime(timezone=True), nullable=False, index=True),
        Column("wall_ts", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("type", String(64), nullable=False, index=True),
        Column("entity", String(64), nullable=False),
        Column("entity_id", String(64), nullable=False, index=True),
        Column("topic", String(64), nullable=False),
        Column("payload", JsonType, nullable=False),
    )


class Subscriber:
    def __init__(self, hub: Hub, topics: set[str] | None, maxsize: int = 10_000) -> None:
        self.hub = hub
        self.topics = topics
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def wants(self, topic: str) -> bool:
        return self.topics is None or topic in self.topics or "*" in self.topics

    def offer(self, msg: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.dropped += 1

    async def get(self) -> dict[str, Any]:
        return await self.queue.get()

    def close(self) -> None:
        self.hub.subscribers.discard(self)


class Hub:
    def __init__(self, faults: Faults | None = None) -> None:
        self.subscribers: set[Subscriber] = set()
        self.faults = faults
        self.seq = 0  # across all topics: orders events globally
        self.topic_seq: dict[str, int] = {}  # per topic, contiguous: a gap means lost events

    def subscribe(self, topics: Iterable[str] | None = None) -> Subscriber:
        sub = Subscriber(self, set(topics) if topics is not None else None)
        self.subscribers.add(sub)
        return sub

    def publish(self, topic: str, msg: dict[str, Any]) -> None:
        self.seq += 1
        self.topic_seq[topic] = self.topic_seq.get(topic, 0) + 1
        msg = {**msg, "topic": topic, "seq": self.seq, "topicSeq": self.topic_seq[topic]}
        out = self.faults.outbound(msg) if self.faults else [msg]
        for m in out:
            for sub in list(self.subscribers):
                if sub.wants(m["topic"]):
                    sub.offer(m)


class EventLog:
    def __init__(self, table: Table, hub: Hub) -> None:
        self.table = table
        self.hub = hub

    async def emit(
        self,
        session: AsyncSession,
        type: str,
        entity: str,
        entity_id: str,
        payload: dict[str, Any],
        topic: str | None = None,
        ts: datetime | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        ts = ts or clock.now()
        topic = topic or entity
        result = await session.execute(
            self.table.insert()
            .values(sim_ts=ts, type=type, entity=entity, entity_id=entity_id, topic=topic, payload=payload)
            .returning(self.table.c.id)
        )
        event_id = result.scalar_one()
        envelope = {
            "eventId": event_id,
            "type": type,
            "entity": entity,
            "entityId": entity_id,
            "ts": clock.iso(ts),
            "data": payload,
        }
        if publish:
            session.sync_session.info.setdefault("pending_pub", []).append((self.hub, topic, envelope))
        return envelope

    async def list(self, session: AsyncSession, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        t = self.table
        rows = await session.execute(select(t).where(t.c.id > after).order_by(t.c.id).limit(limit))
        return [
            {
                "eventId": r.id,
                "simTs": clock.iso(r.sim_ts),
                "type": r.type,
                "entity": r.entity,
                "entityId": r.entity_id,
                "topic": r.topic,
                "payload": r.payload,
            }
            for r in rows
        ]


@event.listens_for(Session, "after_commit")
def _publish_after_commit(session: Session) -> None:
    pending = session.info.pop("pending_pub", None)
    if not pending:
        return
    for hub, topic, envelope in pending:
        try:
            hub.publish(topic, envelope)
        except Exception:  # pragma: no cover - never let fan-out break a commit
            log.exception("publish failed")


@event.listens_for(Session, "after_rollback")
def _discard_after_rollback(session: Session) -> None:
    session.info.pop("pending_pub", None)
