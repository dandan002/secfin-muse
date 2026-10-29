"""FastAPI factory shared by every mock.

Each service gets: ``/`` (identity + disclaimer), ``/health``, and an ``/_admin`` surface
(faults, reset, tick, events, clock) that mock-control drives. Middleware pins the sim
time from ``X-Sim-Time`` and applies HTTP faults.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from . import DISCLAIMER, clock, rng
from .db import Database
from .events import EventLog, Hub, make_event_table
from .faults import FaultConfig, Faults
from .keys import AuthSpec, KeyRing, Plan, Principal, make_key_table
from .sched import Scheduler
from .settings import CoreSettings, settings

log = logging.getLogger(__name__)

SIM_TIME_HEADER = "x-sim-time"
ADMIN_TOKEN_HEADER = "x-admin-token"
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
    # Only mock-control owns the clock table; every other mock gets the clock pushed to it.
    owns_clock: bool = False
    # API keys, scopes and plans for the business API (mockcore.keys); None leaves it open.
    auth: AuthSpec | None = None


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
        self.keys: KeyRing | None = None
        if spec.auth:
            self.keys = KeyRing(spec.name, spec.auth, make_key_table(self.metadata), self.cfg.key_secret,
                                rng.get_seed)
        self.scheduler = Scheduler()
        self.http = httpx.AsyncClient(timeout=10.0)
        self._tasks: list[asyncio.Task] = []
        self.verbs: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {}
        self.clock_rev = 0  # revision of the last clock push accepted (non-owners)
        self.started = False

    def verb(self, name: str) -> Callable:
        """Register a scenario verb, callable by mock-control at ``POST /_admin/scenario/{name}``."""

        def deco(fn: Callable[[dict[str, Any]], Awaitable[Any]]) -> Callable:
            self.verbs[name] = fn
            return fn

        return deco

    def session(self) -> AsyncSession:
        return self.db.session()

    @property
    def gate(self) -> KeyRing:
        """The service's key ring; only services whose spec declares ``auth`` have one."""
        if self.keys is None:
            raise RuntimeError(f"{self.spec.name} declares no auth")
        return self.keys

    def require(self, *scopes: str) -> Callable[..., Any]:
        """FastAPI dependency: a valid API key entitled to ``scopes`` (see mockcore.keys)."""
        return self.gate.require(*scopes)

    async def get_session(self) -> AsyncIterator[AsyncSession]:
        async with self.db.session() as s:
            yield s

    # -- lifecycle -----------------------------------------------------------------------
    async def start(self, background: bool = True) -> None:
        if self.spec.owns_clock:
            await clock.ensure_clock_table(self.db.engine)
        await self.db.init()
        await self.refresh_clock()
        if not self.spec.owns_clock:
            await self._load_pushed_clock()
        rng.set_seed(clock.clock.state.seed)
        meta = self.spec.base.ServiceMeta
        async with self.session() as s:
            seeded = await s.get(meta, "seeded")
        if seeded is None or seeded.value.get("seed") != clock.clock.state.seed:
            await self.reset(clock.clock.state.seed)
        else:
            self.scheduler.reset()
            if self.keys:
                await self.keys.load(self.db)
        if self.spec.on_startup:
            await self.spec.on_startup(self)
        if background:
            if self.spec.owns_clock:
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
        self.hub.topic_seq.clear()
        await self.db.truncate()
        self.scheduler.reset()
        if self.spec.on_reset:
            with clock.at(clock.clock.now()):
                await self.spec.on_reset(self)
        meta = self.spec.base.ServiceMeta
        async with self.session() as s:
            await s.merge(meta(key="seeded", value={"seed": seed}))
            if not self.spec.owns_clock:  # truncate wiped the stored clock; keep it
                await s.merge(meta(key="clock", value=self._clock_record()))
            await s.commit()
        if self.keys:  # a new seed means new seed-derived bootstrap keys (none change with MOCK_KEY_SECRET)
            await self.keys.load(self.db)

    # -- clock ---------------------------------------------------------------------------
    async def refresh_clock(self) -> None:
        """The owner re-reads its clock table; everyone else already holds the pushed state."""
        if self.spec.owns_clock:
            clock.clock.state = await clock.load_clock(self.db.engine)

    def _clock_record(self) -> dict[str, Any]:
        return {"state": clock.clock.state.to_json(), "rev": self.clock_rev}

    async def _load_pushed_clock(self) -> None:
        async with self.session() as s:
            row = await s.get(self.spec.base.ServiceMeta, "clock")
        if row is None:  # never heard from mock-control: paused at the default epoch
            clock.clock.state = clock.ClockState(wall_anchor=time.time(), seed=self.cfg.seed)
        else:
            clock.clock.state = clock.ClockState.from_json(row.value["state"])
            self.clock_rev = int(row.value.get("rev", 0))

    async def accept_clock(self, state: clock.ClockState, rev: int) -> bool:
        """Apply a pushed clock unless a newer one already arrived (pushes can overtake)."""
        if rev < self.clock_rev:
            return False
        clock.clock.state, self.clock_rev = state, rev
        async with self.session() as s:
            await s.merge(self.spec.base.ServiceMeta(key="clock", value=self._clock_record()))
            await s.commit()
        return True

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
        # Guard every /_admin route here, including ones a service registers on its own app.
        if path.startswith("/_admin") and not _admin_ok(self.ctx.cfg.admin_token, headers.get(ADMIN_TOKEN_HEADER)):
            if scope["type"] == "http":
                await _plain(send, 401, {"detail": "admin token required"})
            else:
                await send({"type": "websocket.close", "code": 4401})
            return
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


def _admin_ok(expected: str, given: str | None) -> bool:
    return not expected or (given is not None and hmac.compare_digest(given, expected))


async def _plain(send: Send, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    await send(
        {"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]}
    )
    await send({"type": "http.response.body", "body": data})


class ResetRequest(BaseModel):
    seed: int | None = None


class KeyIn(BaseModel):
    principal: str
    scopes: list[str]
    plan: str = "standard"
    label: str = ""
    ttlDays: float | None = None


class KeyChange(BaseModel):
    scopes: list[str] | None = None
    plan: str | None = None


class RotateIn(BaseModel):
    graceSeconds: int = 3600


class PlanIn(BaseModel):
    ratePerSecond: float = Field(gt=0)
    burst: float = Field(ge=1)
    dailyQuota: int | None = Field(default=None, ge=1)
    description: str = ""


class ClockPush(BaseModel):
    state: dict[str, Any]
    rev: int


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

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, Any]:
        return {"service": spec.name, "label": spec.label, "description": spec.description, "disclaimer": DISCLAIMER}

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": spec.name, "simTime": clock.iso(clock.clock.live_now())}

    admin = APIRouter(prefix="/_admin", tags=["admin"])  # token checked in _SimTimeAndFaults

    @admin.get("/faults")
    async def get_faults() -> dict[str, Any]:
        return ctx.faults.to_json()

    @admin.put("/faults")
    async def put_faults(cfg: FaultConfig) -> dict[str, Any]:
        ctx.faults.config = cfg
        return ctx.faults.to_json()

    @admin.post("/reset")
    async def reset(req: ResetRequest | None = None) -> dict[str, Any]:
        await ctx.refresh_clock()
        seed = req.seed if req and req.seed is not None else clock.clock.state.seed
        await ctx.reset(seed)
        return {"ok": True, "seed": seed}

    @admin.post("/tick")
    async def tick(until: str | None = Query(None, description="Run jobs due up to this sim time")) -> dict:
        await ctx.refresh_clock()
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

    @admin.put("/clock")
    async def put_clock(push: ClockPush) -> dict[str, Any]:
        """mock-control pushes the sim clock here; the mocks never read each other's databases."""
        if spec.owns_clock:
            raise HTTPException(409, "this service owns the clock; use PUT /clock")
        accepted = await ctx.accept_clock(clock.ClockState.from_json(push.state), push.rev)
        return {"accepted": accepted, "rev": ctx.clock_rev, **clock.clock.state.to_json()}

    @admin.get("/scenario")
    async def list_verbs() -> dict[str, Any]:
        return {name: (fn.__doc__ or "").strip() for name, fn in ctx.verbs.items()}

    @admin.post("/scenario/{verb}")
    async def run_verb(verb: str, args: dict[str, Any] | None = None) -> Any:
        fn = ctx.verbs.get(verb)
        if fn is None:
            raise HTTPException(404, f"unknown scenario verb {verb!r}; have {sorted(ctx.verbs)}")
        return await fn(args or {})

    if ctx.keys:
        _key_routes(app, admin, ctx.keys)
    app.include_router(admin)
    return app


def _key_routes(app: FastAPI, admin: APIRouter, ring: KeyRing) -> None:
    """Key management on ``/_admin/keys`` (admin token), and ``GET /whoami`` for any valid key."""

    @admin.get("/keys")
    async def list_keys() -> dict[str, Any]:
        return {**ring.catalog(), "keys": ring.list()}

    @admin.post("/keys", status_code=201)
    async def issue_key(body: KeyIn) -> dict[str, Any]:
        """Issue a random key. The plaintext is in this response only; it is never stored."""
        ttl = timedelta(days=body.ttlDays) if body.ttlDays else None
        key, row = await ring.issue(body.principal, body.scopes, body.plan, body.label, ttl)
        return {"apiKey": key, **ring.describe(row)}

    @admin.patch("/keys/{key_id}")
    async def change_key(key_id: str, body: KeyChange) -> dict[str, Any]:
        """Change a key's entitlements (scopes) or plan; takes effect on its next call."""
        return ring.describe(await ring.change(key_id, body.scopes, body.plan))

    @admin.post("/keys/{key_id}/revoke")
    async def revoke_key(key_id: str) -> dict[str, Any]:
        return ring.describe(await ring.revoke(key_id))

    @admin.post("/keys/{key_id}/rotate", status_code=201)
    async def rotate_key(key_id: str, body: RotateIn | None = None) -> dict[str, Any]:
        """A new key with the same entitlements; the old one expires after ``graceSeconds``."""
        grace = timedelta(seconds=(body or RotateIn()).graceSeconds)
        key, new, old = await ring.rotate(key_id, grace)
        return {"apiKey": key, **ring.describe(new), "previous": ring.describe(old)}

    @admin.put("/plans/{name}")
    async def put_plan(name: str, plan: PlanIn) -> dict[str, Any]:
        """Change a plan's limits for every key on it (until restart, or DELETE /_admin/plans)."""
        ring.set_plan(Plan(name, plan.ratePerSecond, plan.burst, plan.dailyQuota, plan.description))
        return ring.catalog()

    @admin.delete("/plans")
    async def reset_plans() -> dict[str, Any]:
        ring.reset_plans()
        return ring.catalog()

    @app.get("/whoami", tags=["meta"])
    async def whoami(principal: Principal = Depends(ring.require())) -> dict[str, Any]:
        """Who this key belongs to, what it's entitled to, and how much of its plan is left."""
        row = next(k for k in ring.list() if k["keyId"] == principal.key_id)
        return {**principal.to_json(), "plan": ring.plans[principal.plan].to_json(), "usage": row["usage"],
                "expiresAt": row["expiresAt"]}



async def count(session: AsyncSession, stmt: Any) -> int:
    from sqlalchemy import func

    return (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
