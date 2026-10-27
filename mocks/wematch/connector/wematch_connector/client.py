"""Async client for Venue B's member API: lists, matches, negotiation, loans (see ../../API.md)."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ._http import Http


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class ListLine(_Model):
    """One line of an interest list. BORROW: ``feeBps`` is the most we pay; LEND: the least we take."""

    instrument: str
    side: Literal["BORROW", "LEND"]
    quantity: int = Field(..., gt=0)
    feeBps: float = Field(..., ge=0)
    minQuantity: int = 0
    term: Literal["OPEN", "TERM"] = "OPEN"
    collateralType: Literal["CASH", "NONCASH"] = "NONCASH"
    collateralCcy: str = "USD"


class InterestList(_Model):
    listId: str
    name: str
    product: str
    status: str
    lines: int  # accepted lines
    rejectedLines: int = 0
    errors: list[dict[str, Any]] = []  # upload response only: {lineNo, error, input} per rejected line
    duplicate: bool = False  # upload response only: same clientRef as an earlier list (that list is returned)
    clientRef: str | None = None


class Match(_Model):
    matchId: str
    listId: str
    counterparty: dict[str, Any]
    isin: str
    side: Literal["BORROW", "LEND"]
    quantity: int
    feeBps: float
    proposedBy: str
    status: str  # PROPOSED (either side may counter or accept), AGREED, REJECTED, EXPIRED
    ourAccept: bool
    counterpartyAccept: bool
    rounds: int


class Loan(_Model):
    loanId: str
    matchId: str
    isin: str
    side: Literal["BORROW", "LEND"]
    counterparty: dict[str, Any]
    quantityOriginal: int
    quantityOpen: int
    feeBps: float
    pendingRerateBps: float | None = None
    status: str
    settlementDate: str


class VenueBClient:
    """List matching: upload interest lists, run the optimiser, negotiate matches, manage loans.

    >>> async with VenueBClient(url, api_key=key) as wm:
    ...     line = ListLine(instrument="US9939277482", side="BORROW", quantity=500_000, feeBps=40)
    ...     lst = await wm.upload_list([line])
    ...     await wm.run_optimiser(lst.listId)
    ...     for m in await wm.matches(list_id=lst.listId): await wm.accept(m.matchId)
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key
        self._h = Http(base_url, {"X-Api-Key": api_key}, timeout, transport)

    async def __aenter__(self) -> VenueBClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._h.aclose()

    async def entities(self) -> list[dict[str, Any]]:
        """Member entity table (``entity``, ``name``, ``lei``)."""
        return await self._h.json("GET", "/v1/entities")

    # -- interest lists -----------------------------------------------------------------------
    async def upload_list(self, lines: Iterable[ListLine], name: str = "interest list",
                          product: Literal["SBL", "TRS", "REPO"] = "SBL", client_ref: str | None = None,
                          auto_accept: bool = False) -> InterestList:
        body = {"name": name, "product": product, "clientRef": client_ref, "autoAccept": auto_accept,
                "lines": [ln.model_dump() for ln in lines]}
        return InterestList.model_validate(await self._h.json("POST", "/v1/interest-lists", json=body))

    async def upload_csv(self, rows: Iterable[ListLine] | str, name: str = "csv upload",
                         product: str = "SBL", client_ref: str | None = None) -> InterestList:
        """Upload as ``text/csv`` (your rows, or a CSV file's text with any of the accepted headers)."""
        if isinstance(rows, str):
            text = rows
        else:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["instrument", "side", "quantity", "fee_bps", "min_qty"])
            for r in rows:
                w.writerow([r.instrument, r.side, r.quantity, r.feeBps, r.minQuantity])
            text = buf.getvalue()
        params = {k: v for k, v in {"name": name, "product": product, "clientRef": client_ref}.items() if v}
        data = await self._h.json("POST", "/v1/interest-lists", content=text.encode(), params=params,
                                  headers={"content-type": "text/csv"})
        return InterestList.model_validate(data)

    async def lists(self) -> list[InterestList]:
        return [InterestList.model_validate(x) for x in await self._h.json("GET", "/v1/interest-lists")]

    async def list_detail(self, list_id: str) -> dict[str, Any]:
        """The list with every ``lineItems`` entry and its matched quantity."""
        return await self._h.json("GET", f"/v1/interest-lists/{list_id}")

    async def withdraw(self, list_id: str) -> InterestList:
        return InterestList.model_validate(await self._h.json("DELETE", f"/v1/interest-lists/{list_id}"))

    # -- matching and negotiation -------------------------------------------------------------
    async def run_optimiser(self, list_id: str | None = None) -> dict[str, Any]:
        """Match now (it also runs every 5 sim-minutes). Returns counts of matches proposed."""
        return await self._h.json("POST", "/v1/optimiser/run", json={"listId": list_id})

    async def matches(self, status: str | None = None, list_id: str | None = None) -> list[Match]:
        params = {k: v for k, v in {"status": status, "listId": list_id}.items() if v}
        return [Match.model_validate(m) for m in await self._h.json("GET", "/v1/matches", params=params)]

    async def match(self, match_id: str) -> Match:
        return Match.model_validate(await self._h.json("GET", f"/v1/matches/{match_id}"))

    async def propose(self, match_id: str, fee_bps: float, quantity: int | None = None) -> Match:
        body = {"feeBps": fee_bps} | ({"quantity": quantity} if quantity else {})
        return Match.model_validate(await self._h.json("POST", f"/v1/matches/{match_id}/propose", json=body))

    async def counter(self, match_id: str, fee_bps: float, quantity: int | None = None) -> Match:
        body = {"feeBps": fee_bps} | ({"quantity": quantity} if quantity else {})
        return Match.model_validate(await self._h.json("POST", f"/v1/matches/{match_id}/counter", json=body))

    async def accept(self, match_id: str) -> Match:
        """Our side accepts; the match becomes AGREED (and a loan books) once both sides have."""
        return Match.model_validate(await self._h.json("POST", f"/v1/matches/{match_id}/accept"))

    async def reject(self, match_id: str) -> Match:
        return Match.model_validate(await self._h.json("POST", f"/v1/matches/{match_id}/reject"))

    # -- loans --------------------------------------------------------------------------------
    async def loans(self, status: str | None = None) -> list[Loan]:
        params = {"status": status} if status else {}
        return [Loan.model_validate(x) for x in await self._h.json("GET", "/v1/loans", params=params)]

    async def rerate(self, loan_id: str, fee_bps: float) -> Loan:
        """Request a new fee; the counterparty answers asynchronously (see the loan's history)."""
        return Loan.model_validate(await self._h.json("POST", f"/v1/loans/{loan_id}/rerate", json={"feeBps": fee_bps}))

    async def return_shares(self, loan_id: str, quantity: int | None = None) -> Loan:
        body = {"quantity": quantity} if quantity else None
        return Loan.model_validate(await self._h.json("POST", f"/v1/loans/{loan_id}/return", json=body))

    async def recall(self, loan_id: str, quantity: int | None = None) -> Loan:
        body = {"quantity": quantity} if quantity else None
        return Loan.model_validate(await self._h.json("POST", f"/v1/loans/{loan_id}/recall", json=body))

    async def stats(self) -> dict[str, Any]:
        return await self._h.json("GET", "/v1/stats")

    async def stream(self, topics: Iterable[str] = ("lists", "matches", "loans", "optimiser"),
                     ) -> AsyncIterator[dict[str, Any]]:
        """Events: list_uploaded / list_withdrawn, match_proposed / _countered / _updated / _agreed /
        _rejected / _expired, loan_booked / loan_rerate_requested / ..., optimiser_run."""
        async for event in self._h.ws(f"/v1/stream?key={self.api_key}&topics={','.join(topics)}"):
            yield event
