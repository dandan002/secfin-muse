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


async def test_sign_in_off_keeps_the_dev_desk_open(client: httpx.AsyncClient) -> None:
    assert (await client.get("/locates")).status_code == 200
