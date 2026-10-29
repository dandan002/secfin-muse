import asyncio
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import MetaData

from mockcore.db import Database
from mockcore.keys import AuthSpec, Grant, KeyRing, Plan, Principal, _hash, make_key_table

SCOPES = {"orders:read": "read orders", "orders:write": "place orders"}


def ring(master: str = "s3cret", plans: dict[str, Plan] | None = None) -> KeyRing:
    spec = AuthSpec(SCOPES, lambda: [Grant("FIRM", ("orders:read", "orders:write"))])
    if plans:
        spec.plans = plans
    return KeyRing("mock-venue", spec, make_key_table(MetaData()), master, lambda: 7)


def put(r: KeyRing, principal: str = "FIRM", scopes: tuple[str, ...] = ("orders:read",), plan: str = "standard",
        **extra) -> str:
    """Add a key to the ring's cache directly (what load() does from the table) and return it."""
    key_id, secret = r._bootstrap_parts(principal)
    r._rows[key_id] = {"id": key_id, "secret_hash": _hash(secret), "principal": principal, "scopes": list(scopes),
                       "plan": plan, "label": "", "bootstrap": True, "status": "ACTIVE",
                       "created_at": datetime.now(UTC), "expires_at": None, "revoked_at": None, **extra}
    return r._format(key_id, secret)


def status(r: KeyRing, key: str | None, *scopes: str) -> int | str:
    try:
        return r.check(key, scopes)[0].principal
    except HTTPException as e:
        return e.status_code


def test_keys_authenticate_and_scopes_authorise():
    r = ring()
    key = put(r)
    assert key.startswith("mk_venue_")
    assert status(r, key, "orders:read") == "FIRM"
    assert status(r, key, "orders:write") == 403
    assert status(r, None) == 401
    assert status(r, key[:-1] + ("0" if key[-1] != "0" else "1")) == 401  # wrong secret, right id
    assert status(r, key.replace("mk_venue_", "mk_other_")) == 401  # another service's key


def test_bootstrap_keys_need_the_master_secret():
    assert put(ring("s3cret")) == put(ring("s3cret"))
    assert put(ring("s3cret")) != put(ring("other"))
    assert ring("").mode == "seed" and ring("x").mode == "secret"
    assert ring("s3cret").derive("webhook", "PB") != ring("").derive("webhook", "PB")


def test_revoked_and_expired_keys_are_refused():
    r = ring()
    assert status(r, put(r, status="REVOKED")) == 401
    assert status(r, put(r, expires_at=datetime.now(UTC) - timedelta(seconds=1))) == 401
    assert status(r, put(r, expires_at=datetime.now(UTC) + timedelta(hours=1))) == "FIRM"


def test_plans_rate_limit_and_cap_the_day():
    r = ring(plans={"tiny": Plan("tiny", 0.001, 2, 3)})
    key = put(r, plan="tiny")
    assert [status(r, key) for _ in range(3)] == ["FIRM", "FIRM", 429]  # burst of 2
    r.set_plan(Plan("tiny", 1000, 1000, 3))
    assert status(r, key) == "FIRM"  # the third call of the day
    with pytest.raises(HTTPException) as e:
        r.check(key)
    assert e.value.status_code == 429 and e.value.detail["error"] == "quota_exceeded"
    assert int(e.value.headers["Retry-After"]) <= 86400


def test_require_sets_rate_limit_headers_and_names_missing_scopes():
    r = ring()
    key = put(r)
    app = FastAPI()

    @app.get("/orders")
    async def orders(p: Principal = Depends(r.require("orders:read"))) -> dict:
        return {"who": p.principal}

    @app.post("/orders")
    async def place(p: Principal = Depends(r.require("orders:write"))) -> dict:
        return {}

    async def go() -> tuple[httpx.Response, ...]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return (await c.get("/orders", headers={"Authorization": f"Bearer {key}"}),
                    await c.get("/orders", headers={"X-Api-Key": key}),
                    await c.get(f"/orders?key={key}"),
                    await c.post("/orders", headers={"X-Api-Key": key}),
                    await c.get("/orders"))

    bearer, header, query, forbidden, anonymous = asyncio.run(go())
    assert bearer.json() == {"who": "FIRM"} and header.status_code == query.status_code == 200
    assert bearer.headers["X-RateLimit-Limit"] == "100000"
    assert forbidden.status_code == 403 and forbidden.json()["detail"]["missingScopes"] == ["orders:write"]
    assert anonymous.status_code == 401 and "WWW-Authenticate" in anonymous.headers


def test_routes_cannot_require_undeclared_scopes():
    with pytest.raises(AssertionError):
        ring().require("admin:everything")


@pytest.mark.skipif("MOCK_TEST_DATABASE_ADMIN_URL" not in os.environ, reason="needs Postgres")
async def test_issued_keys_rotate_and_revocations_survive_a_reload():
    import asyncpg

    admin = os.environ["MOCK_TEST_DATABASE_ADMIN_URL"]
    conn = await asyncpg.connect(admin)
    await conn.execute("DROP DATABASE IF EXISTS loandesk_test_keys WITH (FORCE)")
    await conn.execute("CREATE DATABASE loandesk_test_keys")
    await conn.close()
    url = admin.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + "/loandesk_test_keys"

    meta = MetaData(schema="venue")
    r = KeyRing("mock-venue", AuthSpec(SCOPES, lambda: [Grant("FIRM", ("orders:read",))]), make_key_table(meta),
                "s3cret", lambda: 7)
    db = Database(url, "venue", meta)
    try:
        await db.init()
        await r.load(db)
        boot = r.bootstrap_key("FIRM")
        assert boot and status(r, boot, "orders:read") == "FIRM"

        key, row = await r.issue("HF1", ["orders:read", "orders:write"], "trial", "hedge fund")
        assert status(r, key, "orders:write") == "HF1"
        with pytest.raises(HTTPException):
            await r.issue("HF1", ["orders:delete"])

        new, _, old = await r.rotate(row["id"], timedelta(seconds=0))
        assert status(r, key) == 401 and status(r, new, "orders:write") == "HF1"

        await r.change(r._bootstrap_parts("FIRM")[0], ["orders:read", "orders:write"], None)
        await r.revoke(r._bootstrap_parts("FIRM")[0])
        await db.truncate()  # a sim reset keeps the keys
        await r.load(db)
        assert r.bootstrap_key("FIRM") is None and status(r, boot) == 401  # revocation sticks
        assert status(r, new) == "HF1"
        assert {k["status"] for k in r.list()} == {"ACTIVE", "EXPIRED", "REVOKED"}
    finally:
        await db.dispose()
