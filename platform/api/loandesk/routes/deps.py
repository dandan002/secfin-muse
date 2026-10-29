from __future__ import annotations

from fastapi import Header


def trader(x_trader: str | None = Header(None)) -> str:
    """Who is acting. A placeholder until the desk has real logins (Cognito roles in the spec)."""
    return f"trader:{(x_trader or 'desk').strip().lower()[:32]}"
