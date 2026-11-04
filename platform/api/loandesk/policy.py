"""Desk policy the trader sets at run time: the autopilot's limits, and the order counterparties are worked in.

Settings (``LOANDESK_AUTOPILOT_*``, ``LOANDESK_DANGER_*``) are where a desk starts; whatever the desk sets here
wins from then on, survives a restart (one row per key in ``desk_settings``) and is an ``policy`` event in the
audit trail.

Counterparty priority is two ordered lists of counterparty codes. Those listed go first, in the listed order;
the rest follow in the desk's usual order:

* ``returns``: whose borrows we give back first when the box is over the shorts (then the dearest fee).
* ``recalls``: whose loans of ours we recall first to cover a short (then the cheapest loan).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from . import clock, events
from .models import DeskSetting
from .settings import get_settings

ENTITY = "policy"
Lists = Literal["returns", "recalls"]
LISTS: tuple[Lists, ...] = ("returns", "recalls")


class Limits(BaseModel):
    """One mode's envelope. Kinds: None means every kind the gate executes."""

    kinds: list[str] | None = None
    maxCardUsd: float = Field(..., ge=0)
    dayUsd: float = Field(..., ge=0)
    dayCount: int = Field(..., ge=0)
    minConfidence: float = Field(0.0, ge=0, le=1)
    counterTolerancePct: float = Field(0.0, ge=0, le=100)  # danger: take a lender's counter within this


class Priority(BaseModel):
    returns: list[str] = []
    recalls: list[str] = []


def _defaults() -> dict[str, Any]:
    cfg = get_settings()
    return {
        "autopilot": Limits(kinds=list(cfg.autopilot_kinds), maxCardUsd=cfg.autopilot_max_card_usd,
                            dayUsd=cfg.autopilot_day_usd, dayCount=cfg.autopilot_day_count,
                            minConfidence=cfg.autopilot_min_confidence).model_dump(),
        "danger": Limits(kinds=None, maxCardUsd=cfg.danger_max_card_usd, dayUsd=cfg.danger_day_usd,
                         dayCount=cfg.danger_day_count,
                         counterTolerancePct=cfg.danger_counter_tolerance_pct).model_dump(),
        "breaker": {"tripAfter": cfg.autopilot_trip_after},
        "priority": Priority().model_dump(),
    }


_set: dict[str, dict[str, Any]] = {}  # what the desk set, over the defaults; loaded at startup
_meta: dict[str, dict[str, Any]] = {}


def get(key: str) -> dict[str, Any]:
    return _set.get(key) or _defaults()[key]


def limits(mode: str) -> Limits:
    return Limits(**get(mode))


def trip_after() -> int:
    return int(get("breaker")["tripAfter"])


def priority() -> Priority:
    return Priority(**get("priority"))


def rank(which: Lists, cp: str | None) -> int:
    """Where ``cp`` stands in a priority list: its place if listed, else after everyone listed."""
    order = getattr(priority(), which)
    return order.index(cp) if cp in order else len(order)


def snapshot() -> dict[str, Any]:
    d = _defaults()
    return {k: {"value": _set.get(k) or d[k], "default": d[k], "set": k in _set, **_meta.get(k, {})} for k in d}


async def load(s: Any) -> None:
    """Read what the desk set from the database (at startup)."""
    _set.clear()
    _meta.clear()
    for r in (await s.execute(select(DeskSetting))).scalars():
        if r.key in _defaults():
            _set[r.key] = r.value
            _meta[r.key] = {"by": r.updated_by, "at": clock.iso(r.updated_at)}


async def put(s: Any, key: str, value: dict[str, Any] | None, by: str) -> dict[str, Any]:
    """Set ``key`` (None: back to the settings' default). The caller commits."""
    d = _defaults()
    if key not in d:
        raise KeyError(key)
    before = get(key)
    row = await s.get(DeskSetting, key)
    now = clock.now()
    if value is None:
        if row is not None:
            await s.delete(row)
        _set.pop(key, None)
        _meta.pop(key, None)
    else:
        if row is None:
            s.add(DeskSetting(key=key, value=value, updated_by=by, updated_at=now))
        else:
            row.value, row.updated_by, row.updated_at = value, by, now
        _set[key] = value
        _meta[key] = {"by": by, "at": clock.iso(now)}
    events.record(s, ENTITY, key, "reset" if value is None else "set", {"value": get(key)}, before={"value": before},
                  actor=by)
    return snapshot()[key]


def reset_cache() -> None:
    """Tests: forget what was loaded."""
    _set.clear()
    _meta.clear()
