"""FastAPI factory shared by every mock.

Each service gets: ``/`` (identity + disclaimer), ``/health``, and an ``/_admin`` surface
(faults, reset, tick, events, clock) that mock-control drives. Middleware pins the sim
time from ``X-Sim-Time`` and applies HTTP faults.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from . import DISCLAIMER, clock, rng
from .db import Database
from .events import EventLog, Hub, make_event_table
from .faults import FaultConfig, Faults
from .sched import Scheduler
from .settings import CoreSettings, settings

log = logging.getLogger(__name__)

SIM_TIME_HEADER = "x-sim-time"
ResetHook = Callable[["ServiceContext"], Awaitable[None]]


@dataclass
class ServiceSpec:
    name: str  # code name, e.g. "mock-ngt"
    label: str  # user-facing generic label, e.g. "Venue A (NGT-style)"
    schema: str  # Postgres schema
    base: Any  # declarative base from mockcore.db.make_base
    description: str = ""
    on_reset: ResetHook | None = None  # repopulate seeded state after truncate
    on_startup: ResetHook | None = None  # start extra servers (e.g. FIX acceptor)
    on_shutdown: ResetHook | None = None
    tags: list[dict[str, str]] = field(default_factory=list)


class ServiceContext:
    def __init__(self, spec: ServiceSpec, cfg: CoreSettings | None = None) -> None:
        self.spec = spec
        self.cfg = cfg or settings()
        self.metadata: MetaData = spec.base.metadata
        self.event_table = make_event_table(self.metadata)
        self.db = Database(self.cfg.database_url, spec.schema, self.metadata)
        self.faults = Faults(spec.name)
        self.hub = Hub(self.faults)
        self.events = EventLog(self.event_table, self.hub)
        self.scheduler = Scheduler()
        self.http = httpx.AsyncClient(timeout=10.0)
        self._tasks: list[asyncio.Task] = []
        self.verbs: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {}
        self.started = False

    def verb(self, name: str) -> Callable:
        """Register a scenario verb, callable by mock-control at ``POST /_admin/scenario/{name}``."""

        def deco(fn: Callable[[dict[str, Any]], Awaitable[Any]]) -> Callable:
            self.verbs[name] = fn
            return fn

        return deco

    def session(self) -> AsyncSession:
        return self.db.session()

    async def get_session(self) -> AsyncIterator[AsyncSession]:
        async with self.db.session() as s:
            yield s

    # -- lifecycle -----------------------------------------------------------------------
    async def start(self, background: bool = True) -> None:
        await clock.ensure_clock_table(self.db.engine)
        await self.db.init()
        clock.clock.state = await clock.load_clock(self.db.engine)
        rng.set_seed(clock.clock.state.seed)
        meta = self.spec.base.ServiceMeta
        async with self.session() as s:
            seeded = await s.get(meta, "seeded")
        if seeded is None or seeded.value.get("seed") != clock.clock.state.seed:
            await self.reset(clock.clock.state.seed)
        else:
            self.scheduler.reset()
        if self.spec.on_startup:
            await self.spec.on_startup(self)
        if background:
            self._tasks.append(asyncio.create_task(self._poll_clock(), name=f"{self.spec.name}-clock"))
            self._tasks.append(
                asyncio.create_task(self.scheduler.loop(self.cfg.scheduler_poll_s), name=f"{self.spec.name}-sched")
            )
        self.started = True

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self.spec.on_shutdown:
            await self.spec.on_shutdown(self)
        await self.http.aclose()
        await self.db.dispose()
        self.started = False

    async def reset(self, seed: int) -> None:
        rng.set_seed(seed)
        self.faults.reset()
        self.hub.seq = 0
        await self.db.truncate()
        self.scheduler.reset()
        if self.spec.on_reset:
            with clock.at(clock.clock.now()):
                await self.spec.on_reset(self)
        meta = self.spec.base.ServiceMeta
        async with self.session() as s:
            await s.merge(meta(key="seeded", value={"seed": seed}))
            await s.commit()

    async def _poll_clock(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.clock_poll_s)
            try:
                state = await clock.load_clock(self.db.engine)
            except Exception:  # pragma: no cover - DB hiccup; keep last known state
                log.warning("clock poll failed", exc_info=True)
                continue
            if state != clock.clock.state:
                clock.clock.state = state

    # -- helpers -------------------------------------------------------------------------
    async def ws_stream(
        self,
        ws: WebSocket,
        topics: list[str] | None,
        snapshot: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        on_message: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """Snapshot-then-deltas WebSocket stream. Subscribes before snapshotting so no gap."""
        await ws.accept()
        sub = self.hub.subscribe(topics)
        try:
            if snapshot:
                for msg in await snapshot():
                    await ws.send_text(json.dumps(msg, default=str))
            await ws.send_text(json.dumps({"type": "snapshot_end", "ts": clock.iso(clock.now())}))

            async def pump_out() -> None:
                while True:
                    msg = await sub.get()
                    await ws.send_text(json.dumps(msg, default=str))

            async def pump_in() -> None:
                while True:
                    raw = await ws.receive_text()
                    if on_message:
                        try:
                            await on_message(json.loads(raw))
                        except Exception as e:  # report but keep the socket
                            await ws.send_text(json.dumps({"type": "error", "error": str(e)}))

            done, pending = await asyncio.wait(
                [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        except WebSocketDisconnect:
            pass
        finally:
            sub.close()


class _SimTimeAndFaults:
    """Pure ASGI middleware (works for HTTP and WebSocket, preserves contextvars)."""

    def __init__(self, app: ASGIApp, ctx: ServiceContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        is_admin = path.startswith("/_admin") or path in ("/health", "/", "/docs", "/openapi.json")
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = None
        if SIM_TIME_HEADER in headers:
            with contextlib.suppress(ValueError):
                token = clock.pin(clock.parse_ts(headers[SIM_TIME_HEADER]))
        try:
            if not is_admin and scope["type"] == "http":
                faults = self.ctx.faults
                if faults.config.outage:
                    await _plain(send, 503, {"error": "service unavailable (simulated outage)"})
                    return
                delay = faults.http_delay_s()
                if delay:
                    await asyncio.sleep(delay)
                if faults.http_error():
                    await _plain(send, 500, {"error": "simulated internal error"})
                    return
            elif not is_admin and scope["type"] == "websocket" and self.ctx.faults.config.outage:
                await send({"type": "websocket.close", "code": 1013})
                return
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                clock.unpin(token)


async def _plain(send: Send, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    await send(
        {"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]}
    )
    await send({"type": "http.response.body", "body": data})


class ResetRequest(BaseModel):
    seed: int | None = None


def create_app(ctx: ServiceContext) -> FastAPI:
    spec = ctx.spec

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await ctx.start()
        try:
            yield
        finally:
            await ctx.stop()

    app = FastAPI(
        title=f"{spec.label} — {spec.name}",
        description=f"{spec.description}\n\n**{DISCLAIMER}**",
        version="0.1.0",
        lifespan=lifespan,
        openapi_tags=spec.tags or None,
    )
    app.add_middleware(_SimTimeAndFaults, ctx=ctx)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.ctx = ctx

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if ctx.cfg.admin_token and x_admin_token != ctx.cfg.admin_token:
            raise HTTPException(401, "admin token required")

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, Any]:
        return {"service": spec.name, "label": spec.label, "description": spec.description, "disclaimer": DISCLAIMER}

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": spec.name, "simTime": clock.iso(clock.clock.live_now())}

    admin = APIRouter(prefix="/_admin", tags=["admin"], dependencies=[Depends(require_admin)])

    @admin.get("/faults")
    async def get_faults() -> dict[str, Any]:
        return ctx.faults.to_json()

    @admin.put("/faults")
    async def put_faults(cfg: FaultConfig) -> dict[str, Any]:
        ctx.faults.config = cfg
        return ctx.faults.to_json()

    @admin.post("/reset")
    async def reset(req: ResetRequest | None = None) -> dict[str, Any]:
        clock.clock.state = await clock.load_clock(ctx.db.engine)
        seed = req.seed if req and req.seed is not None else clock.clock.state.seed
        await ctx.reset(seed)
        return {"ok": True, "seed": seed}

    @admin.post("/tick")
    async def tick(until: str | None = Query(None, description="Run jobs due up to this sim time")) -> dict:
        clock.clock.state = await clock.load_clock(ctx.db.engine)
        target: datetime = clock.parse_ts(until) if until else clock.clock.live_now()
        ran = await ctx.scheduler.run_due(target)
        return {"ran": ran, "until": clock.iso(target)}

    @admin.get("/jobs")
    async def jobs() -> list[dict[str, Any]]:
        return ctx.scheduler.pending()

    @admin.get("/events")
    async def events(after: int = 0, limit: int = Query(500, le=5000)) -> list[dict[str, Any]]:
        async with ctx.session() as s:
            return await ctx.events.list(s, after, limit)

    @admin.get("/clock")
    async def get_clock() -> dict[str, Any]:
        return clock.clock.state.to_json()

    @admin.get("/scenario")
    async def list_verbs() -> dict[str, Any]:
        return {name: (fn.__doc__ or "").strip() for name, fn in ctx.verbs.items()}

    @admin.post("/scenario/{verb}")
    async def run_verb(verb: str, args: dict[str, Any] | None = None) -> Any:
        fn = ctx.verbs.get(verb)
        if fn is None:
            raise HTTPException(404, f"unknown scenario verb {verb!r}; have {sorted(ctx.verbs)}")
        return await fn(args or {})

    app.include_router(admin)
    return app


async def count(session: AsyncSession, stmt: Any) -> int:
    from sqlalchemy import func

    return (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
