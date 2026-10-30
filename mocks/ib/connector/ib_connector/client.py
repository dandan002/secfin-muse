"""Desk Chat bot API client (see ../../API.md): what the LoanDesk agent uses to read and post chat."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from ._http import Http


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class User(_Model):
    uuid: str
    handle: str
    displayName: str
    firm: str
    kind: str  # human | persona | bot
    cpCode: str | None = None


class Chip(_Model):
    """A structured instrument reference Desk Chat attached to part of the text."""

    type: str
    instrumentId: str
    isin: str
    composite: str
    quantity: int | None = None
    start: int
    end: int


class Message(_Model):
    messageId: int
    streamId: str
    seq: int  # per stream, increasing by one
    sender: User
    text: str
    chips: list[Chip] = []
    mentions: list[str] = []
    kind: str
    via: str  # human | bot | persona
    blastId: str | None = None
    clientMsgId: str | None = None
    ts: str


class Stream(_Model):
    streamId: str
    name: str
    type: str
    members: list[dict[str, Any]]
    lastSeq: int


class DeskChatBot:
    """>>> async with DeskChatBot(url, bot_token=tok) as bot:
    ...     async for msg in bot.subscribe():
    ...         if msg.sender.kind == "persona":
    ...             await bot.send(msg.streamId, "looking", client_msg_id=f"ack-{msg.messageId}")
    """

    def __init__(self, base_url: str, bot_token: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.bot_token = bot_token
        self._h = Http(base_url, {"Authorization": f"Bearer {bot_token}"}, timeout, transport)

    async def __aenter__(self) -> DeskChatBot:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._h.aclose()

    async def streams(self) -> list[Stream]:
        """Rooms the bot is a member of."""
        return [Stream.model_validate(s) for s in await self._h.json("GET", "/streams")]

    async def messages(self, stream_id: str, after: int = 0, limit: int = 100) -> tuple[list[Message], bool]:
        """One page of messages with ``seq > after``, oldest first, and whether more follow."""
        page = await self._h.json("GET", f"/streams/{stream_id}/messages", params={"after": after, "limit": limit})
        return [Message.model_validate(m) for m in page["messages"]], page["hasMore"]

    async def history(self, stream_id: str, after: int = 0) -> AsyncIterator[Message]:
        """Every message after ``after``, paging as needed (e.g. to catch up after a disconnect)."""
        while True:
            page, more = await self.messages(stream_id, after, limit=1000)
            for m in page:
                yield m
            if not more or not page:
                return
            after = page[-1].seq

    async def send(self, stream_id: str, text: str, chips: list[dict[str, Any]] | None = None,
                   client_msg_id: str | None = None) -> Message:
        """Post as the bot; returns the stored message. ``client_msg_id`` comes back on it (and on
        the subscription), so you can match your own posts."""
        body = {"text": text, "chips": chips, "clientMsgId": client_msg_id}
        data = await self._h.json("POST", f"/chats/{stream_id}/messages",
                                  json={k: v for k, v in body.items() if v is not None})
        return Message.model_validate(data)

    async def compose(self, stream_id: str, text: str, user: str) -> None:
        """Put a draft into a human trader's composer (they review and press send)."""
        await self._h.json("POST", "/compose", json={"roomId": stream_id, "text": text, "user": user})

    async def subscribe(self) -> AsyncIterator[Message]:
        """Live messages in every stream the bot belongs to. Use ``history(stream, after=last_seq)``
        to fill any gap after a reconnect."""
        async for event in self._h.ws(f"/streams/subscribe?token={self.bot_token}"):
            if event.get("type") == "message":
                yield Message.model_validate(event["data"])
