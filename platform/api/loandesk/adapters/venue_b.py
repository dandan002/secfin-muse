"""Venue B (Wematch-style): interest lists, optimiser, negotiation and loan lifecycle."""

from __future__ import annotations

from typing import Any

from . import Client


class VenueB(Client):
    system, label = "venue_b", "Venue B (Wematch-style)"

    def headers_for(self, key: str) -> dict[str, str]:
        return {"x-api-key": key}

    def key_from_credentials(self, cred: dict[str, Any]) -> str:
        return cred["apiKey"]

    async def upload_list(self, name: str, lines: list[dict[str, Any]], auto_accept: bool = False,
                          client_ref: str | None = None) -> dict[str, Any]:
        return await self.post("/v1/interest-lists", json={"name": name, "lines": lines, "autoAccept": auto_accept,
                                                           "clientRef": client_ref})

    async def run_optimiser(self, list_id: str) -> dict[str, Any]:
        return await self.post("/v1/optimiser/run", json={"listId": list_id})

    async def interest_list(self, list_id: str) -> dict[str, Any]:
        return await self.get(f"/v1/interest-lists/{list_id}")

    async def matches(self) -> list[dict[str, Any]]:
        return await self.get("/v1/matches")

    async def accept(self, match_id: str) -> dict[str, Any]:
        return await self.post(f"/v1/matches/{match_id}/accept")

    async def loans(self) -> list[dict[str, Any]]:
        return await self.get("/v1/loans")
