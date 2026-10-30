from __future__ import annotations

from fastapi import Header, Request

from .. import auth


def trader(request: Request, x_trader: str | None = Header(None)) -> str:
    """Who is acting: the signed-in Clerk user. With sign-in off (local dev), the ``X-Trader`` header."""
    if auth.enabled():
        return f"trader:{auth.trader_name(request.state.claims)}"
    return f"trader:{(x_trader or 'desk').strip().lower()[:32]}"
