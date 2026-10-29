"""LoanDesk AI platform: the desk's own API.

    uv run uvicorn loandesk.app:app --port 8100

Not affiliated with or endorsed by EquiLend, Wematch, FIS or Bloomberg. All data is synthetic.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import agent, db, secmaster
from .adapters import systems
from .routes import chat, desk, reference, trading
from .settings import get_settings
from .sync import Syncer

log = logging.getLogger("loandesk")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = get_settings()
    secmaster.set_seed(cfg.seed)
    db.configure(cfg.database_url)
    await db.create_all()
    async with db.session() as s:
        n = await secmaster.load(s)
    if n:
        log.info("security master: loaded %d instruments", n)
    syncer = Syncer()
    if cfg.sync_enabled:
        syncer.start()
    await agent.start()
    yield
    await agent.stop()
    await syncer.stop()
    await systems().close()
    await db.dispose()


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="LoanDesk AI", version="0.1.0", lifespan=lifespan,
                  description="Securities-lending desk platform. Synthetic data only.")
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"], allow_headers=["*"])
    for r in (reference.router, trading.router, chat.router, desk.router):
        app.include_router(r)

    @app.get("/health", tags=["audit"])
    async def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
