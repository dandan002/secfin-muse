"""Recalls from lenders (spec module 5): a lender wants back shares it lent us.

A recall arrives three ways. In Desk Chat ("Recall notice: VOD LN 250k, ref HAR-12345, please return by T+1"),
each line is matched to our open borrows from that lender in that name: the one carrying the lender's ref first,
then the oldest (``on_recall``). From the processor, for a Venue A fill or a bilateral trade, whose contract there
is the loan; and on Venue B, on the venue's loan (``on_notice``, both from ``sync``). The recalled shares go on
the trade (``recalled_qty``, due ``recall_due``) and stop counting as cover on the short list. A return card for
them goes to the gate; any excess return already drafted in the name is superseded, since the recall changes
what's spare; and the short list redrafts, so a name the clients still need gets a cover to borrow it elsewhere.
A chat recall we can't match waits in chat for the trader.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from refdata import calendar

from .. import clock, events
from ..models import Action, ChatMessage, Instrument, Order, Trade
from ..settings import get_settings
from .booking import open_qty, trade_json

DUE = re.compile(r"\bT\s*\+\s*(\d)\b", re.I)
REF = re.compile(r"\bref\W+([A-Za-z0-9-]+)|\(([A-Z]{2,5}-\d{3,})\)", re.I)
SOURCE = "recall"  # recall return cards carry source_ref "recall:<chat message id>"


def terms(text: str, sent: Any) -> tuple[Any, str | None]:
    """When the shares are due back (T+n from the notice, T+1 if it doesn't say) and the lender's ref."""
    due = DUE.search(text or "")
    ref = REF.search(text or "")
    return (calendar.add_business_days(clock.local_date(sent), int(due.group(1)) if due else 1),
            (ref.group(1) or ref.group(2)) if ref else None)


async def on_recall(s: AsyncSession, m: ChatMessage) -> tuple[bool, list[str]]:
    """Put a chat recall on the borrows it hits and draft their returns. True if every line was matched in full."""
    if not m.counterparty:
        return False, ["a recall, but from whom isn't clear: the trader matches it"]
    due, ref = terms(m.text, m.sent_at)
    by = f"chat:{m.counterparty}"
    notes: list[str] = []
    made, whole, names = [], True, set()
    for ln in m.lines or []:
        iid, left = ln.get("instrumentId"), int(ln.get("quantity") or 0)
        if not iid or left <= 0:
            notes.append(f"{ln.get('identifier')}: no name or size to match")
            whole = False
            continue
        inst = await s.get(Instrument, iid)
        name = inst.composite if inst else iid
        trades = (await s.execute(select(Trade).where(
            Trade.cp_code == m.counterparty, Trade.instrument_id == iid, Trade.side == "BORROW",
            Trade.loan_status == "OPEN").order_by(Trade.executed_at))).scalars().all()
        if ref and trades:  # theirs carries their ref: a chat borrow keeps it as the order's external ref
            refs = {o.id for o in (await s.execute(select(Order).where(
                Order.id.in_([t.order_id for t in trades if t.order_id]), Order.venue_order_id == ref))).scalars()}
            trades = sorted(trades, key=lambda t: t.order_id not in refs)
        for t in trades:
            free = open_qty(t) - (t.recalled_qty or 0)
            if left <= 0 or free <= 0:
                continue
            qty = min(free, left)
            left -= qty
            names.add(iid)
            why = (f"{m.counterparty} recalled {int(ln['quantity']):,} {name}" + (f" (ref {ref})" if ref else "")
                   + f" in chat, due back {due.isoformat()}: \"{m.text[:120]}\"")
            a = await _recall(s, t, qty, due, ref, by=by, key=m.id, why=why, name=name, notes=notes,
                              original={"message": m.id, "line": ln, "due": due.isoformat(), "ref": ref})
            if a is not None:
                made.append(a.id)
        if left > 0:
            whole = False
            notes.append(f"{name}: {m.counterparty} recalls {left:,} more than we borrow from them")
    notes += await _after(s, names, made, f"{m.counterparty}'s recall (message {m.id})")
    return whole and bool(made), notes


async def on_notice(s: AsyncSession, t: Trade, qty: int, due: Any, *, ref: str, origin: str) -> list[str]:
    """A recall on one of our borrows from the processor or Venue B: put it on the trade and draft the return."""
    inst = await s.get(Instrument, t.instrument_id)
    name = inst.composite if inst else t.instrument_id
    qty = min(qty, open_qty(t) - (t.recalled_qty or 0))
    if qty <= 0:
        return []
    notes: list[str] = []
    via = {"processor": "at the processor", "venue_b": "on Venue B"}.get(origin, f"via {origin}")
    why = f"{t.cp_code} recalled {qty:,} {name} {via} (ref {ref}), due back {due.isoformat()}"
    a = await _recall(s, t, qty, due, ref, by=f"{origin}:{t.cp_code}", key=ref, why=why, name=name, notes=notes,
                      original={"origin": origin, "ref": ref, "due": due.isoformat(), "quantity": qty})
    return notes + await _after(s, {t.instrument_id}, [a.id] if a else [], f"{t.cp_code}'s recall ({ref})")


async def _recall(s: AsyncSession, t: Trade, qty: int, due: Any, ref: str | None, *, by: str, key: str, why: str,
                  name: str, notes: list[str], original: dict[str, Any]) -> Action | None:
    """Mark ``qty`` of a borrow recalled and draft its return (none until it's booked)."""
    from .. import gate

    before = trade_json(t)
    t.recalled_qty, t.recall_due, t.recall_ref = (t.recalled_qty or 0) + qty, due, ref
    t.lifecycle = [*(t.lifecycle or []), {"ts": clock.iso(clock.now()), "event": "RECALLED", "by": by,
                                          "quantity": qty, "due": due.isoformat(), "ref": ref, "source": key}]
    events.record(s, "trade", t.id, "recalled", trade_json(t), before=before, actor=by)
    if not t.sfp_contract_id:
        notes.append(f"{name}: {t.id} is recalled but isn't booked yet: book it, then return it")
        return None
    a = await gate.propose(
        s, kind="lifecycle", payload={"tradeId": t.id, "event": "RETURN", "quantity": qty},
        proposed_by=f"rules:{SOURCE}", title=f"recall from {t.cp_code}: return {qty:,} {name} by {due.isoformat()}",
        rationale=f"{why}. Returning {qty:,} of the {open_qty(t):,} open on {t.id} @ {t.fee_bps:g} bps; not back "
                  f"by then, the lender can buy the shares in at our cost.",
        risk_flags=["warning: due back today"] if due <= clock.local_date() else [],
        source_ref=f"{SOURCE}:{key}", original=original)
    notes.append(f"{name}: recalled {qty:,} on {t.id}, return card {a.id}")
    return a


async def _after(s: AsyncSession, names: set[str], made: list[str], cause: str) -> list[str]:
    """What's spare has changed: supersede excess returns drafted in these names and redraft the short list."""
    from . import shortlist

    notes: list[str] = []
    if names:
        await _supersede_returns(s, names, cause)
        if get_settings().draft_covers:
            out = await shortlist.draft(s, by=f"rules:{SOURCE}")
            if out["drafted"]:
                notes.append(f"short list redrafted: {len(out['drafted'])} card(s)")
    if made:
        events.publish_later(s, "short_list", {"type": "recall", "actions": made})
    return notes


async def _supersede_returns(s: AsyncSession, names: set[str], cause: str) -> None:
    """Excess returns drafted before the recall: what's spare has changed, so the short list drafts them again."""
    from .. import gate
    from .shortlist import RETURN

    for a in (await s.execute(select(Action).where(Action.status == "PENDING", Action.kind == "lifecycle",
                                                   Action.source_ref.in_([f"{RETURN}:{i}" for i in names])))
              ).scalars():
        await gate.reject(s, a, "rules:recall", f"superseded by {cause}", status="SUPERSEDED")
