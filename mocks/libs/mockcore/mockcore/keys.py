"""API keys, scopes and usage plans: the gate in front of every mock's business API.

A key reads ``mk_<service>_<keyId>_<secret>``. Only a SHA-256 of the secret is stored, in the
service's own ``api_keys`` table, which survives ``/_admin/reset`` (keys are identity, not sim state).

* **Bootstrap keys** (the platform's own logins, one per principal the service declares) are derived
  from ``MOCK_KEY_SECRET`` with HMAC, so they survive restarts and snapshot restores, and the operator
  reads them from ``GET /_admin/credentials`` (admin token). Without the secret they're derived from
  the public seed instead: fine on a laptop, guessable anywhere else, so the service logs a warning.
* **Issued keys** (``POST /_admin/keys``) are random; the plaintext is returned once and never stored.
* **Scopes** entitle a key to routes (``require("orders:write")``): a valid key without the scope is a
  403 naming what's missing. **Plans** cap each key: a token bucket per second plus a daily quota
  (UTC day), answered with 429, ``Retry-After`` and ``X-RateLimit-*`` headers.

Rate limits and quotas run on wall time, not sim time, like a real vendor's gateway, and are counted
in memory: a restart forgives a day's usage.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, Response, WebSocket
from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, select, update
from sqlalchemy.dialects.postgresql import insert

from .db import Database, JsonType

log = logging.getLogger(__name__)

# Where a caller may present its key. Bearer first, then the service's named header, then the query
# string (browsers can't set headers on a WebSocket).
HEADERS = ("x-api-key", "x-participant-key")
QUERY_PARAMS = ("key", "token")


@dataclass(frozen=True)
class Plan:
    name: str
    rate_per_s: float
    burst: float
    daily_quota: int | None  # None: unlimited
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ratePerSecond": self.rate_per_s, "burst": self.burst,
                "dailyQuota": self.daily_quota, "description": self.description}


DEFAULT_PLANS: dict[str, Plan] = {
    p.name: p
    for p in (
        Plan("trial", 1.0, 10, 1_000, "Evaluation access: 1 request/s, 1,000 a day"),
        Plan("standard", 20.0, 40, 100_000, "A connected firm: 20 requests/s, 100,000 a day"),
        Plan("partner", 200.0, 400, None, "The LoanDesk platform itself: 200 requests/s, no daily cap"),
    )
}


@dataclass(frozen=True)
class Grant:
    """A bootstrap key: who it acts for, what it may do, and on which plan.

    ``name`` tells apart two keys for the same principal (say a read-only one); it defaults to the
    principal and is what ``bootstrap_key()`` takes.
    """

    principal: str
    scopes: tuple[str, ...]
    plan: str = "standard"
    label: str = ""
    name: str = ""

    @property
    def handle(self) -> str:
        return self.name or self.principal


@dataclass
class AuthSpec:
    scopes: dict[str, str]  # scope -> what it allows, shown in the catalog
    grants: Callable[[], Iterable[Grant]]  # bootstrap principals; may depend on the seed
    plans: dict[str, Plan] = field(default_factory=lambda: dict(DEFAULT_PLANS))


@dataclass
class Principal:
    key_id: str
    principal: str
    scopes: frozenset[str]
    plan: str
    label: str

    def to_json(self) -> dict[str, Any]:
        return {"keyId": self.key_id, "principal": self.principal, "scopes": sorted(self.scopes),
                "plan": self.plan, "label": self.label}


def make_key_table(metadata: MetaData) -> Table:
    return Table(
        "api_keys",
        metadata,
        Column("id", String(16), primary_key=True),
        Column("secret_hash", String(64), nullable=False),
        Column("principal", String(64), nullable=False, index=True),
        Column("scopes", JsonType, nullable=False),
        Column("plan", String(32), nullable=False),
        Column("label", String(128), nullable=False, default=""),
        Column("bootstrap", Boolean, nullable=False, default=False),
        Column("status", String(16), nullable=False, default="ACTIVE"),  # ACTIVE | REVOKED
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True)),
        Column("revoked_at", DateTime(timezone=True)),
        info={"keep_on_reset": True},
    )


class _Bucket:
    def __init__(self, plan: Plan) -> None:
        self.plan, self.tokens, self.ts = plan, plan.burst, time.monotonic()

    def take(self) -> float:
        """0 if a token was taken, else seconds until one is available."""
        now = time.monotonic()
        self.tokens = min(self.plan.burst, self.tokens + (now - self.ts) * self.plan.rate_per_s)
        self.ts = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.plan.rate_per_s


@dataclass
class _Usage:
    day: str = ""
    today: int = 0
    total: int = 0
    denied: int = 0
    last_used: float | None = None

    def to_json(self) -> dict[str, Any]:
        last = datetime.fromtimestamp(self.last_used, UTC).isoformat() if self.last_used else None
        return {"day": self.day, "today": self.today, "total": self.total, "denied": self.denied, "lastUsed": last}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _err(status: int, error: str, detail: str, headers: dict[str, str] | None = None, **extra: Any) -> HTTPException:
    return HTTPException(status, {"error": error, "detail": detail, **extra}, headers=headers)


class KeyRing:
    def __init__(self, service: str, spec: AuthSpec, table: Table, master: str, seed: Callable[[], int]) -> None:
        self.service = service.removeprefix("mock-")
        self.spec = spec
        self.plans = dict(spec.plans)
        self.table = table
        self._master = master
        self._seed = seed
        self._rows: dict[str, dict[str, Any]] = {}
        self._buckets: dict[str, _Bucket] = {}
        self._usage: dict[str, _Usage] = {}
        self.db: Database | None = None
        if not master:
            log.warning("%s: MOCK_KEY_SECRET unset, bootstrap keys are derived from the public seed", service)

    # -- derivation ------------------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return "secret" if self._master else "seed"

    def derive(self, *parts: str, n: int = 32) -> str:
        """A stable secret for this service (bootstrap keys, webhook secrets, FIX passwords)."""
        master = self._master or f"seed:{self._seed()}"
        msg = "|".join([self.service, *parts]).encode()
        return hmac.new(master.encode(), msg, hashlib.sha256).hexdigest()[:n]

    def _bootstrap_parts(self, handle: str) -> tuple[str, str]:
        return self.derive("key-id", handle, n=8), self.derive("key", handle)

    def _format(self, key_id: str, secret: str) -> str:
        return f"mk_{self.service}_{key_id}_{secret}"

    def bootstrap_key(self, handle: str) -> str | None:
        """The plaintext of a bootstrap key (by grant name), unless it was revoked or rotated away."""
        key_id, secret = self._bootstrap_parts(handle)
        row = self._rows.get(key_id)
        return self._format(key_id, secret) if row and self._active(row) else None

    # -- storage ---------------------------------------------------------------------------------
    async def load(self, db: Database) -> None:
        """Make sure every declared principal has its bootstrap key, then cache the table."""
        self.db = db
        grants = list(self.spec.grants())
        wanted = {self._bootstrap_parts(g.handle)[0]: g for g in grants}
        async with db.engine.begin() as conn:
            # Bootstrap keys derived from an older seed (or secret) no longer match anyone: drop them.
            await conn.execute(self.table.delete().where(self.table.c.bootstrap.is_(True),
                                                         self.table.c.id.not_in(list(wanted) or [""])))
            for key_id, g in wanted.items():
                _, secret = self._bootstrap_parts(g.handle)
                # Insert only: an operator's revoke or re-scope of a bootstrap key sticks.
                await conn.execute(insert(self.table).values(
                    id=key_id, secret_hash=_hash(secret), principal=g.principal, scopes=sorted(g.scopes),
                    plan=g.plan, label=g.label or g.handle, bootstrap=True, status="ACTIVE",
                    created_at=_now()).on_conflict_do_nothing(index_elements=["id"]))
            rows = (await conn.execute(select(self.table))).mappings().all()
        self._rows = {r["id"]: dict(r) for r in rows}
        self._buckets.clear()

    async def _write(self, key_id: str, **values: Any) -> dict[str, Any]:
        assert self.db is not None
        async with self.db.engine.begin() as conn:
            await conn.execute(update(self.table).where(self.table.c.id == key_id).values(**values))
            row = (await conn.execute(select(self.table).where(self.table.c.id == key_id))).mappings().one()
        self._rows[key_id] = dict(row)
        self._buckets.pop(key_id, None)
        return self._rows[key_id]

    # -- admin -----------------------------------------------------------------------------------
    def _check_entitlements(self, scopes: Iterable[str], plan: str) -> None:
        unknown = sorted(set(scopes) - set(self.spec.scopes))
        if unknown:
            raise _err(422, "unknown_scope", f"unknown scopes {unknown}", allowed=sorted(self.spec.scopes))
        if plan not in self.plans:
            raise _err(422, "unknown_plan", f"unknown plan {plan!r}", allowed=sorted(self.plans))

    async def issue(self, principal: str, scopes: list[str], plan: str = "standard", label: str = "",
                    ttl: timedelta | None = None) -> tuple[str, dict[str, Any]]:
        self._check_entitlements(scopes, plan)
        assert self.db is not None
        key_id = secrets.token_hex(4)
        while key_id in self._rows:
            key_id = secrets.token_hex(4)
        secret = secrets.token_urlsafe(24).replace("_", "x").replace("-", "y")
        values = {"id": key_id, "secret_hash": _hash(secret), "principal": principal, "scopes": sorted(set(scopes)),
                  "plan": plan, "label": label, "bootstrap": False, "status": "ACTIVE", "created_at": _now(),
                  "expires_at": _now() + ttl if ttl else None, "revoked_at": None}
        async with self.db.engine.begin() as conn:
            await conn.execute(self.table.insert().values(**values))
        self._rows[key_id] = values
        return self._format(key_id, secret), values

    async def revoke(self, key_id: str) -> dict[str, Any]:
        self._get(key_id)
        return await self._write(key_id, status="REVOKED", revoked_at=_now())

    async def change(self, key_id: str, scopes: list[str] | None, plan: str | None) -> dict[str, Any]:
        row = self._get(key_id)
        self._check_entitlements(scopes if scopes is not None else row["scopes"], plan or row["plan"])
        values: dict[str, Any] = {}
        if scopes is not None:
            values["scopes"] = sorted(set(scopes))
        if plan:
            values["plan"] = plan
        return await self._write(key_id, **values) if values else row

    async def rotate(self, key_id: str, grace: timedelta) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """A new key with the same entitlements; the old one keeps working for ``grace``, then expires."""
        row = self._get(key_id)
        if not self._active(row):
            raise _err(409, "inactive_key", f"key {key_id} is revoked or expired")
        new_key, new_row = await self.issue(row["principal"], list(row["scopes"]), row["plan"],
                                            row["label"] or f"rotated from {key_id}")
        expires = _now() + grace
        if row["expires_at"] is None or row["expires_at"] > expires:
            row = await self._write(key_id, expires_at=expires)
        return new_key, new_row, row

    def set_plan(self, plan: Plan) -> None:
        self.plans[plan.name] = plan
        self._buckets.clear()

    def reset_plans(self) -> None:
        self.plans = dict(self.spec.plans)
        self._buckets.clear()

    def _get(self, key_id: str) -> dict[str, Any]:
        row = self._rows.get(key_id)
        if row is None:
            raise _err(404, "unknown_key", f"no key {key_id}")
        return row

    def describe(self, row: dict[str, Any]) -> dict[str, Any]:
        def ts(v: datetime | None) -> str | None:
            return v.isoformat() if v else None

        return {
            "keyId": row["id"], "principal": row["principal"], "scopes": list(row["scopes"]), "plan": row["plan"],
            "label": row["label"], "bootstrap": row["bootstrap"],
            "status": "EXPIRED" if row["status"] == "ACTIVE" and not self._active(row) else row["status"],
            "createdAt": ts(row["created_at"]), "expiresAt": ts(row["expires_at"]),
            "revokedAt": ts(row["revoked_at"]), "hint": self._format(row["id"], "…"),
            "usage": self._usage.get(row["id"], _Usage()).to_json(),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.describe(r) for r in sorted(self._rows.values(), key=lambda r: (r["principal"], r["created_at"]))]

    def catalog(self) -> dict[str, Any]:
        return {"service": self.service, "keyMode": self.mode, "scopes": self.spec.scopes,
                "plans": {n: p.to_json() for n, p in self.plans.items()}}

    # -- the gate --------------------------------------------------------------------------------
    @staticmethod
    def _active(row: dict[str, Any]) -> bool:
        return row["status"] == "ACTIVE" and (row["expires_at"] is None or row["expires_at"] > _now())

    @staticmethod
    def presented(headers: Any, query: Any) -> str | None:
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
        for h in HEADERS:
            if headers.get(h):
                return str(headers[h]).strip()
        for q in QUERY_PARAMS:
            if query.get(q):
                return str(query[q]).strip()
        return None

    def check(self, key: str | None, scopes: Iterable[str] = ()) -> tuple[Principal, dict[str, str]]:
        """Authenticate, authorise and meter one call. Raises 401/403/429; returns rate-limit headers."""
        challenge = {"WWW-Authenticate": f'Bearer realm="{self.service}"'}
        if not key:
            raise _err(401, "unauthenticated", "missing API key", challenge)
        parts = key.split("_")
        row = self._rows.get(parts[2]) if len(parts) == 4 and parts[:2] == ["mk", self.service] else None
        if row is None or not hmac.compare_digest(row["secret_hash"], _hash(parts[3])):
            raise _err(401, "unauthenticated", "unknown API key", challenge)
        if not self._active(row):
            state = "revoked" if row["status"] == "REVOKED" else "expired"
            raise _err(401, "unauthenticated", f"API key {row['id']} is {state}", challenge)
        missing = sorted(set(scopes) - set(row["scopes"]))
        usage = self._usage.setdefault(row["id"], _Usage())
        if missing:
            usage.denied += 1
            raise _err(403, "forbidden", f"key {row['id']} lacks {', '.join(missing)}", missingScopes=missing,
                       grantedScopes=list(row["scopes"]))
        plan = self.plans.get(row["plan"], DEFAULT_PLANS["trial"])
        day = _now().date().isoformat()
        if usage.day != day:
            usage.day, usage.today = day, 0
        reset_s = math.ceil((datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=1) - _now())
                            .total_seconds())
        if plan.daily_quota is not None and usage.today >= plan.daily_quota:
            usage.denied += 1
            raise _err(429, "quota_exceeded", f"daily quota of {plan.daily_quota} used", {
                "Retry-After": str(reset_s), "X-RateLimit-Limit": str(plan.daily_quota),
                "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_s)})
        bucket = self._buckets.get(row["id"])
        if bucket is None or bucket.plan != plan:
            bucket = self._buckets[row["id"]] = _Bucket(plan)
        wait = bucket.take()
        if wait:
            usage.denied += 1
            raise _err(429, "rate_limited", f"rate limit of {plan.rate_per_s:g}/s exceeded",
                       {"Retry-After": str(max(math.ceil(wait), 1))})
        usage.today += 1
        usage.total += 1
        usage.last_used = time.time()
        headers = {}
        if plan.daily_quota is not None:
            headers = {"X-RateLimit-Limit": str(plan.daily_quota),
                       "X-RateLimit-Remaining": str(plan.daily_quota - usage.today),
                       "X-RateLimit-Reset": str(reset_s)}
        p = Principal(row["id"], row["principal"], frozenset(row["scopes"]), row["plan"], row["label"])
        return p, headers

    def require(self, *scopes: str) -> Callable[..., Any]:
        """FastAPI dependency: the calling principal, entitled to every one of ``scopes``."""
        unknown = set(scopes) - set(self.spec.scopes)
        assert not unknown, f"{self.service}: route requires undeclared scopes {unknown}"

        async def dep(request: Request, response: Response) -> Principal:
            principal, headers = self.check(self.presented(request.headers, request.query_params), scopes)
            response.headers.update(headers)
            request.state.principal = principal
            return principal

        return dep

    async def websocket(self, ws: WebSocket, *scopes: str) -> Principal | None:
        """Gate a WebSocket before accepting it: closes with 4401/4403/4429 and returns None on refusal."""
        try:
            return self.check(self.presented(ws.headers, ws.query_params), scopes)[0]
        except HTTPException as e:
            await ws.close(code=4000 + e.status_code)
            return None
