"""Desk Chat (IB-style) bot API: read rooms, post as the desk bot, push drafts into the trader's composer.

Chat ingest (the parser subagent) builds on ``messages()``; sending is an external side effect,
so it only ever happens from an approved action.
"""

from __future__ import annotations

from typing import Any

from . import Client


class DeskChat(Client):
    system, label = "chat", "Desk Chat"

    def key_from_credentials(self, cred: dict[str, Any]) -> str:
        return cred["botToken"]

    async def streams(self) -> list[dict[str, Any]]:
        return await self.get("/streams")

    async def messages(self, stream_id: str, after: int = 0, limit: int = 200) -> dict[str, Any]:
        return await self.get(f"/streams/{stream_id}/messages", params={"after": after, "limit": limit})

    async def send(self, room_id: str, text: str, client_msg_id: str) -> dict[str, Any]:
        return await self.post(f"/chats/{room_id}/messages", json={"text": text, "clientMsgId": client_msg_id})

    async def compose(self, room_id: str, text: str, user: str) -> dict[str, Any]:
        return await self.post("/compose", json={"roomId": room_id, "text": text, "user": user})
