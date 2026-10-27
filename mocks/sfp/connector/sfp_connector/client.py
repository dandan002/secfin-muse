"""Async client for the processor's books-and-records API (see ../../API.md)."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterable
from datetime import date
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from ._http import Http


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class Contract(_Model):
    contractId: str
    uti: str | None = None
    venue: str | None = None
    venueRef: str | None = None
    clientRef: str | None = None
    status: Literal["OPEN", "CLOSED"]
    side: Literal["BORROW", "LEND"]
    counterparty: dict[str, Any]
    isin: str
    quantity: int
    openQuantity: int
    feeBps: float
    price: float
    loanValue: float
    collateralType: str
    collateralCcy: str
    marginPct: float
    collateralValue: float
    settlementDate: date
    history: list[dict[str, Any]] = []


class Booking(_Model):
    contractId: str
    status: Literal["BOOKED", "DUPLICATE"]
    contract: Contract


class Diff(_Model):
    field: str
    ours: Any
    processor: Any


class CompareResult(_Model):
    contractId: str
    status: Literal["MATCHED", "UNMATCHED"]
    diffs: list[Diff]
    breakId: str | None = None


class Break(_Model):
    breakId: str
    contractId: str
    source: str
    diffs: list[Diff]
    status: Literal["OPEN", "RESOLVED"]


class AmendResult(_Model):
    contract: Contract
    changes: dict[str, Any]
    resolvedBreaks: list[str]


class MarginCall(_Model):
    callId: str
    contractId: str
    markDate: date
    direction: Literal["WE_CALL", "WE_ARE_CALLED"]
    amount: float
    ccy: str
    deliverOrReturn: Literal["DLVR", "RTRN"]


class ProcessorClient:
    """Book trades, reconcile them, run lifecycle events, and read marks and the EOD file.

    >>> async with ProcessorClient(url, api_key=key) as sfp:
    ...     booking = await sfp.book(securities_lending_trade(...))
    ...     result = await sfp.compare(our_view_of_the_trade)          # MATCHED / UNMATCHED + diffs
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key
        self._h = Http(base_url, {"X-Api-Key": api_key}, timeout, transport)

    async def __aenter__(self) -> ProcessorClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._h.aclose()

    # -- booking ------------------------------------------------------------------------------
    async def book(self, trade: dict[str, Any]) -> Booking:
        """CDM-shaped (``cdm.securities_lending_trade``) or flat form. Re-sending the same
        ``clientRef`` returns the existing contract with status DUPLICATE."""
        return Booking.model_validate(await self._h.json("POST", "/trades", json=trade))

    async def contract(self, ref: str) -> Contract:
        """By contract id, UTI or venue reference."""
        return Contract.model_validate(await self._h.json("GET", f"/contracts/{ref}"))

    async def contracts(self, status: str | None = None, instrument: str | None = None,
                        limit: int = 500) -> list[Contract]:
        params = {k: v for k, v in {"status": status, "instrument": instrument, "limit": limit}.items() if v}
        return [Contract.model_validate(c) for c in await self._h.json("GET", "/contracts", params=params)]

    async def positions(self, booking_entity: str | None = None) -> list[dict[str, Any]]:
        params = {"bookingEntity": booking_entity} if booking_entity else {}
        return await self._h.json("GET", "/positions", params=params)

    # -- reconciliation -----------------------------------------------------------------------
    async def compare(self, trade: dict[str, Any], contract_id: str | None = None) -> CompareResult:
        """Compare our view of a trade (flat or CDM) with the booked contract; opens a break on diffs."""
        body: dict[str, Any] = {"trade": trade} | ({"contractId": contract_id} if contract_id else {})
        return CompareResult.model_validate(await self._h.json("POST", "/compare", json=body))

    async def breaks(self, status: str | None = None) -> list[Break]:
        params = {"status": status} if status else {}
        return [Break.model_validate(b) for b in await self._h.json("GET", "/breaks", params=params)]

    async def amend(self, ref: str, fields: dict[str, Any], reason: str = "") -> AmendResult:
        """Correct booked fields; breaks the amendment fixes resolve automatically."""
        body = {"fields": fields, "reason": reason}
        return AmendResult.model_validate(await self._h.json("POST", f"/contracts/{ref}/amend", json=body))

    # -- lifecycle ----------------------------------------------------------------------------
    async def _lifecycle(self, ref: str, event: str, **body: Any) -> dict[str, Any]:
        payload = {"event": event} | {k: (v.isoformat() if isinstance(v, date) else v) for k, v in body.items()
                                      if v is not None}
        return await self._h.json("POST", f"/lifecycle/{ref}", json=payload)

    async def return_shares(self, ref: str, quantity: int | None = None, effective: date | None = None) -> Contract:
        out = await self._lifecycle(ref, "RETURN", quantity=quantity, effectiveDate=effective)
        return Contract.model_validate(out["contract"])

    async def recall(self, ref: str, quantity: int | None = None, effective: date | None = None) -> Contract:
        out = await self._lifecycle(ref, "RECALL", quantity=quantity, effectiveDate=effective)
        return Contract.model_validate(out["contract"])

    async def rerate(self, ref: str, fee_bps: float, effective: date | None = None) -> Contract:
        out = await self._lifecycle(ref, "RERATE", feeBps=fee_bps, effectiveDate=effective)
        return Contract.model_validate(out["contract"])

    async def mark(self, ref: str) -> dict[str, Any]:
        """Mark one contract now; returns the mark, with ``marginCall`` when collateral must move."""
        return (await self._lifecycle(ref, "MARK"))["mark"]

    # -- marks and collateral -----------------------------------------------------------------
    async def run_marks(self) -> dict[str, Any]:
        return await self._h.json("POST", "/marks/run")

    async def marks(self, contract_id: str | None = None) -> list[dict[str, Any]]:
        params = {"contractId": contract_id} if contract_id else {}
        return await self._h.json("GET", "/marks", params=params)

    async def margin_calls(self, since: date | None = None) -> list[MarginCall]:
        params = {"since": since.isoformat()} if since else {}
        return [MarginCall.model_validate(c) for c in await self._h.json("GET", "/margin-calls", params=params)]

    async def margin_call_colr003(self, call_id: str) -> bytes:
        """The margin call as ISO 20022 colr.003 MarginCallRequest XML."""
        return (await self._h.raw("GET", f"/margin-calls/{call_id}/colr.003")).content

    # -- end of day ---------------------------------------------------------------------------
    async def eod_contracts(self, as_of: date | None = None) -> list[dict[str, str]]:
        """The end-of-day contracts file, parsed: one dict per row (CSV columns as keys)."""
        params = {"asOf": as_of.isoformat()} if as_of else {}
        text = (await self._h.raw("GET", "/eod/contracts.csv", params=params)).text
        return list(csv.DictReader(io.StringIO(text)))

    # -- streaming ----------------------------------------------------------------------------
    async def stream(self, topics: Iterable[str] = ("contracts", "breaks", "marks")) -> AsyncIterator[dict[str, Any]]:
        """Events: contract_booked / _amended / _return / _recall / _rerate / _mark, break_opened /
        break_resolved, margin_call. Each has ``topic``, ``type``, ``seq``, ``data``."""
        async for event in self._h.ws(f"/stream?key={self.api_key}&topics={','.join(topics)}"):
            yield event
