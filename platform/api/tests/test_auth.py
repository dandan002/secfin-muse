"""Sign-in through Clerk: a valid session token opens the desk and names the trader; nothing else does."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from loandesk import auth
from loandesk.settings import get_settings

ISSUER = "https://desk-test.clerk.accounts.dev"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key: Any = KEY, **claims: Any) -> str:
    now = int(time.time())
    body = {"iss": ISSUER, "sub": "user_2abc", "iat": now, "nbf": now, "exp": now + 60,
            "azp": "http://localhost:5180", **claims}
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def clerk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "clerk_issuer", ISSUER)
    monkeypatch.setattr(get_settings(), "clerk_authorized_parties", ["http://localhost:5180"])  # whatever .env says
    monkeypatch.setattr(get_settings(), "traders", [])
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=KEY.public_key()))
    monkeypatch.setattr(auth, "_jwks", lambda _issuer: jwks)


async def test_without_a_token_the_desk_is_closed(client: httpx.AsyncClient, clerk: None) -> None:
    assert (await client.get("/locates")).status_code == 401
    assert (await client.get("/health")).status_code == 200


@pytest.mark.parametrize("bad", [
    token(OTHER),  # not signed by Clerk
    token(exp=int(time.time()) - 120),  # expired
    token(iss="https://someone-else.clerk.accounts.dev"),
    token(azp="https://evil.example"),  # minted for another site
])
async def test_bad_tokens_are_refused(client: httpx.AsyncClient, clerk: None, bad: str) -> None:
    assert (await client.get("/locates", headers={"authorization": f"Bearer {bad}"})).status_code == 401


async def test_the_token_names_the_trader_not_the_header(client: httpx.AsyncClient, clerk: None) -> None:
    h = {"authorization": f"Bearer {token(trader='Sam')}", "x-trader": "someone-else"}
    r = await client.post("/locates", headers=h, json={"counterparty": "PBALPHA", "lines": [
        {"identifier": "US9988883098", "quantity": 1000}]})  # a typo line: rejected on sight, no venue needed
    assert r.status_code == 201
    ev = (await client.get("/events", headers=h, params={"entity": "locate", "id": r.json()[0]["id"]})).json()
    assert {e["actor"] for e in ev} == {"trader:sam"}
    no_claim = {"authorization": f"Bearer {token()}"}
    assert (await client.get("/locates", headers=no_claim)).status_code == 200  # falls back to the Clerk user id


async def test_docs_and_schema_close_with_sign_in_on(clerk: None, client: httpx.AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


async def test_the_stream_refuses_a_missing_or_bad_token(clerk: None) -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from loandesk.app import create_app

    tc = TestClient(create_app())  # no lifespan: refused before anything it sets up is touched
    for q in ("", f"?token={token(OTHER)}"):
        with pytest.raises(WebSocketDisconnect) as e, tc.websocket_connect("/stream" + q) as ws:
            ws.receive_json()
        assert e.value.code == 4401


async def test_sign_in_off_keeps_the_dev_desk_open(client: httpx.AsyncClient) -> None:
    assert (await client.get("/locates")).status_code == 200


async def test_only_the_desks_traders_get_in(client: httpx.AsyncClient, clerk: None,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A Clerk instance may let anyone sign up: with a trader list set, a valid token from anyone else is refused."""
    monkeypatch.setattr(get_settings(), "traders", ["Sam", "kli"])
    ok = {"authorization": f"Bearer {token(trader='sam')}"}
    stranger = {"authorization": f"Bearer {token(username='mallory')}"}
    assert (await client.get("/locates", headers=ok)).status_code == 200
    r = await client.put("/kill-switch", headers=stranger, json={"on": False})
    assert r.status_code == 403 and "mallory" in r.json()["detail"]


async def test_the_heads_switches(client: httpx.AsyncClient, clerk: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anyone throws the kill switch or turns the autopilot off; only a head releases it, turns it on, or sets
    policy. A head is ``role: head`` in the token (Clerk metadata) or named in LOANDESK_HEADS."""
    from loandesk import autopilot

    monkeypatch.setattr(autopilot, "_state", {"mode": "off", "by": None, "at": None, "tripped": None})
    monkeypatch.setattr(get_settings(), "heads", ["boss@desk.example"])
    trader_ = {"authorization": f"Bearer {token(trader='sam')}"}
    by_meta = {"authorization": f"Bearer {token(trader='kli', role='head')}"}
    by_list = {"authorization": f"Bearer {token(email='Boss@desk.example')}"}
    assert (await client.get("/me", headers=trader_)).json() == {"trader": "trader:sam", "head": False, "signIn": True}
    assert (await client.get("/me", headers=by_meta)).json()["head"] is True
    assert (await client.get("/me", headers=by_list)).json()["head"] is True
    assert (await client.put("/kill-switch", headers=trader_, json={"on": True})).status_code == 200
    r = await client.put("/kill-switch", headers=trader_, json={"on": False})
    assert r.status_code == 403 and "head of desk" in r.json()["detail"]
    assert (await client.put("/kill-switch", headers=by_meta, json={"on": False})).status_code == 200
    assert (await client.put("/autopilot", headers=trader_, json={"mode": "autopilot"})).status_code == 403
    assert (await client.put("/autopilot", headers=by_list, json={"mode": "autopilot"})).status_code == 200
    assert (await client.put("/autopilot", headers=trader_, json={"mode": "off"})).status_code == 200
    lim = (await client.get("/policy", headers=trader_)).json()["breaker"]["value"]
    assert (await client.put("/policy/breaker", headers=trader_, json=lim)).status_code == 403
    assert (await client.delete("/policy/breaker", headers=trader_)).status_code == 403
    assert (await client.put("/policy/breaker", headers=by_meta, json=lim)).status_code == 200


def test_the_stream_closes_when_the_sign_in_lapses_unless_renewed(clerk: None, monkeypatch: pytest.MonkeyPatch,
                                                                   setup: None) -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from loandesk.app import create_app
    from loandesk.routes import desk

    monkeypatch.setattr(desk, "STREAM_GRACE_S", 0)
    tc = TestClient(create_app())
    short = token(exp=int(time.time()) + 1)
    with pytest.raises(WebSocketDisconnect) as e, tc.websocket_connect(f"/stream?token={short}") as ws:
        for _ in range(10):
            ws.receive_json()  # the clock, each second, until the token runs out
    assert e.value.code == 4401
    with tc.websocket_connect(f"/stream?token={token(exp=int(time.time()) + 1)}") as ws:
        ws.send_json({"type": "auth", "token": token()})  # the browser renews as Clerk refreshes
        for _ in range(3):
            assert ws.receive_json()["topic"] == "clock"  # still open past the first token's expiry


async def test_clerk_says_who_trades(client: httpx.AsyncClient, clerk: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """With LOANDESK_TRADER_ROLES, the role in the loandesk token (Clerk public metadata) decides who gets in."""
    monkeypatch.setattr(get_settings(), "trader_roles", ["trader", "head"])
    ok = {"authorization": f"Bearer {token(email='sam@desk.example', role='trader')}"}
    no_role = {"authorization": f"Bearer {token(email='eve@elsewhere.example', role=None)}"}
    default_token = {"authorization": f"Bearer {token()}"}  # Clerk's default token: no role claim at all
    assert (await client.get("/me", headers=ok)).json()["trader"] == "trader:sam@desk.example"
    assert (await client.get("/locates", headers=no_role)).status_code == 403
    r = await client.get("/locates", headers=default_token)
    assert r.status_code == 403 and "JWT template" in r.json()["detail"]
