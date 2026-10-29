from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

os.environ["LOANDESK_SYNC_ENABLED"] = "false"
os.environ["LOANDESK_AGENT_MODE"] = "off"
# Nothing listens here: adapter calls fail fast, the way a down vendor would.
for name in ("CONTROL", "IB", "GATEWAY", "NGT", "WEMATCH", "SFP"):
    os.environ[f"LOANDESK_{name}_URL"] = "http://127.0.0.1:9"

from loandesk import clock, db, secmaster  # noqa: E402
from loandesk.app import create_app  # noqa: E402
from loandesk.settings import get_settings  # noqa: E402


@pytest.fixture(scope="session")
async def setup(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[None]:
    get_settings.cache_clear()
    url = f"sqlite+aiosqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"
    secmaster.set_seed(20261027)
    clock.pin(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))  # 10:00 New York
    db.configure(url)
    await db.create_all()
    async with db.session() as s:
        await secmaster.load(s)
    yield
    await db.dispose()


@pytest.fixture
async def client(setup: None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://desk") as c:
        yield c
