"""Counterparty Gateway: locates arrive in the inbox; decisions go back and reach the client as webhooks."""

from __future__ import annotations

from typing import Any

from . import Client


class Gateway(Client):
    system, label = "gateway", "Counterparty Gateway"

    def key_from_credentials(self, cred: dict[str, Any]) -> str:
        return cred["platformToken"]

    async def inbox(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return await self.get("/v1/inbox", params={"after": after, "limit": limit})

    async def decide(self, locate_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.post("/v1/decisions", json={"locateId": locate_id, "lines": lines})

    async def stock_record(self, trade_date: str | None = None) -> dict[str, Any]:
        """Client short positions at a close (default the latest) and how much of each locate was used."""
        return await self.get("/v1/stock-record", params={"date": trade_date} if trade_date else None)

    async def fails(self, after: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        """Client shorts that failed to deliver at CNS (``after``: the last ``seq`` seen)."""
        return await self.get("/v1/fails", params={"after": after, **({"status": status} if status else {})})

    async def buy_ins(self, after: int = 0) -> list[dict[str, Any]]:
        """CNS buy-ins on fails we didn't deliver (``after``: the last ``seq`` seen)."""
        return await self.get("/v1/buy-ins", params={"after": after})

    async def deliver_fail(self, fail_id: str, quantity: int, ref: str | None = None) -> dict[str, Any]:
        return await self.post(f"/v1/fails/{fail_id}/deliver", json={"quantity": quantity, "ref": ref})
