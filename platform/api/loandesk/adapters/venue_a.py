"""Venue A (NGT-style) over REST. A FIX session adapter can replace this behind the same methods."""

from __future__ import annotations

from typing import Any

from . import Client


class VenueA(Client):
    system, label = "venue_a", "Venue A (NGT-style)"

    def headers_for(self, key: str) -> dict[str, str]:
        return {"x-participant-key": key}

    def key_from_credentials(self, cred: dict[str, Any]) -> str:
        return cred["participantKey"]

    async def availability(self, instrument: str | None = None, limit: int = 5000) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if instrument:
            params["instrument"] = instrument
        return await self.get("/v1/availability", params=params)

    async def prints(self, instrument: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if instrument:
            params["instrument"] = instrument
        return await self.get("/v1/prints", params=params)

    async def place(self, order: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/v1/orders", json=order)

    async def order(self, order_id: str) -> dict[str, Any]:
        return await self.get(f"/v1/orders/{order_id}")

    async def orders(self, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self.get("/v1/orders", params=params)

    async def accept(self, order_id: str) -> dict[str, Any]:
        return await self.post(f"/v1/orders/{order_id}/accept")

    async def counter(self, order_id: str, fee_bps: float, quantity: int | None = None) -> dict[str, Any]:
        return await self.post(f"/v1/orders/{order_id}/counter", json={"feeBps": fee_bps, "quantity": quantity})

    async def cancel(self, order_id: str) -> dict[str, Any]:
        return await self.post(f"/v1/orders/{order_id}/cancel")

    async def trades(self, since: str | None = None) -> list[dict[str, Any]]:
        return await self.get("/v1/trades", params={"since": since} if since else None)
