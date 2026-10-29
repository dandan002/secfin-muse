"""Counterparty limits: how much more the desk can promise or lend to each counterparty.

A counterparty's exposure is its booked trades plus the locates we have approved and it hasn't used: an
approved locate is shares we've promised, so it counts against the same credit line. The desk's own
limits sit on top of the credit limit from reference data:

* credit limit: trades + outstanding locates + this request, in USD;
* locate limit: outstanding locates alone, as a share of the credit limit (``cp_locate_limit_pct``);
* per-name cap: one counterparty's outstanding locates in one name (``locate_name_cap``);
* blocked list: counterparties the desk won't deal with at all (``blocked_counterparties``).

Internal desks (kind ``INTERNAL``) are the same firm: no credit or locate limit, only the per-name cap.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, rules, secmaster
from ..models import Counterparty, Instrument, Locate, Trade
from ..settings import get_settings
from .market import HELD


def price_usd(inst: Instrument) -> float:
    return secmaster.live(inst.id)["lastPx"] * rules.FX_USD.get(inst.ccy, 1.0)


@dataclass
class Book:
    """One counterparty's current usage."""

    trades_usd: float = 0.0
    locates_usd: float = 0.0
    name_qty: dict[str, int] = field(default_factory=dict)  # outstanding locate shares per instrument


async def books(s: AsyncSession, cp_codes: list[str] | None = None) -> dict[str, Book]:
    """Usage for every counterparty (or ``cp_codes``) in two queries."""
    out: dict[str, Book] = defaultdict(Book)
    tq = select(Trade.cp_code, Trade.qty, Trade.price, Trade.ccy)
    lq = (select(Locate.requester_cp, Locate.instrument_id, func.sum(Locate.qty_approved - Locate.qty_used))
          .where(Locate.status.in_(HELD), Locate.expires_at > clock.now(), Locate.instrument_id.is_not(None))
          .group_by(Locate.requester_cp, Locate.instrument_id))
    if cp_codes is not None:
        tq, lq = tq.where(Trade.cp_code.in_(cp_codes)), lq.where(Locate.requester_cp.in_(cp_codes))
    for code, qty, px, ccy in (await s.execute(tq)).all():
        out[code].trades_usd += qty * px * rules.FX_USD.get(ccy, 1.0)
    rows = (await s.execute(lq)).all()
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(
        Instrument.id.in_({iid for _, iid, _ in rows})))).scalars()} if rows else {}
    for code, iid, qty in rows:
        qty = int(qty or 0)
        out[code].name_qty[iid] = qty
        if iid in insts:
            out[code].locates_usd += qty * price_usd(insts[iid])
    return out


def checks(cp: Counterparty | None, inst: Instrument, qty: int, book: Book) -> tuple[list[rules.Check], int]:
    """The limit checks for promising ``qty`` more of ``inst`` to ``cp``, and the most that would pass."""
    cfg = get_settings()
    if cp is None:
        return [rules.Check("counterparty", False, "unknown counterparty")], 0
    px = price_usd(inst)
    held_here = book.name_qty.get(inst.id, 0)
    out = [rules.Check("counterparty blocked", cp.code not in {c.upper() for c in cfg.blocked_counterparties},
                       f"{cp.code} is on the desk's blocked list")]
    room = [max(cfg.locate_name_cap - held_here, 0)]
    out.append(rules.Check("per-name cap", held_here + qty <= cfg.locate_name_cap,
                           f"{held_here + qty:,} of {cfg.locate_name_cap:,} in this name for {cp.code}"))
    if cp.kind != "INTERNAL":
        used = book.trades_usd + book.locates_usd
        credit_room = cp.credit_limit_usd - used
        out.append(rules.Check("credit limit", qty * px <= credit_room,
                               f"${used + qty * px:,.0f} of ${cp.credit_limit_usd:,.0f} "
                               f"(trades ${book.trades_usd:,.0f}, locates ${book.locates_usd:,.0f})"))
        locate_cap = cp.credit_limit_usd * cfg.cp_locate_limit_pct / 100
        locate_room = locate_cap - book.locates_usd
        out.append(rules.Check("locate limit", qty * px <= locate_room,
                               f"${book.locates_usd + qty * px:,.0f} of ${locate_cap:,.0f} outstanding locates "
                               f"({cfg.cp_locate_limit_pct:g}% of credit)"))
        room += [int(max(credit_room, 0) // px) if px else 0, int(max(locate_room, 0) // px) if px else 0]
    if not out[0].ok:
        room.append(0)
    return out, min(room)


async def for_line(s: AsyncSession, cp: Counterparty | None, inst: Instrument, qty: int
                   ) -> tuple[list[rules.Check], int]:
    book = (await books(s, [cp.code])).get(cp.code, Book()) if cp else Book()
    return checks(cp, inst, qty, book)


def summary(cp: Counterparty | None, inst: Instrument, qty: int, book: Book) -> dict[str, Any]:
    """What the reviewer (and the locate card) sees about this counterparty's room."""
    cs, max_qty = checks(cp, inst, qty, book)
    if cp is None:
        return {"known": False, "maxQty": 0}
    return {"code": cp.code, "kind": cp.kind, "agreement": cp.agreement, "creditLimitUsd": cp.credit_limit_usd,
            "tradesUsd": round(book.trades_usd), "locatesUsd": round(book.locates_usd),
            "inThisName": book.name_qty.get(inst.id, 0), "maxQty": max_qty,
            "failing": [f"{c.name}: {c.detail}" for c in cs if not c.ok]}
