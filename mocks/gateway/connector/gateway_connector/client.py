"""Clients for the Counterparty Gateway (see ../../API.md).

* ``GatewayPlatformClient``: the LoanDesk side. Pull or stream incoming locates, post decisions, read the
  stock record (client short positions and the short sales done against each locate).
* ``CounterpartyClient``: what a prime broker or fund uses to send us locates and orders.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator, Iterable
from datetime import date, datetime
from typing import Any, Literal

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from ._http import Http


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class LocateLine(_Model):
    lineNo: int
    identifier: str
    idType: str | None = None
    isin: str | None = None
    quantity: int
    maxFeeBps: float | None = None  # the client's limit, if it sent one
    status: Literal["RECEIVED", "INVALID", "APPROVED", "PARTIAL", "REJECTED"]
    approvedQuantity: int | None = None
    feeBps: float | None = None  # the agreed fee
    rationale: str | None = None
    regShoBasis: str | None = None
    enteredAt: str | None = None
    expiresAt: str | None = None
    usedQuantity: int | None = None  # set at the close it expired into
    error: str | None = None


class LocateRequest(_Model):
    locateId: str
    requestId: str | None = None
    counterparty: str
    format: Literal["CDM", "FLAT", "CSV"]
    settlementDate: str | None = None
    callbackUrl: str | None = None
    status: Literal["RECEIVED", "PARTIALLY_DECIDED", "DECIDED"]
    receivedAt: str
    lines: list[LocateLine]
    seq: int | None = None  # inbox cursor


class Decision(_Model):
    """Our answer to one locate line."""

    lineNo: int
    decision: Literal["APPROVED", "PARTIAL", "REJECTED"]
    approvedQuantity: int = Field(0, ge=0, description="0 with APPROVED means the full requested quantity")
    feeBps: float | None = Field(None, validation_alias=AliasChoices("feeBps", "indicativeFeeBps"),
                                 description="The agreed fee the client pays on what it shorts against the locate")
    rationale: str | None = None
    regShoBasis: str | None = Field(None, description="Reg SHO 203(b) reasonable-grounds basis for an approval")
    expiresAt: datetime | None = Field(None, description="Default: today's close, or the next business day's")


class Receipt(_Model):
    locateId: str
    requestId: str | None = None
    status: str
    lines: int
    invalidLines: list[dict[str, Any]] = []
    idempotentReplay: bool = False


class GatewayPlatformClient:
    """>>> async with GatewayPlatformClient(url, platform_token=tok) as gw:
    ...     for req in await gw.inbox():
    ...         await gw.decide(req.locateId, [Decision(lineNo=ln.lineNo, decision="APPROVED") for ln in req.lines])
    """

    def __init__(self, base_url: str, platform_token: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.platform_token = platform_token
        self._h = Http(base_url, {"Authorization": f"Bearer {platform_token}"}, timeout, transport)

    async def __aenter__(self) -> GatewayPlatformClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._h.aclose()

    async def inbox(self, status: str = "RECEIVED", after: int = 0, limit: int = 200) -> list[LocateRequest]:
        """Locate requests in ``status``; page with ``after`` = the last request's ``seq``."""
        params = {"status": status, "after": after, "limit": limit}
        return [LocateRequest.model_validate(r) for r in await self._h.json("GET", "/v1/inbox", params=params)]

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Live ``locate_received`` / ``order_received`` (topic inbox) and ``locate_decided`` (topic decisions)."""
        async for event in self._h.ws(f"/v1/inbox/stream?token={self.platform_token}"):
            yield event

    async def decide(self, locate_id: str, lines: Iterable[Decision]) -> dict[str, Any]:
        """Answer some or all lines. The gateway sends the result to the counterparty's callback
        as a signed webhook (retried with backoff); see ``deliveries``."""
        body = {"locateId": locate_id, "lines": [d.model_dump(mode="json", exclude_none=True) for d in lines]}
        return await self._h.json("POST", "/v1/decisions", json=body)

    async def deliveries(self, locate_id: str | None = None) -> list[dict[str, Any]]:
        """Webhook delivery attempts: ``url``, ``attempt``, ``statusCode``, ``ok``, ``error``."""
        params = {"locateId": locate_id} if locate_id else {}
        return await self._h.json("GET", "/v1/deliveries", params=params)

    async def stock_record(self, trade_date: date | None = None) -> dict[str, Any]:
        """Client short positions at a close (default the latest), settling the next business day, with
        ``shortSales`` and ``locateUsage`` (how much of each locate expiring then was used)."""
        params = {"date": trade_date.isoformat()} if trade_date else {}
        return await self._h.json("GET", "/v1/stock-record", params=params)

    async def fails(self, after: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        """Client shorts that failed to deliver at CNS, oldest first (``after``: the last ``seq`` seen)."""
        return await self._h.json("GET", "/v1/fails", params={"after": after, **({"status": status} if status else {})})

    async def buy_ins(self, after: int = 0) -> list[dict[str, Any]]:
        """CNS buy-ins on fails we didn't deliver, oldest first (``after``: the last ``seq`` seen)."""
        return await self._h.json("GET", "/v1/buy-ins", params={"after": after})

    async def charge(self, counterparty: str, amount: float, reason: str, ref: str, ccy: str = "USD") -> dict[str, Any]:
        """Post a charge to a client's account; idempotent on ``ref``."""
        return await self._h.json("POST", "/v1/charges", json={"counterparty": counterparty, "amount": amount,
                                                              "ccy": ccy, "reason": reason, "ref": ref})

    async def deliver_fail(self, fail_id: str, quantity: int, ref: str | None = None) -> dict[str, Any]:
        """Deliver shares against a fail; it closes when nothing is outstanding."""
        return await self._h.json("POST", f"/v1/fails/{fail_id}/deliver", json={"quantity": quantity, "ref": ref})


class CounterpartyClient:
    """The counterparty side. Every submission carries an ``Idempotency-Key`` (generated unless you
    pass one), so a retry after a timeout never creates a second request; HTTP 429 is retried
    after ``Retry-After``.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._h = Http(base_url, {"Authorization": f"Bearer {api_key}"}, timeout, transport)

    async def __aenter__(self) -> CounterpartyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._h.aclose()

    async def submit_locates(self, lines: Iterable[tuple[str, int] | tuple[str, int, float]],
                             request_id: str | None = None,
                             settlement_date: date | None = None, callback_url: str | None = None,
                             fmt: Literal["CDM", "FLAT", "CSV"] = "CDM", idempotency_key: str | None = None,
                             ) -> Receipt:
        """``lines`` are ``(identifier, quantity)`` or ``(identifier, quantity, maxFeeBps)``; identifiers may be
        ISIN, CUSIP, SEDOL or ticker. ``maxFeeBps`` is the most the client will pay to short the name."""
        items = [(x[0], x[1], x[2] if len(x) > 2 else None) for x in lines]
        limits = any(f is not None for _, _, f in items)
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        sd = settlement_date.isoformat() if settlement_date else None
        if fmt == "CSV":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["identifier", "quantity", "maxFeeBps"] if limits else ["identifier", "quantity"])
            w.writerows([(i, q, "" if f is None else f) if limits else (i, q) for i, q, f in items])
            params = {k: v for k, v in {"requestId": request_id, "settlementDate": sd,
                                        "callbackUrl": callback_url}.items() if v}
            data = await self._h.json("POST", "/v1/locates", content=buf.getvalue().encode(), params=params,
                                      headers={**headers, "content-type": "text/csv"})
        elif fmt == "CDM":
            doc: dict[str, Any] = {"borrowerLocate": {
                "locateRequestId": request_id, "settlementDate": sd, "callbackUrl": callback_url,
                "locateLines": [{"security": {"identifier": i}, "quantity": {"amount": q, "unit": "SHARES"},
                                 **({"maxFee": {"bps": f}} if f is not None else {})} for i, q, f in items]}}
            data = await self._h.json("POST", "/v1/locates", json=doc, headers=headers)
        else:
            doc = {"requestId": request_id, "settlementDate": sd, "callbackUrl": callback_url,
                   "lines": [{"identifier": i, "quantity": q, **({"maxFeeBps": f} if f is not None else {})}
                             for i, q, f in items]}
            data = await self._h.json("POST", "/v1/locates", json=doc, headers=headers)
        return Receipt.model_validate(data)

    async def locate(self, locate_id: str) -> LocateRequest:
        return LocateRequest.model_validate(await self._h.json("GET", f"/v1/locates/{locate_id}"))

    async def locates(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._h.json("GET", "/v1/locates", params={"limit": limit})

    async def submit_order(self, client_order_id: str, side: Literal["BORROW", "LOAN", "RETURN", "RECALL"],
                           identifier: str, quantity: int, fee_bps: float | None = None,
                           settlement_date: date | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        body = {"clientOrderId": client_order_id, "side": side, "identifier": identifier, "quantity": quantity,
                "feeBps": fee_bps, "settlementDate": settlement_date.isoformat() if settlement_date else None}
        return await self._h.json("POST", "/v1/orders", json={k: v for k, v in body.items() if v is not None},
                                  headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())})
