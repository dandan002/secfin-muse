"""Integration fixtures: one real Postgres database and every mock running as its own process.

Set ``MOCK_TEST_DATABASE_ADMIN_URL`` (default ``postgresql://postgres@localhost:5432/postgres``) to
point at a server where the tests may create and drop ``loandesk_test_<service>``: one database
per mock, as in production.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_all import SERVICES, Stack, start_stack  # noqa: E402

ADMIN_URL = os.environ.get("MOCK_TEST_DATABASE_ADMIN_URL", "postgresql://postgres@localhost:5432/postgres")
TEST_DB = "loandesk_test_{service}"


def _recreate_db() -> str:
    import asyncio

    import asyncpg

    async def go() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        try:
            for name, _, _ in SERVICES:
                db = TEST_DB.format(service=name)
                await conn.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
                await conn.execute(f"CREATE DATABASE {db}")
        finally:
            await conn.close()

    asyncio.run(go())
    base = ADMIN_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    return f"{base}/{TEST_DB}"


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    try:
        db_url = _recreate_db()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no Postgres for integration tests: {e}")
    s = start_stack(base_port=18000, db_url=db_url, fix_port=19878,
                    log_dir=Path(__file__).resolve().parents[1] / "out" / "test-logs")
    try:
        yield s
    finally:
        s.stop()


class Mocks:
    """Convenience wrapper: authenticated clients for each mock plus control helpers."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.urls = stack.urls
        self.http = httpx.Client(timeout=60)

    def url(self, svc: str, path: str) -> str:
        return self.urls[svc] + path

    def get(self, svc: str, path: str, **kw) -> httpx.Response:
        return self.http.get(self.url(svc, path), **kw)

    def post(self, svc: str, path: str, **kw) -> httpx.Response:
        return self.http.post(self.url(svc, path), **kw)

    def put(self, svc: str, path: str, **kw) -> httpx.Response:
        return self.http.put(self.url(svc, path), **kw)

    def reset(self, mode: str = "stepped", seed: int = 20261027, epoch: str = "2026-10-27T09:00:00-04:00") -> dict:
        r = self.post("control", "/reset", json={"seed": seed, "mode": mode, "epoch": epoch})
        r.raise_for_status()
        body = r.json()
        bad = {k: v for k, v in body["services"].items() if isinstance(v, dict) and v.get("error")}
        assert not bad, bad
        return body

    def advance(self, seconds: float) -> dict:
        r = self.post("control", "/clock/advance", json={"seconds": seconds})
        r.raise_for_status()
        return r.json()

    def creds(self, svc: str) -> dict:
        return self.get(svc, "/_admin/credentials").json()

    def verb(self, svc: str, verb: str, **args) -> dict:
        r = self.post(svc, f"/_admin/scenario/{verb}", json=args)
        r.raise_for_status()
        return r.json()


@pytest.fixture()
def mocks(stack: Stack) -> Iterator[Mocks]:
    m = Mocks(stack)
    m.reset()
    yield m
    m.http.close()
