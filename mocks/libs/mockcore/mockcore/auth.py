"""Signed webhooks. (API keys live in ``mockcore.keys``.)

Webhook signature header: ``X-Mock-Signature: t=<unix>,v1=<hex hmac-sha256(secret, "t.body")>``.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Mock-Signature"


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if tolerance_s and abs(time.time() - ts) > tolerance_s:
        return False
    expected = sign(secret, body, ts)
    return hmac.compare_digest(expected, header)
