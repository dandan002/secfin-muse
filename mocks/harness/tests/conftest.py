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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run_all, smoke

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
        if "MOCK_TEST_DATABASE_ADMIN_URL" in os.environ:  # a database was promised (e.g. CI): don't go green
            pytest.fail(f"MOCK_TEST_DATABASE_ADMIN_URL is set but Postgres is unreachable: {e}")
        pytest.skip(f"no Postgres for integration tests: {e}")
    # A fixed key secret: bootstrap keys then stay put across the tests' resets to other seeds.
    s = start_stack(base_port=18000, db_url=db_url, fix_port=19878,
                    log_dir=Path(__file__).resolve().parents[1] / "out" / "test-logs",
                    extra_env={"MOCK_KEY_SECRET": "harness-test-secret"})
    try:
        yield s
    finally:
        s.stop()


# Who the tests are when a call doesn't say: control's operator, our Venue A participant, the desk.
DEFAULT_KEYS = {"control": ("authorization", "operatorKey"), "ngt": ("x-participant-key", "participantKey"),
                "ib": ("x-api-key", "deskKey")}


class Mocks:
    """Convenience wrapper: authenticated clients for each mock plus control helpers."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.urls = stack.urls
        self.http = httpx.Client(timeout=60)
        self._keys: dict[str, dict[str, str]] = {}

    def url(self, svc: str, path: str) -> str:
        return self.urls[svc] + path

    def _auth(self, svc: str, kw: dict) -> dict:
        """Add the service's default key; a caller's own headers (say a Bearer) win."""
        if svc in DEFAULT_KEYS and svc not in self._keys:
            header, field = DEFAULT_KEYS[svc]
            key = self.creds(svc)[field]
            self._keys[svc] = {header: f"Bearer {key}" if header == "authorization" else key}
        return {**kw, "headers": {**self._keys.get(svc, {}), **(kw.get("headers") or {})}}

    def get(self, svc: str, path: str, **kw) -> httpx.Response:
        return self.http.get(self.url(svc, path), **self._auth(svc, kw))

    def post(self, svc: str, path: str, **kw) -> httpx.Response:
        return self.http.post(self.url(svc, path), **self._auth(svc, kw))

    def put(self, svc: str, path: str, **kw) -> httpx.Response:
        return self.http.put(self.url(svc, path), **self._auth(svc, kw))

    def delete(self, svc: str, path: str, **kw) -> httpx.Response:
        return self.http.delete(self.url(svc, path), **self._auth(svc, kw))

    def patch(self, svc: str, path: str, **kw) -> httpx.Response:
        return self.http.patch(self.url(svc, path), **self._auth(svc, kw))

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
        return self.http.get(self.url(svc, "/_admin/credentials")).raise_for_status().json()

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
