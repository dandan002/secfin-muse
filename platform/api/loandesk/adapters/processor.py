"""Processor (FIS-style): books and records. CDM-shaped bookings, compare, amend."""

from __future__ import annotations

from typing import Any

from . import Client


class Processor(Client):
    system, label = "processor", "Processor (FIS-style)"

    def headers_for(self, key: str) -> dict[str, str]:
        return {"x-api-key": key}

    def key_from_credentials(self, cred: dict[str, Any]) -> str:
        return cred["apiKey"]

    async def book(self, cdm: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/trades", json=cdm)

    async def compare(self, cdm: dict[str, Any], contract_id: str | None = None) -> dict[str, Any]:
        return await self.post("/compare", json={"trade": cdm, "contractId": contract_id})

    async def amend(self, contract_id: str, fields: dict[str, Any], reason: str) -> dict[str, Any]:
        return await self.post(f"/contracts/{contract_id}/amend", json={"fields": fields, "reason": reason})

    async def lifecycle(self, contract_id: str, event: str, quantity: int | None = None,
                        fee_bps: float | None = None, price: float | None = None) -> dict[str, Any]:
        """RETURN, RECALL, RERATE, MARK or BUY_IN (a venue's, booked here) on one contract."""
        return await self.post(f"/lifecycle/{contract_id}", json={"event": event, "quantity": quantity,
                                                                  "feeBps": fee_bps, "price": price})

    async def run_marks(self) -> dict[str, Any]:
        """Mark every open contract to market; margin calls come back in ``calls``."""
        return await self.post("/marks/run")

    async def positions(self) -> list[dict[str, Any]]:
        """Open quantity per (instrument, side, booking entity) across our contracts."""
        return await self.get("/positions")

    async def contracts(self) -> list[dict[str, Any]]:
        return await self.get("/contracts")

    async def recalls(self, after: int = 0) -> list[dict[str, Any]]:
        """Lenders' recalls on contracts where we borrow, after ``seq``: Venue A fills and bilateral trades."""
        return await self.get("/recalls", params={"after": after})

    async def buy_ins(self, after: int = 0) -> list[dict[str, Any]]:
        """Lenders' buy-ins on our overdue recalls at the processor, after ``seq``."""
        return await self.get("/buy-ins", params={"after": after})
