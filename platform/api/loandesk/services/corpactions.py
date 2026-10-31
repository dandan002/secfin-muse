"""Corporate actions ahead (spec module 5): where a dividend or a vote may bring recalls on our borrows.

A lender that wants its dividend rather than a manufactured payment, or wants to vote, recalls its shares to have
them back by the record date. Recalls come a few business days before it, so the desk can see them coming: per
event with a record date in the next ``days`` business days, in a name we borrow or the clients are short, our open
borrows from each lender (less what they've recalled already), how likely a recall is for that kind of event, and
what the short list would be short by if they all came, or as many as expected. For a dividend, the clients short
over the record date owe the manufactured payment.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from refdata import calendar

from .. import clock, rules, secmaster
from ..models import Instrument, Trade
from .booking import open_qty


async def exposure(s: AsyncSession, days: int = 10) -> list[dict[str, Any]]:
    from . import shortlist

    today = clock.local_date()
    sl = await shortlist.build(s)
    lines = {ln["instrumentId"]: ln for ln in sl["lines"]}
    borrows: dict[str, list[Trade]] = defaultdict(list)
    for t in (await s.execute(select(Trade).where(Trade.side == "BORROW", Trade.loan_status == "OPEN")
                              .order_by(Trade.executed_at))).scalars():
        borrows[t.instrument_id].append(t)
    names = set(borrows) | {iid for iid, ln in lines.items() if ln["need"]}
    events = secmaster.corporate_actions(today, calendar.add_business_days(today, days), names)
    insts = {i.id: i for i in (await s.execute(select(Instrument).where(
        Instrument.id.in_({e.instrument_id for e in events})))).scalars()}
    out = []
    for e in events:
        inst, ln = insts.get(e.instrument_id), lines.get(e.instrument_id)
        lenders = []
        for t in borrows.get(e.instrument_id, []):
            already = any(x.get("event") == "RECALLED" and x.get("eventId") == e.event_id for x in t.lifecycle or [])
            free = max(open_qty(t) - (t.recalled_qty or 0), 0)
            lenders.append({"tradeId": t.id, "counterparty": t.cp_code, "openQty": open_qty(t),
                            "recalledQty": t.recalled_qty or 0, "atRisk": 0 if already else free,
                            "recalledForThis": already})
        at_risk = sum(x["atRisk"] for x in lenders)
        expected = int(round(at_risk * e.recall_p, -2))
        need, cover = (ln["need"], ln["cover"]) if ln else (0, 0)
        px = (ln or {}).get("lastPx") or secmaster.live(e.instrument_id)["lastPx"] or 0.0
        fx = rules.FX_USD.get(e.ccy, 1.0)
        out.append({
            **e.to_json(), "composite": inst.composite if inst else e.instrument_id,
            "label": secmaster.CA_LABEL.get(e.kind, e.kind),
            "daysToRecord": (e.record_date - today).days, "noticeDue": e.notice_date <= today,
            "lenders": lenders, "borrowed": sum(x["openQty"] for x in lenders), "atRisk": at_risk,
            "expectedRecall": expected, "clientShort": need, "cover": cover,
            "shortIfAllRecalled": max(need - (cover - at_risk), 0) if need else 0,
            "shortIfExpected": max(need - (cover - expected), 0) if need else 0,
            "atRiskNotional": round(at_risk * px * fx, 2),
            # The clients short over the record date owe the dividend, paid as a manufactured payment.
            "manufacturedUsd": round(need * e.amount * fx, 2) if e.kind == "DIVIDEND" and e.amount else None,
        })
    out.sort(key=lambda x: (x["recordDate"], -x["atRiskNotional"]))
    return out
