"""Async client for the custodian's ISO 20022 endpoints and status stream (see ../../API.md)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets

from .iso import SettlementInstruction, StatusAdvice, build_sese020, build_sese023, parse


class CustodianRejected(Exception):
    """The custodian refused a message: schema failure (400), unknown ISIN (422) or a rejected
    cancellation (409). ``advice`` is its sese.024 / sese.027, when it sent one."""

    def __init__(self, status: int, advice: StatusAdvice | None, errors: str | None = None) -> None:
        reason = advice.detail if advice else None
        super().__init__(f"{status}: {errors or reason or 'rejected'}")
        self.status, self.advice, self.errors = status, advice, errors


class CustodianClient:
    """Instruct settlements, cancel them, and follow their status.

    >>> async with CustodianClient(url, api_key=key, our_bic="LDSKUS33XXX") as cust:
    ...     ack = await cust.instruct(SettlementInstruction(...))   # sese.023 -> sese.024 PACK
    ...     async for advice in cust.stream(): ...                  # MACH, PENF, SETT (sese.025)
    """

    def __init__(self, base_url: str, api_key: str, our_bic: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key, self.our_bic = api_key, our_bic
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport,
                                       headers={"X-Api-Key": api_key})

    async def __aenter__(self) -> CustodianClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post_xml(self, path: str, xml: bytes) -> tuple[StatusAdvice, str | None]:
        r = await self._http.post(path, content=xml, headers={"content-type": "application/xml"})
        advice = parse(r.content) if r.content.lstrip().startswith(b"<") else None
        if r.status_code >= 400 or advice is None:
            raise CustodianRejected(r.status_code, advice, r.headers.get("x-validation-errors"))
        return advice, r.headers.get("x-instruction-id")

    async def instruct(self, instruction: SettlementInstruction) -> StatusAdvice:
        """Send a sese.023; returns the custodian's sese.024 acceptance (PACK), whose
        ``custodian_ref`` is the instruction id."""
        if instruction.our_bic != self.our_bic:
            raise ValueError("instruction.our_bic must be this client's BIC")
        advice, _ = await self._post_xml("/iso/sese.023", build_sese023(instruction))
        return advice

    async def cancel(self, tx_id: str, custodian_ref: str | None = None) -> StatusAdvice:
        """Send a sese.020; returns the sese.027 (ACCP). A sese.024 CAND follows on the stream."""
        advice, _ = await self._post_xml("/iso/sese.020", build_sese020(tx_id, self.our_bic, custodian_ref))
        return advice

    async def outbox(self, after: int = 0, limit: int = 200) -> list[tuple[int, StatusAdvice]]:
        """Every message the custodian has sent you, oldest first, as ``(out_id, advice)``. Poll with
        ``after`` = the last ``out_id`` you processed."""
        r = await self._http.get("/iso/outbox", params={"after": after, "limit": limit})
        r.raise_for_status()
        return [(m["outId"], parse(m["xml"])) for m in r.json()]

    async def instruction(self, ref: str) -> dict[str, Any]:
        """Current state by your tx_id or the custodian's id (JSON view, with history)."""
        r = await self._http.get(f"/instructions/{ref}")
        r.raise_for_status()
        return r.json()

    async def stream(self) -> AsyncIterator[StatusAdvice]:
        """Live sese.024 / sese.025 / sese.027 messages as the custodian sends them."""
        url = self.base_url.replace("http", "ws", 1) + f"/stream?key={self.api_key}"
        async with websockets.connect(url) as ws:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") in ("snapshot_end", "error"):
                    continue
                yield parse(msg["data"]["xml"])
