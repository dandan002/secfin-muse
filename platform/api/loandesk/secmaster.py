"""Security master (spec module 1): resolve any identifier to one instrument, and show it with live data.

Reference data comes from the refdata feed and is loaded into ``instruments`` at startup. Live
fields (last price, fee, utilization) come from the market-data feed and are computed on read;
the tier is classified with the desk's own bands, not the feed's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mockcore import rng
from refdata import identifiers as ids
from refdata import market
from refdata.counterparties import counterparties
from refdata.universe import Instrument as RefInstrument
from refdata.universe import instrument_by_id, universe
from refdata.universe import resolve as ref_resolve

from . import clock
from .models import Counterparty, Instrument
from .settings import get_settings

_SHAPES = {"ISIN": re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$"), "CUSIP": re.compile(r"^[A-Z0-9]{8}\d$"),
           "SEDOL": re.compile(r"^[B-DF-HJ-NP-TV-Z0-9]{6}\d$")}


def set_seed(seed: int) -> None:
    rng.set_seed(seed)


async def load(s: AsyncSession) -> int:
    """Upsert the universe and the counterparty table from the reference feed."""
    have = set((await s.execute(select(Instrument.id))).scalars())
    n = 0
    for i in universe():
        if i.id in have:
            continue
        s.add(Instrument(id=i.id, ticker=i.ticker, mic=i.mic, composite=i.composite, name=i.name, country=i.country,
                         ccy=i.ccy, sector=i.sector, cfi=i.cfi, isin=i.isin, sedol=i.sedol, cusip=i.cusip,
                         figi=i.figi, shs_out=i.shs_out, free_float_pct=i.free_float_pct, adv_20d=i.adv_20d,
                         adv_90d=i.adv_90d, si_pct=i.si_pct, index_name=i.index, threshold_flag=i.threshold_flag,
                         restricted=i.restricted, next_ex_div=i.next_ex_div))
        n += 1
    have_cp = set((await s.execute(select(Counterparty.code))).scalars())
    for c in counterparties():
        if c.code not in have_cp:
            s.add(Counterparty(code=c.code, name=c.name, kind=c.kind, region=c.region, ngt_code=c.ngt_code,
                               wematch_entity=c.wematch_entity, lei=c.lei, bic=c.bic,
                               credit_limit_usd=c.credit_limit_usd, agreement=c.agreement,
                               collateral_schedule=c.collateral_schedule, persona=c.persona))
    await s.commit()
    return n


def classify(fee_bps: float, util_pct: float) -> str:
    cfg = get_settings()
    if fee_bps > cfg.hot_min_fee_bps or util_pct > cfg.hot_min_util_pct:
        return "HOT"
    if fee_bps < cfg.gc_max_fee_bps and util_pct < cfg.gc_max_util_pct:
        return "GC"
    return "WARM"


def live(inst_id: str) -> dict[str, Any]:
    """Market-data snapshot for one name at desk time."""
    ref = instrument_by_id(inst_id)
    snap = market.snapshot(ref, clock.now())
    tier = classify(snap["feeBps"], snap["utilPct"])
    return {"lastPx": snap["lastPx"], "feeBps": snap["feeBps"], "utilPct": snap["utilPct"], "tier": tier,
            "daysToCover": ref.days_to_cover}


def is_etb(inst: Instrument | RefInstrument, tier: str) -> bool:
    return tier == "GC" and not inst.threshold_flag and not inst.restricted


def instrument_json(i: Instrument, with_live: bool = True) -> dict[str, Any]:
    out = {"id": i.id, "ticker": i.ticker, "mic": i.mic, "composite": i.composite, "name": i.name,
           "country": i.country, "ccy": i.ccy, "sector": i.sector, "cfi": i.cfi, "isin": i.isin, "sedol": i.sedol,
           "cusip": i.cusip, "figi": i.figi, "shsOut": i.shs_out, "freeFloatPct": i.free_float_pct,
           "adv20d": i.adv_20d, "adv90d": i.adv_90d, "siPct": i.si_pct, "index": i.index_name,
           "thresholdFlag": i.threshold_flag, "restricted": i.restricted, "nextExDiv": i.next_ex_div}
    if with_live:
        lv = live(i.id)
        out.update(lv, etb=is_etb(i, lv["tier"]))
    return out


@dataclass
class Resolution:
    query: str
    id_type: str | None
    instrument: Instrument | None = None
    error: str | None = None
    suggestions: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"query": self.query, "idType": self.id_type, "error": self.error, "suggestions": self.suggestions,
                "instrument": instrument_json(self.instrument) if self.instrument else None}


def id_shape(q: str) -> str | None:
    """What an identifier looks like, before its check digit is trusted."""
    v = q.strip().upper()
    if len(v) == 12 and v.startswith("BBG"):
        return "FIGI"
    for name, rx in _SHAPES.items():
        if rx.match(v):
            return name
    return None


async def resolve(s: AsyncSession, query: str) -> Resolution:
    """Validate check digits first, so a typo gets a "did you mean" instead of a wrong name."""
    q = " ".join(query.strip().upper().split())
    if not q:
        return Resolution(query, None, error="empty identifier")
    shape = id_shape(q)
    detected = ids.detect(q)
    if shape in ("ISIN", "CUSIP", "SEDOL") and detected is None:
        hints = [c for c in ids.suggest_fix(q) if ref_resolve(c)]
        return Resolution(query, shape, error=f"bad {shape} check digit", suggestions=hints[:3])
    ref = ref_resolve(q) or (ref_resolve(f"{q} US") if " " not in q and not detected else None)
    if ref is None:
        return Resolution(query, detected or shape or "TICKER", error="not found")
    return Resolution(query, detected or ("TICKER" if " " in q or not shape else shape),
                      instrument=await s.get(Instrument, ref.id))


async def search(s: AsyncSession, q: str, limit: int = 12) -> list[Instrument]:
    """Autocomplete: exact identifier first, then ticker prefix, then name contains."""
    q = q.strip().upper()
    if not q:
        return []
    exact = ref_resolve(q)
    like = f"{q}%"
    rows = (await s.execute(
        select(Instrument).where(or_(Instrument.ticker.like(like), Instrument.composite.like(like),
                                     func.upper(Instrument.name).like(f"%{q}%"), Instrument.isin == q,
                                     Instrument.sedol == q, Instrument.cusip == q))
        .order_by(func.length(Instrument.ticker), Instrument.adv_20d.desc()).limit(limit))).scalars().all()
    if exact:
        rows = [r for r in rows if r.id != exact.id]
        head = await s.get(Instrument, exact.id)
        rows = ([head] if head else []) + list(rows)
    return list(rows)[:limit]
