"""Corporate actions that make lenders recall: dividends and shareholder meetings, deterministic per seed.

A lender who wants the dividend itself rather than the borrower's manufactured payment (taxed differently, or
not at all under a treaty), or wants to vote its shares, recalls them so they're back by the record date. Every
system derives the same calendar from (seed, instrument), so the venues, the processor and the desk agree:

* ``DIVIDEND``: each name with a ``next_ex_div``. Under T+1 in the US the record date is the ex-date; elsewhere
  it's the business day after.
* ``AGM``: about 4% of names hold an annual meeting with a record date in the next six weeks.
* ``SPECIAL_MEETING``: about 1% hold a contested vote (a merger, a proxy fight): lenders recall most for these.

``recall_p`` is the chance a given lender recalls a given borrow for the event. Lenders send the notice
``NOTICE_DAYS`` business days before the record date, due back the business day before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from mockcore import rng

from . import calendar
from .universe import Instrument, universe

WINDOW_START = date(2026, 10, 28)
WINDOW_DAYS = 42
NOTICE_DAYS = 3
RECALL_P = {"DIVIDEND": 0.2, "AGM": 0.15, "SPECIAL_MEETING": 0.6}


@dataclass(frozen=True)
class CorpAction:
    event_id: str
    instrument_id: str
    kind: str  # DIVIDEND | AGM | SPECIAL_MEETING
    record_date: date
    ex_date: date | None  # dividends
    amount: float | None  # dividend per share, in the listing currency
    ccy: str
    country: str
    recall_p: float

    @property
    def notice_date(self) -> date:
        return calendar.add_business_days(self.record_date, -NOTICE_DAYS, self.country)

    @property
    def due_date(self) -> date:
        return calendar.add_business_days(self.record_date, -1, self.country)

    def to_json(self) -> dict[str, Any]:
        return {"eventId": self.event_id, "instrumentId": self.instrument_id, "kind": self.kind,
                "recordDate": self.record_date.isoformat(),
                "exDate": self.ex_date.isoformat() if self.ex_date else None,
                "amount": self.amount, "ccy": self.ccy, "recallP": self.recall_p,
                "noticeDate": self.notice_date.isoformat(), "dueDate": self.due_date.isoformat()}


def _on_business_day(d: date, country: str) -> date:
    while not calendar.is_business_day(d, country):
        d += timedelta(days=1)
    return d


def _events(inst: Instrument) -> list[CorpAction]:
    out = []
    if inst.next_ex_div:
        ex = _on_business_day(date.fromisoformat(inst.next_ex_div), inst.country)
        rec = ex if inst.country == "US" else calendar.add_business_days(ex, 1, inst.country)
        yld = 0.005 + 0.03 * rng.hash_uniform("ca-yield", inst.id)  # 0.5-3.5% a year, paid quarterly
        amount = max(round(inst.base_px * yld / 4, 6), 0.0001)
        out.append(CorpAction(f"CA-{inst.id}-D", inst.id, "DIVIDEND", rec, ex, amount, inst.ccy, inst.country,
                              RECALL_P["DIVIDEND"]))
    if not inst.is_etf:
        u = rng.hash_uniform("ca-meeting", inst.id)
        kind = "SPECIAL_MEETING" if u < 0.01 else "AGM" if u < 0.05 else None
        if kind:
            day = WINDOW_START + timedelta(days=3 + int(rng.hash_uniform("ca-meeting-day", inst.id) * WINDOW_DAYS))
            out.append(CorpAction(f"CA-{inst.id}-{'S' if kind == 'SPECIAL_MEETING' else 'M'}", inst.id, kind,
                                  _on_business_day(day, inst.country), None, None, inst.ccy, inst.country,
                                  RECALL_P[kind]))
    return out


@lru_cache(maxsize=8)
def _calendar(seed: int) -> tuple[CorpAction, ...]:
    return tuple(sorted((e for i in universe(seed) for e in _events(i)), key=lambda e: (e.record_date, e.event_id)))


def corporate_actions(start: date | None = None, end: date | None = None, instrument_ids: set[str] | None = None,
                      seed: int | None = None) -> list[CorpAction]:
    """Events with a record date in [start, end], oldest first (optionally for some names only)."""
    rows = _calendar(rng.get_seed() if seed is None else seed)
    return [e for e in rows if (start is None or e.record_date >= start) and (end is None or e.record_date <= end)
            and (instrument_ids is None or e.instrument_id in instrument_ids)]


def event_by_id(event_id: str, seed: int | None = None) -> CorpAction | None:
    return next((e for e in _calendar(rng.get_seed() if seed is None else seed) if e.event_id == event_id), None)


def recalls(event: CorpAction, loan_ref: str) -> bool:
    """Does the lender on this loan recall it for the event? Deterministic per (seed, event, loan)."""
    return rng.hash_uniform("ca-recall", event.event_id, loan_ref) < event.recall_p
