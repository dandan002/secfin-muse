"""LoanDesk AI platform: the desk's own API.

    uv run uvicorn loandesk.app:app --port 8100

Not affiliated with or endorsed by EquiLend, Wematch, FIS or Bloomberg. All data is synthetic.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import agent, auth, autopilot, db, gate, observe, policy, secmaster
from .adapters import systems
from .routes import chat, desk, reference, trading
from .settings import get_settings
from .sync import Syncer

log = logging.getLogger("loandesk")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = get_settings()
    if not auth.enabled():
        log.warning("sign-in is off (LOANDESK_CLERK_ISSUER unset): the desk API is open to anyone who can reach it")
    secmaster.set_seed(cfg.seed)
    db.configure(cfg.database_url)
    await db.create_all()
    async with db.session() as s:
        n = await secmaster.load(s)
        await policy.load(s)
        await gate.load_kill_switch(s)
    if gate.kill_switch()["on"]:
        log.warning("the kill switch is on (thrown by %s): nothing executes until a trader releases it",
                    gate.kill_switch()["by"])
    if n:
        log.info("security master: loaded %d instruments", n)
    await agent.start()  # before the syncer: cards it drafts at start-up get their trade review as they're proposed
    syncer = Syncer()
    if cfg.sync_enabled:
        syncer.start()
    await autopilot.restore()
    autopilot.start()
    if autopilot.mode() != "off":
        log.warning("autopilot starts in %s mode: the desk approves its own cards within its limits", autopilot.mode())
    if observe.enabled():
        log.info("tracing model calls and agent runs to Langfuse at %s", cfg.langfuse_host)
    yield
    await autopilot.stop()
    await agent.stop()
    observe.flush()
    await syncer.stop()
    await systems().close()
    await db.dispose()


def create_app() -> FastAPI:
    cfg = get_settings()
    # With sign-in on, the schema and docs pages close too: they'd map every route for anyone (they sit outside the
    # gated routers). Open in local dev.
    docs = {} if not auth.enabled() else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="LoanDesk AI", version="0.1.0", lifespan=lifespan,
                  description="Securities-lending desk platform. Synthetic data only.", **docs)
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"], allow_headers=["*"])
    for r in (reference.router, trading.router, chat.router, desk.router):
        app.include_router(r, dependencies=[Depends(auth.require_user)])

    @app.get("/health", tags=["audit"])
    async def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
