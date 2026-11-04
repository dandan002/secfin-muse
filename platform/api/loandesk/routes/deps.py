from __future__ import annotations

from fastapi import Header, HTTPException, Request

from .. import auth


def trader(request: Request, x_trader: str | None = Header(None)) -> str:
    """Who is acting: the signed-in Clerk user. With sign-in off (local dev), the ``X-Trader`` header."""
    if auth.enabled():
        return f"trader:{auth.trader_name(request.state.claims)}"
    return f"trader:{(x_trader or 'desk').strip().lower()[:32]}"


def require_head(request: Request, what: str) -> None:
    """Some switches are the head of desk's: releasing the kill switch, turning the autopilot on, the policy."""
    if not auth.is_head(getattr(request.state, "claims", None)):
        raise HTTPException(403, f"only a head of desk can {what}")


def head(request: Request, x_trader: str | None = Header(None)) -> str:
    """A trader who is a head of desk (dependency for the policy routes)."""
    require_head(request, "change the desk's policy")
    return trader(request, x_trader)
