"""Adapters to the outside world: one client per external system, each with its own credentials.

The mocks stand in for real vendors, so each adapter only speaks HTTP and could later point at a
real endpoint. In local dev an adapter with no configured key asks the mock's ``/_admin/credentials``.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..settings import Settings, get_settings


class AdapterError(Exception):
    def __init__(self, system: str, status: int | None, message: str, body: Any = None) -> None:
        super().__init__(f"{system}: {message}")
        self.system, self.status, self.body = system, status, body


class Client:
    system: str = "?"
    label: str = "?"

    def __init__(self, base_url: str, key: str | None, cfg: Settings) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = key
        self._cfg = cfg
        self._http = httpx.AsyncClient(timeout=10)

    # -- credentials ------------------------------------------------------------------------
    def headers_for(self, key: str) -> dict[str, str]:
        return {"authorization": f"Bearer {key}"}

    def key_from_credentials(self, cred: dict[str, Any]) -> str:
        raise NotImplementedError

    async def key(self) -> str:
        if not self._key:
            admin = {"x-admin-token": self._cfg.mock_admin_token} if self._cfg.mock_admin_token else {}
            try:
                r = await self._http.get(f"{self.base_url}/_admin/credentials", headers=admin)
            except httpx.HTTPError as e:
                raise AdapterError(self.system, None, f"unreachable ({type(e).__name__})") from e
            if r.status_code >= 300:
                raise AdapterError(self.system, r.status_code, "no key configured and credentials not available")
            self._key = self.key_from_credentials(r.json())
        return self._key

    # -- requests -----------------------------------------------------------------------------
    async def request(self, method: str, path: str, **kw: Any) -> Any:
        headers = {**self.headers_for(await self.key()), **(kw.pop("headers", None) or {})}
        try:
            r = await self._http.request(method, f"{self.base_url}{path}", headers=headers, **kw)
        except httpx.HTTPError as e:
            raise AdapterError(self.system, None, f"unreachable ({type(e).__name__})") from e
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            detail = body.get("message") or body.get("detail") or body.get("error") if isinstance(body, dict) else body
            raise AdapterError(self.system, r.status_code, str(detail)[:300], body)
        return r.json() if r.content else None

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def health(self) -> bool:
        try:
            return (await self._http.get(f"{self.base_url}/health", timeout=2)).status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._http.aclose()


class Control(Client):
    system, label = "control", "Simulation Control"

    def key_from_credentials(self, cred: dict[str, Any]) -> str:
        return cred["observerKey"]

    async def clock(self) -> dict[str, Any]:
        return await self.get("/clock")


class Systems:
    """Every adapter, built once per process."""

    def __init__(self, cfg: Settings | None = None) -> None:
        from .chat import DeskChat
        from .custodian import Custodian
        from .gateway import Gateway
        from .processor import Processor
        from .venue_a import VenueA
        from .venue_b import VenueB

        cfg = cfg or get_settings()
        self.control = Control(cfg.control_url, cfg.control_key, cfg)
        self.chat = DeskChat(cfg.ib_url, cfg.ib_token, cfg)
        self.gateway = Gateway(cfg.gateway_url, cfg.gateway_token, cfg)
        self.venue_a = VenueA(cfg.ngt_url, cfg.ngt_key, cfg)
        self.venue_b = VenueB(cfg.wematch_url, cfg.wematch_key, cfg)
        self.processor = Processor(cfg.sfp_url, cfg.sfp_key, cfg)
        self.custodian = Custodian(cfg.custodian_url, cfg.custodian_key, cfg)

    def all(self) -> list[Client]:
        return [self.control, self.chat, self.gateway, self.venue_a, self.venue_b, self.processor,
                self.custodian]

    async def status(self) -> list[dict[str, Any]]:
        return [{"system": c.system, "label": c.label, "url": c.base_url, "up": await c.health()} for c in self.all()]

    async def close(self) -> None:
        for c in self.all():
            await c.close()


_systems: Systems | None = None


def systems() -> Systems:
    global _systems
    if _systems is None:
        _systems = Systems()
    return _systems


def set_systems(s: Systems | None) -> None:
    global _systems
    _systems = s
