"""Security master, counterparties and the market grid."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import secmaster
from ..db import get_session
from ..models import Counterparty, Instrument, InternalPosition
from ..services import inventory, market

router = APIRouter(tags=["reference"])


@router.get("/instruments/resolve")
async def resolve(q: str, s: AsyncSession = Depends(get_session)) -> dict:
    return (await secmaster.resolve(s, q)).to_json()


@router.get("/instruments/search")
async def search(q: str, limit: int = Query(12, le=50), s: AsyncSession = Depends(get_session)) -> list[dict]:
    return [secmaster.instrument_json(i) for i in await secmaster.search(s, q, limit)]


@router.get("/instruments/{instrument_id}/market")
async def instrument_market(instrument_id: str, s: AsyncSession = Depends(get_session)) -> dict:
    inst = await s.get(Instrument, instrument_id)
    if inst is None:
        raise HTTPException(404, "unknown instrument")
    return {"instrument": secmaster.instrument_json(inst), "market": await market.market_for(s, inst)}


@router.get("/counterparties")
async def counterparties(s: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await s.execute(select(Counterparty).order_by(Counterparty.code))).scalars()
    return [{"code": c.code, "name": c.name, "kind": c.kind, "region": c.region, "ngtCode": c.ngt_code,
             "wematchEntity": c.wematch_entity, "lei": c.lei, "creditLimitUsd": c.credit_limit_usd,
             "agreement": c.agreement, "collateralSchedule": c.collateral_schedule} for c in rows]


@router.get("/market")
async def market_grid(tier: str | None = None, q: str | None = None, ids: str | None = None,
                      limit: int = Query(300, le=2000), s: AsyncSession = Depends(get_session)) -> list[dict]:
    return await market.grid(s, tier=tier, q=q, ids=ids.split(",") if ids else None, limit=limit)


@router.get("/inventory")
async def internal_inventory(s: AsyncSession = Depends(get_session)) -> list[dict]:
    """Our books from the processor, netted per name: what we hold, what's free after locates, and by book."""
    names = list((await s.execute(select(InternalPosition.instrument_id).distinct())).scalars())
    rows = await market.grid(s, ids=names, only_supplied=False, limit=2000) if names else []
    out = []
    for r in rows:
        books = await inventory.for_instrument(s, r["instrumentId"])
        out.append({"instrumentId": r["instrumentId"], "composite": r["composite"], "isin": r["isin"],
                    "tier": r["tier"], "borrowed": sum(b["borrowed"] for b in books),
                    "lent": sum(b["lent"] for b in books), "net": sum(b["net"] for b in books),
                    "free": r["availableBy"]["internal"], "costBps": (r["internal"] or {}).get("costBps"),
                    "books": books})
    return sorted(out, key=lambda x: -x["net"])
