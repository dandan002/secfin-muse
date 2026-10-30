"""Chat inbox: what chat ingest read from Desk Chat (borrows, loans, recalls...) and what it wasn't sure of."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import events
from ..db import get_session
from ..ingest import chat
from ..models import ChatMessage
from .deps import trader

router = APIRouter(tags=["chat"])


@router.get("/chat/messages")
async def messages(status: str | None = None, intent: str | None = None, limit: int = Query(200, le=2000),
                   s: AsyncSession = Depends(get_session)) -> list[dict]:
    q = select(ChatMessage).order_by(ChatMessage.received_at.desc(), ChatMessage.seq.desc()).limit(limit)
    if status:
        q = q.where(ChatMessage.status.in_(status.upper().split(",")))
    if intent:
        q = q.where(ChatMessage.intent.in_(intent.upper().split(",")))
    return [chat.message_json(m) for m in (await s.execute(q)).scalars()]


async def _open(s: AsyncSession, msg_id: str) -> ChatMessage:
    m = await s.get(ChatMessage, msg_id)
    if m is None:
        raise HTTPException(404, "no such chat message")
    if m.status not in ("UNSURE", "FLAGGED", "OPEN", "AGREED"):
        raise HTTPException(409, f"message is {m.status}")
    return m


@router.post("/chat/messages/{msg_id}/dismiss")
async def dismiss(msg_id: str, who: str = Depends(trader), s: AsyncSession = Depends(get_session)) -> dict:
    m = await _open(s, msg_id)
    before = chat.message_json(m)
    m.status = "DISMISSED"
    events.record(s, "chat", m.id, "dismissed", chat.message_json(m), before=before, actor=who)
    await s.commit()
    return chat.message_json(m)
