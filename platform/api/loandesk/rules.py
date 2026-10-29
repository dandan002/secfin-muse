"""The deterministic engine: locate routing, pre-trade checks and order routing.

Models never compute money or limits (spec: *determinism boundary*). Everything here is plain
code the agent can call as a tool and the trader can read as a reason.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .models import Counterparty, Instrument
from .settings import get_settings

Route = Literal["FILTER", "AUTO", "REVIEW", "HOLD"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def locate_filters(inst: Instrument, *, price: float, available: int, availability_fresh: bool
                   ) -> list[tuple[str, str]]:
    """Lines the desk won't locate, whatever the request: ``(action, reason)`` for each filter that matches.

    The threshold, restricted and price filters are static facts about the name. Zero availability only
    counts while the feed is fresh, so a stalled feed sends lines to review instead of declining them all.
    """
    cfg = get_settings()
    price_usd = price * FX_USD.get(inst.ccy, 1.0)  # the minimum is in dollars, whatever the listing currency
    shown = f"${price_usd:.2f}" if inst.ccy == "USD" else f"{inst.ccy} {price:g} (${price_usd:.2f})"
    hits = [(cfg.filter_threshold, inst.threshold_flag, "on the Reg SHO threshold list (persistent fails)"),
            (cfg.filter_restricted, inst.restricted, "on the restricted list"),
            (cfg.filter_no_availability, availability_fresh and available <= 0, "no availability"),
            (cfg.filter_low_price, price_usd < cfg.min_price_usd,
             f"priced at {shown}, under the ${cfg.min_price_usd:g} minimum")]
    return [(action, reason) for action, hit, reason in hits if hit and action != "off"]


def locate_route(inst: Instrument, tier: str, etb: bool, qty: int, available: int,
                 cp: Counterparty | None) -> tuple[Route, list[str]]:
    """Spec module 3 decision tiers: who gets to decide this line."""
    hold: list[str] = []
    if tier == "HOT":
        hold.append("hot name")
    if cp is None:
        hold.append("unknown counterparty")
    if hold:
        return "HOLD", hold
    review: list[str] = []
    if not etb:
        review.append(f"{tier.lower()} name, not on the ETB list")
    if available < qty:
        review.append(f"only {available:,} available")
    elif available < 2 * qty:
        review.append(f"availability {available:,} under 2x the request")
    if review:
        return "REVIEW", review
    return "AUTO", ["ETB, under cap, covered by availability"]


def fee_floor(market_bps: float, *, cost_bps: float | None = None, internal_cp: bool = False) -> dict[str, Any]:
    """The least the desk charges to lend: a bit above market (and above what the shares cost us).

    External counterparties pay the market fee plus ``markup_pct`` (at least ``markup_min_bps``); internal
    desks pay market. Either way never below our own borrow cost when we cross from internal inventory.
    """
    cfg = get_settings()
    base = max(market_bps, cost_bps or 0.0)
    floor = base if internal_cp else max(base * (1 + cfg.markup_pct / 100), base + cfg.markup_min_bps)
    return {"marketBps": market_bps, "costBps": cost_bps, "floorBps": round(floor, 1),
            "basis": "internal desk: market" if internal_cp else
            f"market + {cfg.markup_pct:g}% (at least {cfg.markup_min_bps:g} bps)"}


def fee_band(last_fee_bps: float) -> tuple[float, float]:
    """The fat-finger band around the last fee, in bps."""
    band = get_settings().fat_finger_band
    lo, hi = last_fee_bps * (1 - band), last_fee_bps / (1 - band)
    return max(lo - 10, 0), hi + 10  # a 10 bp floor so GC names near zero don't trip on noise


def supply_signals(*, qty_requested: int, available: int, supply: int, held: int, lenders: int | None,
                   util_pct: float, history: list[dict[str, Any]], internal_available: int = 0
                   ) -> list[dict[str, str]]:
    """Plain facts about how gettable the supply really is, for the reviewer to weigh (it decides, not this).

    Listed availability is indicative: lenders recall into high utilization, thin lender bases vanish, and a
    rising fee means the street is already short. ``level`` is ``issue`` (argues for partial or no fill) or
    ``watch`` (worth a sentence).
    """
    cfg = get_settings()
    out: list[dict[str, str]] = []

    def sig(level: str, name: str, detail: str) -> None:
        out.append({"level": level, "signal": name, "detail": detail})

    internally_covered = internal_available >= qty_requested > 0
    if internally_covered:  # our own box is firm: street cover and the lender base don't decide this one
        sig("watch", "covered internally", f"{internal_available:,} of our own inventory available; crossed first")
    else:
        if internal_available:
            street = qty_requested - internal_available
            sig("watch", "part covered internally",
                f"{internal_available:,} from our own inventory, {street:,} from the street")
        cover = available / qty_requested if qty_requested else 0.0
        if cover < 1:
            sig("issue", "short supply", f"{available:,} available for {qty_requested:,} requested ({cover:.2f}x)")
        elif cover < 2:
            sig("issue", "thin cover", f"availability covers {cover:.1f}x the request (under 2x)")
        if lenders is not None and lenders <= 2:
            sig("issue", "thin lender base", f"{lenders} lender{'s' if lenders != 1 else ''} showing supply")
    if supply and held / supply >= 0.5:
        sig("watch", "desk already long the supply", f"approved locates hold {held / supply:.0%} of listed supply")
    if util_pct >= cfg.hot_min_util_pct:
        sig("issue", "utilization at recall levels", f"{util_pct:.1f}% utilized")
    elif util_pct >= 60:
        sig("watch", "utilization elevated", f"{util_pct:.1f}% utilized")
    if len(history) >= 2:
        first, last = history[0], history[-1]
        if last["utilPct"] - first["utilPct"] >= 10:
            sig("issue", "utilization rising",
                f"{first['utilPct']:.1f}% to {last['utilPct']:.1f}% over {len(history)} days")
        if first["feeBps"] > 0:
            change = (last["feeBps"] - first["feeBps"]) / first["feeBps"]
            if change >= 0.25:
                sig("issue", "fee rising", f"{first['feeBps']:g} to {last['feeBps']:g} bps over {len(history)} days "
                                           f"(+{change:.0%})")
        fees = [h["feeBps"] for h in history]
        if min(fees) > 0 and max(fees) / min(fees) >= 1.5:
            sig("watch", "fee volatile", f"{min(fees):g}-{max(fees):g} bps range over {len(history)} days")
        tiers = {h["tier"] for h in history}
        if "HOT" in tiers and history[-1]["tier"] != "HOT":
            sig("watch", "recently hot", f"traded hot within the last {len(history)} days")
    return out


def locate_decision_checks(*, decision: str, quantity: int, qty_requested: int, available: int,
                           fee_bps: float | None, last_fee_bps: float | None, tier: str, shs_out: int,
                           issues: list[str], floor_bps: float | None = None,
                           cp_max_qty: int | None = None) -> list[Check]:
    """Is a proposed locate decision (from the agent, before it reaches the gate) consistent with the book?

    Blocking checks are hard limits. Advisory ones (``blocking=False``) never stop a proposal: they ride on
    the card as risk flags, because the reviewer may have good reasons the trader should read.
    """
    cfg = get_settings()
    out = [Check("decision", decision in ("APPROVED", "PARTIAL", "REJECTED"), decision)]
    if decision == "REJECTED":
        return out
    shape = quantity == qty_requested if decision == "APPROVED" else 0 < quantity < qty_requested
    out.append(Check("quantity", shape, f"{quantity:,} {decision.lower()} of {qty_requested:,} requested"))
    out.append(Check("availability", quantity <= available, f"{quantity:,} vs {available:,} available"))
    if cp_max_qty is not None:
        out.append(Check("counterparty limits", quantity <= cp_max_qty,
                         f"{quantity:,} vs {cp_max_qty:,} the counterparty's limits allow"))
    pct_so = quantity / max(shs_out, 1) * 100
    out.append(Check("concentration (shares out)", pct_so <= cfg.max_pct_shares_out,
                     f"{pct_so:.2f}% of shares outstanding (max {cfg.max_pct_shares_out}%)"))
    if fee_bps is None or fee_bps < 0:
        out.append(Check("fee", False, "no indicative fee"))
    else:
        if floor_bps is not None:
            out.append(Check("fee floor", fee_bps >= floor_bps,
                             f"{fee_bps:g} bps vs the desk's floor of {floor_bps:g} (a bit above market)"))
        if last_fee_bps and last_fee_bps > 0:
            lo, hi = fee_band(last_fee_bps)
            out.append(Check("fat finger (fee)", lo <= fee_bps <= hi,
                             f"{fee_bps:g} bps vs last {last_fee_bps:g} (band {lo:.0f}-{hi:.0f})"))
    if decision == "APPROVED":
        out.append(Check("hot name", tier != "HOT", "full fill on a hot name (desk practice: suggest a partial)",
                         blocking=False))
        out.append(Check("supply issues", not issues, "full fill despite: " + "; ".join(issues), blocking=False))
    return out


def pretrade_checks(*, side: str, inst: Instrument, qty: int, fee_bps: float, price: float, last_fee_bps: float | None,
                    cp: Counterparty | None, cp_exposure_usd: float, fx_usd: float) -> list[Check]:
    """Spec module 4 pre-trade checks. Any failing blocking check stops the order before it leaves the desk."""
    cfg = get_settings()
    notional_usd = qty * price * fx_usd
    out = [Check("restricted list", not inst.restricted, "restricted" if inst.restricted else "clear")]
    if cp is not None:
        headroom = cp.credit_limit_usd - cp_exposure_usd
        out.append(Check("counterparty limit", notional_usd <= headroom,
                         f"${notional_usd:,.0f} vs ${headroom:,.0f} headroom of ${cp.credit_limit_usd:,.0f}"))
    pct_so = qty / max(inst.shs_out, 1) * 100
    out.append(Check("concentration (shares out)", pct_so <= cfg.max_pct_shares_out,
                     f"{pct_so:.2f}% of shares outstanding (max {cfg.max_pct_shares_out}%)"))
    pct_adv = qty / max(inst.adv_20d, 1) * 100
    out.append(Check("concentration (ADV)", pct_adv <= cfg.max_pct_adv,
                     f"{pct_adv:.0f}% of 20d ADV (max {cfg.max_pct_adv:.0f}%)", blocking=False))
    if last_fee_bps and last_fee_bps > 0:
        lo, hi = fee_band(last_fee_bps)
        out.append(Check("fat finger (fee)", lo <= fee_bps <= hi,
                         f"{fee_bps:g} bps vs last {last_fee_bps:g} (band {lo:.0f}-{hi:.0f})"))
        if side == "LEND":
            floor = fee_floor(last_fee_bps, internal_cp=cp is not None and cp.kind == "INTERNAL")["floorBps"]
            out.append(Check("fee floor", fee_bps >= floor,
                             f"lending at {fee_bps:g} bps vs the desk's floor of {floor:g} (a bit above market)"))
    return out


def route_order(*, side: str, tier: str, qty: int) -> tuple[str, str, str]:
    """(venue, venue order type, reason). GC borrows go to firm bids; specials are negotiated."""
    if side == "LEND":
        return "VENUE_A", "OFFER", "lend: rest an offer on Venue A"
    if tier == "GC":
        return "VENUE_A", "FIRM_BID", "GC name: firm bid on Venue A fills against any lender with supply"
    return "VENUE_A", "BID", f"{tier.lower()} name: negotiated bid on Venue A (lenders may counter)"


FX_USD = {"USD": 1.0, "GBP": 1.27, "EUR": 1.08, "JPY": 0.0067}


def default_terms(inst: Instrument, cp: Counterparty | None) -> dict[str, Any]:
    us = inst.country == "US"
    return {"collateralType": "CASH", "collateralCcy": inst.ccy, "marginPct": 102.0 if us else 105.0,
            "legalAgreement": cp.agreement if cp else ("MSLA" if us else "GMSLA_2010"),
            "bookingEntity": get_settings().booking_entity, "term": "OPEN"}
