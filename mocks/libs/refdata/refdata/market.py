"""Deterministic market processes: lending fee, utilization and price as functions of sim time.

The value at time t depends only on (seed, instrument, t), never on who asked first, so two
venues that both quote "AAPL US" at 09:31 see the same underlying fair fee.

* Fee: Ornstein-Uhlenbeck in log space on a 15-minute grid, re-anchored daily from the
  stationary distribution, linearly interpolated between grid points. Each name has a small
  per-day chance of a squeeze that multiplies the fee 3-8x from a random intraday start.
* Utilization drifts with the fee's deviation from its mean.
* Price: daily geometric Brownian motion from the base price (the last public close, for the public
  universe) at each name's own volatility, plus intraday 15-minute steps.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from mockcore import rng

from .universe import Instrument, classify

GRID_MIN = 15
STEPS_PER_DAY = 24 * 60 // GRID_MIN
EPOCH_DAY = date(2026, 10, 27)

THETA = 0.08  # mean reversion per grid step
SIGMA_STEP = 0.05  # log-fee noise per grid step
SIGMA_STATIONARY = SIGMA_STEP / math.sqrt(1 - (1 - THETA) ** 2)
SQUEEZE_P = {"GC": 0.001, "WARM": 0.01, "HOT": 0.04}
PX_VOL_DAILY = 0.02


def _grid(ts: datetime) -> tuple[date, int, float]:
    ts = ts.astimezone(UTC)
    day = ts.date()
    minutes = ts.hour * 60 + ts.minute + ts.second / 60 + ts.microsecond / 60e6
    step, frac = divmod(minutes / GRID_MIN, 1.0)
    return day, int(step), frac


@lru_cache(maxsize=200_000)
def _log_fee_path(seed: int, inst_id: str, mu: float, day: date) -> tuple[float, ...]:
    x = mu + SIGMA_STATIONARY * rng.hash_normal("fee0", inst_id, day.isoformat())
    path = [x]
    for k in range(1, STEPS_PER_DAY + 1):
        x = x + THETA * (mu - x) + SIGMA_STEP * rng.hash_normal("fee", inst_id, day.isoformat(), k)
        path.append(x)
    return tuple(path)


def _squeeze_mult(inst: Instrument, day: date, step: float) -> float:
    key = (inst.id, day.isoformat())
    if rng.hash_uniform("sq", *key) >= SQUEEZE_P.get(inst.tier, 0.0):
        return 1.0
    start = 13 * 4 + rng.hash_uniform("sq-start", *key) * 7 * 4  # between 13:00Z and 20:00Z
    if step < start:
        return 1.0
    peak = 3 + 5 * rng.hash_uniform("sq-mag", *key)
    ramp = min((step - start) / 4, 1.0)  # reaches full size within an hour
    return 1 + (peak - 1) * ramp


def fee_bps(inst: Instrument, ts: datetime) -> float:
    day, step, frac = _grid(ts)
    mu = math.log(inst.base_fee_bps)
    path = _log_fee_path(rng.get_seed(), inst.id, mu, day)
    x = path[step] * (1 - frac) + path[step + 1] * frac
    return round(math.exp(x) * _squeeze_mult(inst, day, step + frac), 1)


def util_pct(inst: Instrument, ts: datetime) -> float:
    fee = fee_bps(inst, ts)
    dev = math.log(fee / inst.base_fee_bps)
    return round(min(max(inst.base_util_pct + 12 * dev, 0.5), 99.9), 2)


def tier_at(inst: Instrument, ts: datetime) -> str:
    return classify(fee_bps(inst, ts), util_pct(inst, ts))


@lru_cache(maxsize=50_000)
def _day_open_log_px(seed: int, inst_id: str, base_px: float, day: date, vol: float = PX_VOL_DAILY) -> float:
    days = (day - EPOCH_DAY).days
    x = math.log(base_px)
    step = 1 if days >= 0 else -1
    for d in range(0, days, step):
        x += step * vol * rng.hash_normal("px-day", inst_id, (EPOCH_DAY + timedelta(days=d)).isoformat())
    return x


@lru_cache(maxsize=200_000)
def _log_px_path(seed: int, inst_id: str, base_px: float, day: date, vol: float = PX_VOL_DAILY) -> tuple[float, ...]:
    x = _day_open_log_px(seed, inst_id, base_px, day, vol)
    sigma = vol / math.sqrt(STEPS_PER_DAY)
    path = [x]
    for k in range(1, STEPS_PER_DAY + 1):
        x += sigma * rng.hash_normal("px", inst_id, day.isoformat(), k)
        path.append(x)
    return tuple(path)


def price(inst: Instrument, ts: datetime) -> float:
    day, step, frac = _grid(ts)
    path = _log_px_path(rng.get_seed(), inst.id, inst.base_px, day, inst.px_vol_daily)
    x = path[step] * (1 - frac) + path[step + 1] * frac
    return round(math.exp(x), 0 if inst.ccy == "JPY" else 2)


def snapshot(inst: Instrument, ts: datetime) -> dict:
    fee = fee_bps(inst, ts)
    util = util_pct(inst, ts)
    return {
        "instrumentId": inst.id,
        "lastPx": price(inst, ts),
        "feeBps": fee,
        "utilPct": util,
        "tier": classify(fee, util),
    }
