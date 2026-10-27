"""Simulation Control client (see ../../API.md): the operator's handle on the whole simulation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import httpx

from ._http import Http


def _ts(v: datetime | str) -> str:
    return v.isoformat() if isinstance(v, datetime) else v


class SimControl:
    """Clock, seeded resets, fault injection and scenario runs.

    >>> async with SimControl("https://control.mocks.example.com") as sim:
    ...     await sim.reset(seed=20261027, mode="stepped")
    ...     run = await sim.run_scenario("demo", mode="stepped", wait=True)
    ...     await sim.set_fault("ngt", outage=True)
    """

    def __init__(self, base_url: str, admin_token: str | None = None, timeout: float = 120.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._h = Http(base_url, {"X-Admin-Token": admin_token} if admin_token else {}, timeout, transport)

    async def __aenter__(self) -> SimControl:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._h.aclose()

    # -- clock --------------------------------------------------------------------------------
    async def clock(self) -> dict[str, Any]:
        """``now``, ``now_local`` (New York), ``mode``, ``speed``, ``paused``, ``seed``."""
        return await self._h.json("GET", "/clock")

    async def set_clock(self, speed: float | None = None, paused: bool | None = None,
                        mode: Literal["live", "stepped"] | None = None) -> dict[str, Any]:
        body = {k: v for k, v in {"speed": speed, "paused": paused, "mode": mode}.items() if v is not None}
        return await self._h.json("PUT", "/clock", json=body)

    async def start(self) -> dict[str, Any]:
        return await self._h.json("POST", "/clock/start")

    async def pause(self) -> dict[str, Any]:
        return await self._h.json("POST", "/clock/pause")

    async def jump(self, to: datetime | str) -> dict[str, Any]:
        return await self._h.json("POST", "/clock/jump", json={"to": _ts(to)})

    async def advance(self, seconds: float | None = None, to: datetime | str | None = None) -> dict[str, Any]:
        """Stepped mode: move time forward and run every system's due jobs up to it."""
        body = {"seconds": seconds} if seconds is not None else {"to": _ts(to)} if to is not None else None
        if body is None:
            raise ValueError("give seconds or to")
        return await self._h.json("POST", "/clock/advance", json=body)

    async def reset(self, seed: int | None = None, epoch: datetime | str | None = None,
                    mode: Literal["live", "stepped"] = "live", speed: float = 1.0,
                    paused: bool = True) -> dict[str, Any]:
        """Truncate and reseed every system. Same seed, same IDs, keys and data."""
        body = {"seed": seed, "epoch": _ts(epoch) if epoch else None, "mode": mode, "speed": speed, "paused": paused}
        return await self._h.json("POST", "/reset", json={k: v for k, v in body.items() if v is not None})

    # -- systems and faults -------------------------------------------------------------------
    async def services(self) -> dict[str, Any]:
        return await self._h.json("GET", "/services")

    async def faults(self) -> dict[str, Any]:
        return await self._h.json("GET", "/faults")

    async def set_fault(self, service: str, **fault: Any) -> dict[str, Any]:
        """``latency_ms``, ``jitter_ms``, ``error_pct``, ``outage``, ``drop_pct``, ``dup_pct``, ``reorder_pct``."""
        return await self._h.json("PUT", f"/faults/{service}", json=fault)

    async def clear_faults(self) -> dict[str, Any]:
        return await self._h.json("DELETE", "/faults")

    # -- scenarios ----------------------------------------------------------------------------
    async def scenarios(self) -> list[dict[str, Any]]:
        return await self._h.json("GET", "/scenarios")

    async def scenario(self, name: str) -> dict[str, Any]:
        return await self._h.json("GET", f"/scenarios/{name}")

    async def run_scenario(self, name: str, mode: Literal["live", "stepped"] = "stepped",
                           speed: float | None = None, seed: int | None = None, reset: bool = True,
                           wait: bool = False) -> dict[str, Any]:
        body = {k: v for k, v in {"mode": mode, "speed": speed, "seed": seed, "reset": reset}.items() if v is not None}
        return await self._h.json("POST", f"/scenarios/{name}/run", json=body, params={"wait": str(wait).lower()})

    async def runs(self) -> list[dict[str, Any]]:
        return await self._h.json("GET", "/runs")

    async def run(self, run_id: str) -> dict[str, Any]:
        return await self._h.json("GET", f"/runs/{run_id}")

    async def stop_run(self, run_id: str) -> dict[str, Any]:
        return await self._h.json("POST", f"/runs/{run_id}/stop")

    # -- per-system simulator controls --------------------------------------------------------
    async def credentials(self, system_url: str) -> dict[str, Any]:
        """A system's seed-derived credentials (its ``/_admin/credentials``), with this client's token."""
        r = await self._h.client.get(system_url.rstrip("/") + "/_admin/credentials")
        r.raise_for_status()
        return r.json()
