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


class NotATrader(AuthError):
    """Signed in, but not someone the desk lets trade."""


@lru_cache
def _jwks(issuer: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer.rstrip('/')}/.well-known/jwks.json", cache_keys=True, lifespan=3600)


def enabled() -> bool:
    return bool(get_settings().clerk_issuer)


async def verify(token: str) -> dict[str, Any]:
    """The token's claims, if Clerk signed it for this desk, it's live, and it names one of the desk's traders."""
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
    listed = bool(_who(claims) & {t.strip().lower() for t in cfg.traders})
    role = str(claims.get("role") or "").lower()
    by_role = role in {r.strip().lower() for r in cfg.trader_roles}
    if (cfg.traders or cfg.trader_roles) and not (listed or by_role):
        why = ("the session token has no role: is the loandesk JWT template set up (infra/clerk_setup.py)?"
               if cfg.trader_roles and "role" not in claims else "")
        raise NotATrader(f"{trader_name(claims)} isn't one of the desk's traders{'; ' + why if why else ''}")
    return claims


def trader_name(claims: dict[str, Any]) -> str:
    """The ``trader`` claim (the ``loandesk`` token template sets it), else ``username``, else the email, else the
    Clerk user id."""
    return str(claims.get("trader") or claims.get("username") or claims.get("email") or claims["sub"]
               ).strip().lower()[:40]


def _who(claims: dict[str, Any]) -> set[str]:
    """Every name the desk may know this person by, for the trader and head lists."""
    return {str(v).strip().lower() for v in (trader_name(claims), claims.get("username"), claims.get("email"),
                                             claims.get("sub")) if v}


def is_head(claims: dict[str, Any] | None) -> bool:
    """Head of desk: ``role: head`` in the token (Clerk public metadata), or named in ``LOANDESK_HEADS``."""
    if claims is None:
        return not enabled()  # sign-in off: local dev, everyone is the head
    heads = {h.strip().lower() for h in get_settings().heads}
    return str(claims.get("role") or "").lower() == "head" or bool(_who(claims) & heads)


async def require_user(conn: HTTPConnection) -> None:
    """App-wide dependency: with Clerk on, no valid token, no desk. (The WebSocket checks its own ``?token=``.)"""
    if not enabled() or conn.scope["type"] == "websocket" or conn.url.path == "/health":
        return
    scheme, _, token = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "sign in first", headers={"WWW-Authenticate": "Bearer"})
    try:
        conn.state.claims = await verify(token)
    except NotATrader as e:
        raise HTTPException(403, str(e)) from e
    except AuthError as e:
        raise HTTPException(401, f"invalid session: {e}", headers={"WWW-Authenticate": "Bearer"}) from e
