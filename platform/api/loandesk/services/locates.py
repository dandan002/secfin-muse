"""Locates (spec module 3): intake from any channel, rules-engine routing, decisions and replies.

Every approval decrements the live availability ledger (``market.held``) and records the basis the
desk relied on under Reg SHO 203(b). Lines the rules engine can't clear wait for a trader (or, once
it's wired, the agent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, events, rules, secmaster
from ..adapters import AdapterError, systems
from ..models import Counterparty, Instrument, Locate
from ..settings import get_settings
from . import limits, market

Decision = Literal["APPROVED", "PARTIAL", "REJECTED"]

# The ETB list the auto-approve policy relies on: rebuilt each desk day from the market-data feed.
_etb_built_at: datetime | None = None


def etb_built_at() -> datetime:
    global _etb_built_at
    now = clock.now()
    if _etb_built_at is None or clock.local_date(_etb_built_at) != clock.local_date(now):
        _etb_built_at = now
    return _etb_built_at


def etb_fresh() -> bool:
    """Rule 203(b): an ETB list only counts if it was built from data under 24 hours old."""
    return (clock.now() - etb_built_at()).total_seconds() < 24 * 3600


@dataclass
class LineIn:
    identifier: str
    quantity: int
    line_no: int | None = None


def locate_json(loc: Locate, inst: Instrument | None = None, available: int | None = None,
                by_kind: dict[str, int] | None = None, floor: dict[str, Any] | None = None,
                cp_room: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {"id": loc.id, "requestRef": loc.request_ref, "lineNo": loc.line_no, "counterparty": loc.requester_cp,
           "identifier": loc.identifier, "instrumentId": loc.instrument_id, "qtyRequested": loc.qty_req,
           "qtyApproved": loc.qty_approved, "qtyUsed": loc.qty_used, "status": loc.status, "route": loc.route,
           "routeReasons": loc.route_reasons, "tier": loc.tier_at_decision, "feeIndBps": loc.fee_ind_bps,
           "decidedBy": loc.decided_by, "rationale": loc.rationale, "channel": loc.source_channel,
           "sourceRef": loc.source_ref, "regShoBasis": loc.reg_sho_basis, "replyStatus": loc.reply_status,
           "error": loc.error, "receivedAt": clock.iso(loc.received_at), "decidedAt": clock.iso(loc.decided_at),
           "expiresAt": clock.iso(loc.expires_at)}
    if inst is not None:
        lv = secmaster.live(inst.id)
        out["instrument"] = {"composite": inst.composite, "name": inst.name, "isin": inst.isin, "ccy": inst.ccy,
                             "tier": lv["tier"], "feeBps": lv["feeBps"], "utilPct": lv["utilPct"],
                             "lastPx": lv["lastPx"], "etb": secmaster.is_etb(inst, lv["tier"])}
    if available is not None:
        out["available"] = available
        if by_kind is not None:
            out["availableBy"] = by_kind
        out["suggestedQty"] = min(loc.qty_req, available // 1000 * 1000 if available >= 1000 else available)
    if floor is not None:
        out["feeFloorBps"] = floor["floorBps"]
    if cp_room is not None:
        out["counterpartyRoom"] = cp_room
        if "suggestedQty" in out:
            out["suggestedQty"] = min(out["suggestedQty"], cp_room["maxQty"])
    return out


def price_floor(inst: Instrument, cp: Counterparty | None, rows: list[Any], held_qty: int) -> dict[str, Any]:
    """The least we lend this name at now. Crossing our own inventory, never under what it cost us."""
    internal = next((a for a in rows if a.source == market.INTERNAL), None)
    crossing = internal is not None and market.cross(rows, held_qty)["internal"] > 0
    return rules.fee_floor(secmaster.live(inst.id)["feeBps"],
                           cost_bps=internal.indicative_fee_bps if crossing and internal else None,
                           internal_cp=cp is not None and cp.kind == "INTERNAL")


async def pricing(s: AsyncSession, inst: Instrument, cp: Counterparty | None) -> dict[str, Any]:
    rows = (await market.supply(s, [inst.id])).get(inst.id, [])
    return price_floor(inst, cp, rows, (await market.held(s, [inst.id])).get(inst.id, 0))


async def intake(s: AsyncSession, *, counterparty: str, lines: list[LineIn], channel: str, request_ref: str,
                 source_ref: str | None = None, actor: str = "system") -> list[Locate]:
    """Store each line, route it, and let the standing ETB policy clear what it may."""
    cfg = get_settings()
    cp = await s.get(Counterparty, counterparty.upper())
    now = clock.now()
    fresh = await market.availability_fresh(s)
    out = []
    for n, ln in enumerate(lines, start=1):
        res = await secmaster.resolve(s, ln.identifier)
        loc = Locate(id=events.new_id("loc"), request_ref=request_ref, line_no=ln.line_no or n,
                     requester_cp=cp.code if cp else counterparty.upper(), identifier=ln.identifier,
                     qty_req=ln.quantity, status="RECEIVED", route="HOLD", route_reasons=[], source_channel=channel,
                     source_ref=source_ref, received_at=now,
                     reply_status="PENDING" if channel in ("API", "CHAT") else "N/A")
        if res.instrument is None:
            loc.status, loc.route = "INVALID", "HOLD"
            loc.error = res.error + (f"; did you mean {res.suggestions[0]}?" if res.suggestions else "")
        else:
            inst = res.instrument
            loc.instrument_id = inst.id
            lv = secmaster.live(inst.id)
            etb = secmaster.is_etb(inst, lv["tier"]) and etb_fresh()
            avail = await market.available(s, inst.id)
            loc.route, loc.route_reasons = rules.locate_route(inst, lv["tier"], etb, ln.quantity, avail, cp)
            loc.tier_at_decision = lv["tier"]
            loc.fee_ind_bps = (await pricing(s, inst, cp))["floorBps"]  # quote a bit above market
            hits = await filters(s, inst, available=avail, fresh=fresh)
            if cp is not None:
                failing = [c for c in (await limits.for_line(s, cp, inst, ln.quantity))[0] if not c.ok]
                hits += [("reject" if c.name == "counterparty blocked" else "hold", f"{c.name}: {c.detail}")
                         for c in failing]
            if any(a == "reject" for a, _ in hits):
                loc.route, loc.route_reasons = "FILTER", [r for _, r in hits]
            elif hits:
                loc.route, loc.route_reasons = "HOLD", [r for _, r in hits] + [r for r in loc.route_reasons
                                                                               if loc.route == "HOLD"]
        s.add(loc)
        await s.flush()
        events.record(s, "locate", loc.id, "received", locate_json(loc), actor=actor)
        if loc.route == "FILTER":
            await decide(s, loc, "REJECTED", 0, by="policy:FILTER", rationale="; ".join(loc.route_reasons),
                         reply=False)
        elif loc.route == "AUTO" and cfg.auto_approve_etb:
            await decide(s, loc, "APPROVED", loc.qty_req, by="policy:ETB-AUTO",
                         rationale="ETB name under the per-name cap with live availability", reply=False)
        out.append(loc)
    await s.flush()
    await flush_auto_replies(s, out)
    return out


async def filters(s: AsyncSession, inst: Instrument, *, available: int | None = None,
                  fresh: bool | None = None) -> list[tuple[str, str]]:
    """The desk's pre-routing filters for one name (see ``rules.locate_filters``)."""
    lv = secmaster.live(inst.id)
    return rules.locate_filters(
        inst, price=lv["lastPx"],
        available=await market.available(s, inst.id) if available is None else available,
        availability_fresh=await market.availability_fresh(s) if fresh is None else fresh)


async def reg_sho_basis(s: AsyncSession, loc: Locate, crossed: int = 0) -> str:
    """What the desk relied on, at decision time. Internal inventory first, with how much this line crossed."""
    sup = (await market.supply(s, [loc.instrument_id])).get(loc.instrument_id or "", [])
    parts = []
    if loc.route == "AUTO":
        parts.append(f"ETB_LIST@{clock.iso(etb_built_at())}")
    for a in sorted(sup, key=lambda a: (a.source != market.INTERNAL, a.source)):
        if a.source == market.INTERNAL:
            if a.qty > 0:
                parts.append(f"INTERNAL_INVENTORY:{a.qty}@{clock.iso(a.as_of)} (crossed {crossed})")
        else:
            parts.append(f"{a.source}_AVAIL:{a.qty}@{clock.iso(a.as_of)}")
    return " + ".join(parts) or "TRADER_ATTESTATION"


class DecisionError(ValueError):
    pass


async def decide(s: AsyncSession, loc: Locate, decision: Decision, qty: int | None, *, by: str,
                 rationale: str | None = None, fee_bps: float | None = None, reply: bool = True,
                 override_floor: bool = False) -> Locate:
    """Decide one line. Counterparty limits are hard; the fee floor gives way only to a trader's override."""
    if loc.status not in ("RECEIVED",):
        raise DecisionError(f"locate {loc.id} is {loc.status}")
    qty = 0 if decision == "REJECTED" else (qty if qty is not None else loc.qty_req)
    if qty > loc.qty_req:
        raise DecisionError("approved quantity exceeds requested")
    if decision != "REJECTED" and qty <= 0:
        raise DecisionError("approve a positive quantity, or reject")
    if decision == "APPROVED" and qty < loc.qty_req:
        decision = "PARTIAL"
    if decision != "REJECTED" and loc.instrument_id:
        inst = await s.get(Instrument, loc.instrument_id)
        cp = await s.get(Counterparty, loc.requester_cp)
        cs, max_qty = await limits.for_line(s, cp, inst, qty)
        if failing := [f"{c.name} ({c.detail})" for c in cs if not c.ok]:
            raise DecisionError(f"over counterparty limits: {'; '.join(failing)}; at most {max_qty:,} fits")
        floor = (await pricing(s, inst, cp))["floorBps"]
        if fee_bps is None:  # the quote from intake, re-quoted if the market has moved through it
            fee_bps = max(loc.fee_ind_bps or 0.0, floor)
        elif fee_bps < floor:
            if not (override_floor and by.startswith("trader:")):
                raise DecisionError(f"{fee_bps:g} bps is under the desk's floor of {floor:g} "
                                    f"(a bit above market); a trader can override")
            rationale = f"{rationale + '; ' if rationale else ''}fee {fee_bps:g} under floor {floor:g}, overridden"
    before = locate_json(loc)
    # Cross first: before this line holds anything, how much of it our own inventory covers.
    crossed = min(qty, (await market.split(s, loc.instrument_id))["internal"]) if loc.instrument_id else 0
    loc.status, loc.qty_approved, loc.decided_by, loc.rationale = decision, qty, by, rationale
    loc.decided_at, loc.expires_at = clock.now(), clock.end_of_day()
    if fee_bps is not None:
        loc.fee_ind_bps = fee_bps
    if decision != "REJECTED":
        loc.reg_sho_basis = await reg_sho_basis(s, loc, crossed)
    events.record(s, "locate", loc.id, decision.lower(), locate_json(loc), before=before, actor=by)
    if reply:
        await send_reply(s, [loc])
    return loc


async def send_reply(s: AsyncSession, locs: list[Locate]) -> None:
    """Confirm on the channel the request came in on. Gateway lines are grouped per request."""
    by_req: dict[str, list[Locate]] = {}
    for loc in locs:
        if loc.source_channel == "API" and loc.status in ("APPROVED", "PARTIAL", "REJECTED"):
            by_req.setdefault(loc.request_ref, []).append(loc)
    for ref, group in by_req.items():
        lines = [{"lineNo": x.line_no, "decision": x.status, "approvedQuantity": x.qty_approved,
                  "indicativeFeeBps": x.fee_ind_bps, "rationale": x.rationale, "regShoBasis": x.reg_sho_basis,
                  "expiresAt": clock.iso(x.expires_at)} for x in group]
        try:
            await systems().gateway.decide(ref, lines)
            status = "SENT"
        except AdapterError as e:
            status = "FAILED"
            for x in group:
                x.error = str(e)
        for x in group:
            x.reply_status = status
    # Chat replies are drafted as actions for the gate once chat ingest lands (see agent/).


async def flush_auto_replies(s: AsyncSession, locs: list[Locate]) -> None:
    """Lines decided by policy (ETB approvals, filter declines) in one intake are confirmed in a single reply."""
    await send_reply(s, [x for x in locs if x.decided_by in ("policy:ETB-AUTO", "policy:FILTER")
                         and x.reply_status == "PENDING"])


async def release(s: AsyncSession, loc: Locate, by: str) -> Locate:
    if loc.status not in ("APPROVED", "PARTIAL"):
        raise DecisionError(f"locate {loc.id} is {loc.status}")
    before = locate_json(loc)
    loc.status = "RELEASED"
    events.record(s, "locate", loc.id, "released", locate_json(loc), before=before, actor=by)
    return loc


async def expire_due(s: AsyncSession) -> int:
    rows = (await s.execute(select(Locate).where(Locate.status.in_(("APPROVED", "PARTIAL")),
                                                 Locate.expires_at <= clock.now()))).scalars().all()
    for loc in rows:
        loc.status = "EXPIRED"
        events.record(s, "locate", loc.id, "expired", locate_json(loc))
    return len(rows)


async def listing(s: AsyncSession, *, status: list[str] | None = None, route: str | None = None,
                  limit: int = 500) -> list[dict[str, Any]]:
    q = select(Locate).order_by(Locate.received_at.desc()).limit(limit)
    if status:
        q = q.where(Locate.status.in_(status))
    if route:
        q = q.where(Locate.route == route)
    locs = list((await s.execute(q)).scalars())
    iids = list({x.instrument_id for x in locs if x.instrument_id})
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(Instrument.id.in_(iids)))).scalars()}
    sup, hld = await market.supply(s, iids), await market.held(s, iids)
    codes = list({x.requester_cp for x in locs})
    cps = {c.code: c for c in (await s.execute(select(Counterparty).where(Counterparty.code.in_(codes)))).scalars()}
    usage = await limits.books(s, codes)
    out = []
    for x in locs:
        inst = insts.get(x.instrument_id or "")
        if inst is None:
            out.append(locate_json(x))
            continue
        rows, h, cp = sup.get(inst.id, []), hld.get(inst.id, 0), cps.get(x.requester_cp)
        room = limits.summary(cp, inst, x.qty_req, usage.get(x.requester_cp, limits.Book())) \
            if x.status == "RECEIVED" else None
        out.append(locate_json(x, inst, max(sum(a.qty for a in rows) - h, 0), market.cross(rows, h),
                               price_floor(inst, cp, rows, h), room))
    return out
