"""Verify the gateway's signed decision webhooks.

Header ``X-Mock-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw body)>``,
the same scheme as Stripe-style webhooks. Verify against the raw request bytes, before parsing.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Mock-Signature"


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance_s: int | None = 300, now: float | None = None) -> bool:
    """True if ``header`` signs ``body`` with ``secret`` and (unless ``tolerance_s`` is None) is
    fresh. Rejecting stale timestamps stops replays of captured requests."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, given = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        return False
    if tolerance_s is not None and abs((time.time() if now is None else now) - ts) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)
