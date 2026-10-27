"""Async REST + WebSocket client for Venue A (see ../../API.md)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx
import websockets

from .models import Availability, Order, OrderRequest, Participant, Print, Trade


class VenueAError(Exception):
    """A request the venue refused. ``code`` is the venue's error code, e.g. ``AGREEMENT_MISMATCH``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class VenueAClient:
    """Participant API: reference data, availability, prints, orders and trades.

    >>> async with VenueAClient("https://ngt.mocks.example.com", participant_key=key) as venue:
    ...     order = await venue.place_order(OrderRequest(...))
    """

    def __init__(self, base_url: str, participant_key: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.participant_key = participant_key
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport,
                                       headers={"X-Participant-Key": participant_key})

    async def __aenter__(self) -> VenueAClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            code = body.get("error") if isinstance(body.get("error"), str) else f"HTTP_{r.status_code}"
            raise VenueAError(r.status_code, code, str(body.get("message") or body.get("detail") or r.text))
        return r.json()

    # -- reference and market data -----------------------------------------------------------
    async def participants(self) -> list[Participant]:
        return [Participant.model_validate(p) for p in await self._call("GET", "/v1/participants")]

    async def availability(self, instrument: str | None = None, lender: str | None = None,
                           min_quantity: int = 0, limit: int = 500, offset: int = 0) -> Availability:
        params = {"instrument": instrument, "lender": lender, "minQuantity": min_quantity, "limit": limit,
                  "offset": offset}
        data = await self._call("GET", "/v1/availability", params={k: v for k, v in params.items() if v is not None})
        return Availability.model_validate(data)

    async def prints(self, instrument: str | None = None, limit: int = 50) -> list[Print]:
        params: dict[str, Any] = {"limit": limit} | ({"instrument": instrument} if instrument else {})
        return [Print.model_validate(p) for p in await self._call("GET", "/v1/prints", params=params)]

    # -- orders -------------------------------------------------------------------------------
    async def place_order(self, req: OrderRequest) -> Order:
        return Order.model_validate(await self._call("POST", "/v1/orders", json=req.model_dump(mode="json")))

    async def order(self, order_id: str) -> Order:
        """By venue order id or by your clientOrderId."""
        return Order.model_validate(await self._call("GET", f"/v1/orders/{order_id}"))

    async def orders(self, status: str | None = None, limit: int = 200) -> list[Order]:
        params: dict[str, Any] = {"limit": limit} | ({"status": status} if status else {})
        return [Order.model_validate(o) for o in await self._call("GET", "/v1/orders", params=params)]

    async def counter(self, order_id: str, fee_bps: float, quantity: int | None = None) -> Order:
        body = {"feeBps": fee_bps} | ({"quantity": quantity} if quantity else {})
        return Order.model_validate(await self._call("POST", f"/v1/orders/{order_id}/counter", json=body))

    async def accept(self, order_id: str) -> Trade:
        """Take the lender's counter as it stands."""
        return Trade.model_validate(await self._call("POST", f"/v1/orders/{order_id}/accept"))

    async def cancel(self, order_id: str) -> Order:
        return Order.model_validate(await self._call("POST", f"/v1/orders/{order_id}/cancel"))

    # -- trades -------------------------------------------------------------------------------
    async def trades(self, since: str | None = None, limit: int = 500) -> list[Trade]:
        params: dict[str, Any] = {"limit": limit} | ({"since": since} if since else {})
        return [Trade.model_validate(t) for t in await self._call("GET", "/v1/trades", params=params)]

    async def trade(self, trade_id_or_uti: str) -> Trade:
        return Trade.model_validate(await self._call("GET", f"/v1/trades/{trade_id_or_uti}"))

    # -- streaming ----------------------------------------------------------------------------
    async def stream(self, topics: Iterable[str] = ("availability", "prints", "orders", "trades"),
                     ) -> AsyncIterator[dict[str, Any]]:
        """Snapshot, then live events: ``{"topic", "type", "topicSeq", "data", ...}``.

        ``topicSeq`` counts each topic's events from 1 without gaps; a jump means events were lost
        (e.g. a chaos drill), so re-read ``/v1/orders`` and ``/v1/trades`` to resynchronise.
        """
        url = self.base_url.replace("http", "ws", 1) + "/v1/stream"
        query = f"?topics={','.join(topics)}&key={self.participant_key}"
        async with websockets.connect(url + query) as ws:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "snapshot_end":
                    yield msg
