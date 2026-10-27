"""HTTP plumbing shared by this connector's clients."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets


class ApiError(Exception):
    """A request the system refused. ``code`` is its error code when it sent one, else ``HTTP_<status>``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class Http:
    def __init__(self, base_url: str, headers: dict[str, str], timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None, retry_429: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.retry_429 = retry_429
        self.client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def raw(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(self.retry_429 + 1):
            r = await self.client.request(method, path, **kw)
            if r.status_code != 429 or attempt == self.retry_429:
                break
            await asyncio.sleep(float(r.headers.get("retry-after", "1")))  # honour the server's rate limit
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {}
            code = body.get("error") if isinstance(body.get("error"), str) else f"HTTP_{r.status_code}"
            raise ApiError(r.status_code, code, str(body.get("message") or body.get("detail") or r.text)[:500])
        return r

    async def json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.raw(method, path, **kw)
        return r.json() if r.content else None

    async def ws(self, path: str) -> AsyncIterator[dict[str, Any]]:
        """Yield every event (each has ``topic`` and a gap-free per-topic ``topicSeq``); skips the
        server's ``snapshot_end`` marker."""
        async with websockets.connect(self.base_url.replace("http", "ws", 1) + path) as sock:
            async for raw in sock:
                msg = json.loads(raw)
                if msg.get("type") != "snapshot_end":
                    yield msg
