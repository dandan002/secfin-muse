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

    async def lists(self) -> list[dict[str, Any]]:
        return await self.get("/v1/interest-lists")

    async def interest_list(self, list_id: str) -> dict[str, Any]:
        return await self.get(f"/v1/interest-lists/{list_id}")

    async def withdraw(self, list_id: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/v1/interest-lists/{list_id}")

    async def matches(self, list_id: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if list_id:
            params["listId"] = list_id
        return await self.get("/v1/matches", params=params)

    async def accept(self, match_id: str) -> dict[str, Any]:
        return await self.post(f"/v1/matches/{match_id}/accept")

    async def propose(self, match_id: str, fee_bps: float, quantity: int | None = None) -> dict[str, Any]:
        """Our counter: a new fee (and optionally a smaller size); the dealer answers."""
        return await self.post(f"/v1/matches/{match_id}/propose", json={"feeBps": fee_bps, "quantity": quantity})

    async def reject(self, match_id: str) -> dict[str, Any]:
        return await self.post(f"/v1/matches/{match_id}/reject")

    async def loans(self) -> list[dict[str, Any]]:
        return await self.get("/v1/loans")

    async def rerate(self, loan_id: str, fee_bps: float) -> dict[str, Any]:
        """Asks the counterparty; the loan carries ``pendingRerateBps`` until they agree or decline."""
        return await self.post(f"/v1/loans/{loan_id}/rerate", json={"feeBps": fee_bps})

    async def return_qty(self, loan_id: str, quantity: int | None = None) -> dict[str, Any]:
        """Return (we borrowed) or recall (we lent): the venue picks by our side of the loan."""
        return await self.post(f"/v1/loans/{loan_id}/return", json={"quantity": quantity})
