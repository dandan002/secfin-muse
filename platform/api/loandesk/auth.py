"""Sign-in through Clerk: the desk verifies each request's Clerk session token (a short-lived RS256 JWT).

Set ``LOANDESK_CLERK_ISSUER`` (the Frontend API URL of the Clerk instance, e.g. ``https://xyz.clerk.accounts.dev``)
and every route but ``/health`` needs ``Authorization: Bearer <token>``; the WebSocket takes ``?token=``. The
trader is who the token says, never the ``X-Trader`` header. Unset, the desk is open (local dev only).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException
from starlette.requests import HTTPConnection

from .settings import get_settings


class AuthError(Exception):
    pass


@lru_cache
def _jwks(issuer: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer.rstrip('/')}/.well-known/jwks.json", cache_keys=True, lifespan=3600)


def enabled() -> bool:
    return bool(get_settings().clerk_issuer)


async def verify(token: str) -> dict[str, Any]:
    """The token's claims, if Clerk signed it for this desk and it's live."""
    cfg = get_settings()
    issuer = cfg.clerk_issuer.rstrip("/")
    try:
        key = await asyncio.to_thread(_jwks(issuer).get_signing_key_from_jwt, token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=issuer, leeway=5,
                            options={"require": ["exp", "iat", "sub"]})
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e
    parties = cfg.clerk_authorized_parties or cfg.cors_origins
    if claims.get("azp") and claims["azp"] not in parties:  # a token minted for another site
        raise AuthError(f"token is for {claims['azp']}")
    return claims


def trader_name(claims: dict[str, Any]) -> str:
    """A ``trader`` (or ``username``) claim added to the Clerk session token, else the Clerk user id."""
    return str(claims.get("trader") or claims.get("username") or claims["sub"]).strip().lower()[:32]


async def require_user(conn: HTTPConnection) -> None:
    """App-wide dependency: with Clerk on, no valid token, no desk. (The WebSocket checks its own ``?token=``.)"""
    if not enabled() or conn.scope["type"] == "websocket" or conn.url.path == "/health":
        return
    scheme, _, token = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "sign in first", headers={"WWW-Authenticate": "Bearer"})
    try:
        conn.state.claims = await verify(token)
    except AuthError as e:
        raise HTTPException(401, f"invalid session: {e}", headers={"WWW-Authenticate": "Bearer"}) from e
