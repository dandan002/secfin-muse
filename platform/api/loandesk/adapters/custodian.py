"""Custodian: settlement of loan and return legs (the JSON form; the custodian builds the ISO 20022 sese.023), and
cash payments not against securities (a buy-in's cost)."""

from __future__ import annotations

from typing import Any

from . import Client


class Custodian(Client):
    system, label = "custodian", "Custodian"

    def headers_for(self, key: str) -> dict[str, str]:
        return {"x-api-key": key}

    def key_from_credentials(self, cred: dict[str, Any]) -> str:
        return cred["apiKey"]

    async def instruct(self, instruction: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/instructions", json=instruction)

    async def instructions(self, status: str | None = None) -> list[dict[str, Any]]:
        return await self.get("/instructions", params={"status": status} if status else None)

    async def pay(self, payment: dict[str, Any]) -> dict[str, Any]:
        """``{txId, direction: PAY | RECEIVE, amount, ccy, counterpartyBic, valueDate?, reference?}``."""
        return await self.post("/payments", json=payment)

    async def payments(self, status: str | None = None) -> list[dict[str, Any]]:
        return await self.get("/payments", params={"status": status} if status else None)
