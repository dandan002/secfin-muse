"""Live market (spec module 2): venue availability, last prints and the desk's live availability ledger."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, secmaster
from ..models import Availability, Instrument, Locate, Print
from ..settings import get_settings

INTERNAL = "INTERNAL"  # our own books, netted (see services/inventory.py)

HELD = ("APPROVED", "PARTIAL")  # locate states that still hold shares against availability


async def upsert_availability(s: AsyncSession, source: str, rows: list[dict[str, Any]]) -> int:
    """``rows``: Venue A's aggregated availability (instrumentId, quantity, lenders, bestFeeBps, asOf)."""
    have = {a.instrument_id: a for a in (await s.execute(select(Availability).where(Availability.source == source)))
            .scalars()}
    now = clock.now()
    for r in rows:
        a = have.get(r["instrumentId"])
        if a is None:
            a = Availability(instrument_id=r["instrumentId"], source=source, qty=0, lenders=0, as_of=now)
            s.add(a)
        a.qty, a.lenders, a.indicative_fee_bps = int(r["quantity"]), int(r.get("lenders") or 0), r.get("bestFeeBps")
        a.as_of = clock.parse_ts(r["asOf"]) if r.get("asOf") else now
    return len(rows)


async def add_prints(s: AsyncSession, venue: str, prints: list[dict[str, Any]]) -> int:
    seen = set((await s.execute(select(Print.venue_ref).where(Print.venue == venue))).scalars())
    n = 0
    for p in prints:
        ref = f"{p['instrumentId']}@{p['ts']}:{p['quantity']}"
        if ref in seen:
            continue
        seen.add(ref)
        s.add(Print(venue=venue, venue_ref=ref, instrument_id=p["instrumentId"], qty=int(p["quantity"]),
                    fee_bps=float(p["feeBps"]), ts=clock.parse_ts(p["ts"])))
        n += 1
    return n


async def held(s: AsyncSession, instrument_ids: list[str] | None = None) -> dict[str, int]:
    """Shares promised to approved locates that haven't been used, released or expired."""
    q = (select(Locate.instrument_id, func.sum(Locate.qty_approved - Locate.qty_used))
         .where(Locate.status.in_(HELD), Locate.expires_at > clock.now()).group_by(Locate.instrument_id))
    if instrument_ids is not None:
        q = q.where(Locate.instrument_id.in_(instrument_ids))
    return {k: int(v or 0) for k, v in (await s.execute(q)).all()}


async def supply(s: AsyncSession, instrument_ids: list[str] | None = None) -> dict[str, list[Availability]]:
    q = select(Availability)
    if instrument_ids is not None:
        q = q.where(Availability.instrument_id.in_(instrument_ids))
    out: dict[str, list[Availability]] = defaultdict(list)
    for a in (await s.execute(q)).scalars():
        out[a.instrument_id].append(a)
    return out


def cross(sup: list[Availability], held_qty: int) -> dict[str, int]:
    """Cross first: what approved locates hold comes out of our own inventory before street supply."""
    internal = sum(a.qty for a in sup if a.source == INTERNAL)
    street = sum(a.qty for a in sup if a.source != INTERNAL)
    from_internal = min(held_qty, internal)
    return {"internal": internal - from_internal, "street": max(street - (held_qty - from_internal), 0)}


async def split(s: AsyncSession, instrument_id: str) -> dict[str, int]:
    """Live availability by kind: ``internal`` (our books, firm) and ``street`` (venue feeds, indicative)."""
    rows = (await supply(s, [instrument_id])).get(instrument_id, [])
    return cross(rows, (await held(s, [instrument_id])).get(instrument_id, 0))


async def available(s: AsyncSession, instrument_id: str) -> int:
    """Live availability: every source's supply less what approved locates already hold."""
    rows = (await supply(s, [instrument_id])).get(instrument_id, [])
    return max(sum(a.qty for a in rows) - (await held(s, [instrument_id])).get(instrument_id, 0), 0)


async def availability_fresh(s: AsyncSession) -> bool:
    """Has any availability arrived recently enough to treat a missing name as having none?"""
    latest = (await s.execute(select(func.max(Availability.as_of)).where(Availability.source != INTERNAL))).scalar()
    if latest is None:
        return False
    if latest.tzinfo is None:  # SQLite drops the zone; stored in UTC
        latest = latest.replace(tzinfo=UTC)
    return (clock.now() - latest).total_seconds() <= get_settings().availability_fresh_s


async def prints_for(s: AsyncSession, instrument_id: str, limit: int = 5) -> list[Print]:
    q = select(Print).where(Print.instrument_id == instrument_id).order_by(Print.ts.desc()).limit(limit)
    return list((await s.execute(q)).scalars())


async def last_fee(s: AsyncSession, instrument_id: str) -> float | None:
    p = await prints_for(s, instrument_id, 1)
    return p[0].fee_bps if p else None


async def trend(s: AsyncSession, instrument_ids: list[str], days: int = 5) -> dict[str, list[float]]:
    since = clock.now() - timedelta(days=days)
    q = (select(Print.instrument_id, Print.fee_bps).where(Print.instrument_id.in_(instrument_ids), Print.ts >= since)
         .order_by(Print.ts))
    out: dict[str, list[float]] = defaultdict(list)
    for iid, fee in (await s.execute(q)).all():
        out[iid].append(fee)
    return out


def _row(i: Instrument, sup: list[Availability], held_qty: int, prints: list[float]) -> dict[str, Any]:
    lv = secmaster.live(i.id)
    by_src = {a.source: a for a in sup}
    total = sum(a.qty for a in sup)
    best = min((a.indicative_fee_bps for a in sup if a.indicative_fee_bps is not None and a.qty > 0
                and a.source != INTERNAL), default=None)
    return {"instrumentId": i.id, "composite": i.composite, "name": i.name, "isin": i.isin, "sedol": i.sedol,
            "country": i.country, "ccy": i.ccy, **lv, "etb": secmaster.is_etb(i, lv["tier"]),
            "thresholdFlag": i.threshold_flag, "restricted": i.restricted,
            "venueA": {"qty": by_src["VENUE_A"].qty, "fee": by_src["VENUE_A"].indicative_fee_bps,
                       "lenders": by_src["VENUE_A"].lenders} if "VENUE_A" in by_src else None,
            "venueB": {"qty": by_src["VENUE_B"].qty, "fee": by_src["VENUE_B"].indicative_fee_bps}
            if "VENUE_B" in by_src else None,
            "internal": {"qty": by_src[INTERNAL].qty, "books": by_src[INTERNAL].lenders,
                         "costBps": by_src[INTERNAL].indicative_fee_bps} if INTERNAL in by_src else None,
            "supply": total, "held": held_qty, "available": max(total - held_qty, 0),
            "availableBy": cross(sup, held_qty), "bestFeeBps": best,
            "lastFeeBps": prints[-1] if prints else None, "trend": prints[-20:]}


async def grid(s: AsyncSession, *, tier: str | None = None, q: str | None = None, only_supplied: bool = True,
               ids: list[str] | None = None, limit: int = 300) -> list[dict[str, Any]]:
    stmt = select(Instrument)
    if ids:
        stmt = stmt.where(Instrument.id.in_(ids))
    elif only_supplied:
        stmt = stmt.where(Instrument.id.in_(select(Availability.instrument_id).where(Availability.qty > 0)))
    if q:
        like = f"%{q.upper()}%"
        stmt = stmt.where(Instrument.composite.like(like) | func.upper(Instrument.name).like(like))
    insts = list((await s.execute(stmt.order_by(Instrument.composite).limit(limit * 3 if tier else limit))).scalars())
    iids = [i.id for i in insts]
    sup, hld, tr = await supply(s, iids), await held(s, iids), await trend(s, iids)
    rows = [_row(i, sup.get(i.id, []), hld.get(i.id, 0), tr.get(i.id, [])) for i in insts]
    if tier:
        rows = [r for r in rows if r["tier"] == tier.upper()]
    return rows[:limit]


async def market_for(s: AsyncSession, inst: Instrument) -> dict[str, Any]:
    row = (await grid(s, ids=[inst.id], only_supplied=False))[0]
    row["prints"] = [{"venue": p.venue, "qty": p.qty, "feeBps": p.fee_bps, "ts": clock.iso(p.ts)}
                     for p in await prints_for(s, inst.id, 5)]
    if row["internal"]:
        from .inventory import for_instrument

        row["internal"]["byBook"] = await for_instrument(s, inst.id)
    return row
