"""The deterministic engine: locate routing, pre-trade checks and order routing.

Models never compute money or limits (spec: *determinism boundary*). Everything here is plain
code the agent can call as a tool and the trader can read as a reason.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .models import Counterparty, Instrument
from .settings import get_settings

Route = Literal["AUTO", "REVIEW", "HOLD"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def locate_route(inst: Instrument, tier: str, etb: bool, qty: int, available: int,
                 cp: Counterparty | None) -> tuple[Route, list[str]]:
    """Spec module 3 decision tiers: who gets to decide this line."""
    cfg = get_settings()
    hold: list[str] = []
    if tier == "HOT":
        hold.append("hot name")
    if inst.threshold_flag:
        hold.append("threshold list")
    if inst.restricted:
        hold.append("restricted list")
    if cp is None:
        hold.append("unknown counterparty")
    if hold:
        return "HOLD", hold
    review: list[str] = []
    if not etb:
        review.append(f"{tier.lower()} name, not on the ETB list")
    if qty > cfg.locate_name_cap:
        review.append(f"over the per-name cap of {cfg.locate_name_cap:,}")
    if available < qty:
        review.append(f"only {available:,} available")
    elif available < 2 * qty:
        review.append(f"availability {available:,} under 2x the request")
    if review:
        return "REVIEW", review
    return "AUTO", ["ETB, under cap, covered by availability"]


def fee_band(last_fee_bps: float) -> tuple[float, float]:
    """The fat-finger band around the last fee, in bps."""
    band = get_settings().fat_finger_band
    lo, hi = last_fee_bps * (1 - band), last_fee_bps / (1 - band)
    return max(lo - 10, 0), hi + 10  # a 10 bp floor so GC names near zero don't trip on noise


def locate_decision_checks(*, decision: str, quantity: int, qty_requested: int, available: int,
                           fee_bps: float | None, last_fee_bps: float | None) -> list[Check]:
    """Is a proposed locate decision (from the agent, before it reaches the gate) consistent with the book?"""
    out = [Check("decision", decision in ("APPROVED", "PARTIAL", "REJECTED"), decision)]
    if decision == "REJECTED":
        return out
    shape = quantity == qty_requested if decision == "APPROVED" else 0 < quantity < qty_requested
    out.append(Check("quantity", shape, f"{quantity:,} {decision.lower()} of {qty_requested:,} requested"))
    out.append(Check("availability", quantity <= available, f"{quantity:,} vs {available:,} available"))
    if fee_bps is None or fee_bps < 0:
        out.append(Check("fee", False, "no indicative fee"))
    elif last_fee_bps and last_fee_bps > 0:
        lo, hi = fee_band(last_fee_bps)
        out.append(Check("fat finger (fee)", lo <= fee_bps <= hi,
                         f"{fee_bps:g} bps vs last {last_fee_bps:g} (band {lo:.0f}-{hi:.0f})"))
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
