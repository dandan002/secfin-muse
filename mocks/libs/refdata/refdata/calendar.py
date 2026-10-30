"""Business-day arithmetic with weekends and a small set of 2026-27 market holidays."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from mockcore.clock import NEW_YORK

MARKET_CLOSE = time(16, 0)  # New York; a locate is good until the close of the day it's granted

HOLIDAYS = {
    "US": {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15"},
    "GB": {"2026-12-25", "2026-12-28", "2027-01-01"},
    "DE": {"2026-12-24", "2026-12-25", "2026-12-31", "2027-01-01"},
    "FR": {"2026-12-25", "2027-01-01"},
    "NL": {"2026-12-25", "2027-01-01"},
    "JP": {"2026-11-03", "2026-11-23", "2026-12-31", "2027-01-01", "2027-01-11"},
}


def is_business_day(d: date, country: str = "US") -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS.get(country, set())


def add_business_days(d: date, n: int, country: str = "US") -> date:
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if is_business_day(d, country):
            remaining -= 1
    return d


def trade_date(ts: datetime) -> date:
    return ts.astimezone(NEW_YORK).date()


def settle_date(ts: datetime, country: str = "US", lag: int = 1) -> date:
    return add_business_days(trade_date(ts), lag, country)


def close_at(d: date) -> datetime:
    """The US market close on ``d``, New York time."""
    return datetime.combine(d, MARKET_CLOSE, tzinfo=NEW_YORK)


def locate_expiry(ts: datetime) -> datetime:
    """A locate granted at ``ts`` is good until today's close, or the next business day's if granted after it."""
    d = trade_date(ts)
    if is_business_day(d) and ts < close_at(d):
        return close_at(d)
    return close_at(add_business_days(d, 1))
