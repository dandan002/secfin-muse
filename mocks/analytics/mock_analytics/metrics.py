"""Short-interest and lending-market analytics as pure functions of (seed, instrument, sim time).

Built on the same fee, utilization and price processes the venues quote (``refdata.market``), so
a squeeze the analytics vendor flags is the same squeeze Venue A and Venue B price.

* Short interest: the instrument's base % of shares outstanding times a slow AR(1) drift (a
  30-day truncated sum of daily shocks, so any day is computable on its own), pushed up when the
  borrow fee runs above its mean.
* Lendable supply is backed out from on-loan shares (about the short interest) and utilization,
  capped at the free float.
* Crowding (0-100) blends short interest as % of float, days to cover and utilization; squeeze
  risk scales crowding by how far the fee has run above its mean.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

from mockcore import rng
from refdata import calendar, market
from refdata.universe import Instrument

AR = 0.85  # daily persistence of the short-interest drift
SI_SHOCK = 0.04  # log short-interest noise per day
AR_DAYS = 30
FEE_COUPLING = 0.10  # log SI per log(fee / base fee) above the mean
DATA_CUT = time(21, 0, tzinfo=UTC)  # the vendor's daily close, after the US close
CTB_BANDS = (25, 50, 100, 200, 300, 500, 1000, 2000, 4000)  # cost-to-borrow score 1..10 by fee bps


def close_ts(d: date) -> datetime:
    return datetime.combine(d, DATA_CUT)


def data_date(ts: datetime) -> date:
    """The latest daily close at or before ``ts``."""
    d = ts.astimezone(UTC).date()
    return d if ts >= close_ts(d) else d - timedelta(days=1)


@lru_cache(maxsize=200_000)
def _drift(seed: int, inst_id: str, day: date) -> float:
    return sum(AR**k * SI_SHOCK * rng.hash_normal("si", inst_id, (day - timedelta(days=k)).isoformat())
               for k in range(AR_DAYS))


def si_pct(inst: Instrument, ts: datetime) -> float:
    """Short interest as % of shares outstanding at ``ts`` (intraday estimate)."""
    fee = market.fee_bps(inst, ts)
    run_up = max(math.log(fee / inst.base_fee_bps), 0.0)
    x = _drift(rng.get_seed(), inst.id, ts.astimezone(UTC).date()) + FEE_COUPLING * run_up
    return round(min(inst.si_pct * math.exp(x), 95.0), 3)


def ctb_score(fee_bps: float) -> int:
    return 1 + sum(fee_bps >= b for b in CTB_BANDS)


def float_shares(inst: Instrument) -> int:
    return int(inst.shs_out * inst.free_float_pct / 100)


def _pct_change(now: float, before: float) -> float | None:
    return round((now / before - 1) * 100, 2) if before else None


def snapshot(inst: Instrument, ts: datetime) -> dict:
    """Everything the vendor publishes for one name at ``ts``."""
    fee = market.fee_bps(inst, ts)
    util = market.util_pct(inst, ts)
    px = market.price(inst, ts)
    si = si_pct(inst, ts)
    si_shares = int(inst.shs_out * si / 100)
    flt = float_shares(inst)
    on_loan = int(si_shares * (0.95 + 0.1 * rng.hash_uniform("on-loan", inst.id)))
    lendable = min(int(on_loan / (util / 100)), flt)
    on_loan = min(on_loan, lendable)
    dtc = si_shares / max(inst.adv_20d, 1)
    pct_float = si_shares / flt * 100 if flt else 0.0
    crowding = 100 * (0.4 * min(pct_float / 40, 1) + 0.3 * min(dtc / 10, 1) + 0.3 * util / 100)
    run_up = min(max(math.log(fee / inst.base_fee_bps) / math.log(4), 0.0), 1.0)
    squeeze = crowding * (0.5 + 0.5 * run_up)
    day = ts.astimezone(UTC).date()
    prior = {n: si_pct(inst, close_ts(day - timedelta(days=n))) for n in (1, 7, 30)}
    return {
        "isin": inst.isin,
        "ticker": inst.ticker,
        "composite": inst.composite,
        "price": px,
        "ccy": inst.ccy,
        "shortInterest": {
            "shares": si_shares,
            "pctSharesOut": round(si, 2),
            "pctFloat": round(pct_float, 2),
            "notional": round(si_shares * px, 0),
            "daysToCover": round(dtc, 2),
            "change1dPct": _pct_change(si, prior[1]),
            "change7dPct": _pct_change(si, prior[7]),
            "change30dPct": _pct_change(si, prior[30]),
            "source": "ESTIMATED",
        },
        "borrow": {
            "indicativeFeeBps": fee,
            "utilizationPct": util,
            "lendableShares": lendable,
            "onLoanShares": on_loan,
            "availableShares": lendable - on_loan,
            "costToBorrowScore": ctb_score(fee),
            "tier": market.tier_at(inst, ts),
        },
        "crowdingScore": round(crowding, 1),
        "squeezeRisk": round(squeeze, 1),
        "thresholdList": inst.threshold_flag,
    }


# -- exchange-reported short interest (FINRA-style, US only) ---------------------------------------
PUBLISH_LAG_BDAYS = 7


def settlement_dates(around: date) -> list[date]:
    """Mid-month and month-end reporting settlement dates (moved back to a business day) near ``around``."""
    out = []
    for m_off in (-2, -1, 0, 1):
        y, m = divmod(around.month - 1 + m_off, 12)
        first = date(around.year + y, m + 1, 1)
        nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        for d in (first.replace(day=15), nxt - timedelta(days=1)):
            while not calendar.is_business_day(d, "US"):
                d -= timedelta(days=1)
            out.append(d)
    return sorted(out)


def publish_date(settlement: date) -> date:
    return calendar.add_business_days(settlement, PUBLISH_LAG_BDAYS, "US")


def latest_published(today: date) -> date:
    return max(d for d in settlement_dates(today) if publish_date(d) <= today)


def exchange_report(inst: Instrument, settlement: date) -> dict:
    """Reported short position on a settlement date: the vendor's estimate plus reporting noise."""
    est = si_pct(inst, close_ts(settlement))
    reported = int(inst.shs_out * est / 100 * (1 + 0.04 * rng.hash_normal("exch-si", inst.id, settlement)))
    prev = max(d for d in settlement_dates(settlement) if d < settlement)
    prev_est = si_pct(inst, close_ts(prev))
    prev_reported = int(inst.shs_out * prev_est / 100 * (1 + 0.04 * rng.hash_normal("exch-si", inst.id, prev)))
    return {
        "isin": inst.isin,
        "ticker": inst.ticker,
        "settlementDate": settlement.isoformat(),
        "publishDate": publish_date(settlement).isoformat(),
        "currentShortPosition": reported,
        "previousShortPosition": prev_reported,
        "changePct": _pct_change(reported, prev_reported),
        "avgDailyVolume": inst.adv_20d,
        "daysToCover": round(reported / max(inst.adv_20d, 1), 2),
        "source": "EXCHANGE_REPORTED",
    }
