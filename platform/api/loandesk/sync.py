"""Background sync with the outside world: clock, gateway inbox, stock record and CNS fails, Desk Chat, Venue A
market data, our orders on both venues, the processor's positions (internal inventory) and the custodian's settlements.

Polling keeps the scaffold simple and restart-safe (cursors live in ``sync_cursors``). The venues
also offer WebSocket streams; switching to them changes only this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import clock, db, events
from .adapters import AdapterError, systems
from .ingest import chat
from .models import Order, SyncCursor, Trade
from .services import booking, fails, inventory, lifecycle, locates, market, orders, recalls, shortlist
from .settings import get_settings

log = logging.getLogger("loandesk.sync")
status: dict[str, dict[str, Any]] = {}


async def _cursor(s: AsyncSession, name: str, default: str = "0") -> str:
    c = await s.get(SyncCursor, name)
    return c.value if c else default


async def _set_cursor(s: AsyncSession, name: str, value: str) -> None:
    c = await s.get(SyncCursor, name)
    if c is None:
        s.add(SyncCursor(name=name, value=value))
    else:
        c.value = value


async def sync_clock() -> None:
    clock.apply(await systems().control.clock())


async def sync_inbox() -> None:
    """Pull new locate requests from the Counterparty Gateway and run them through intake."""
    async with db.session() as s:
        after = int(await _cursor(s, "gateway_inbox"))
        reqs = await systems().gateway.inbox(after=after)
        for r in reqs:
            lines = [locates.LineIn(identifier=ln.get("isin") or ln["identifier"], quantity=ln["quantity"],
                                    line_no=ln["lineNo"], max_fee_bps=ln.get("maxFeeBps"))
                     for ln in r["lines"] if ln["status"] != "INVALID"]
            if lines:
                await locates.intake(s, counterparty=r["counterparty"], lines=lines, channel="API",
                                     request_ref=r["locateId"], source_ref=r.get("requestId"), actor="gateway")
            after = max(after, int(r["seq"]))
        await _set_cursor(s, "gateway_inbox", str(after))
        await s.commit()


async def sync_stock_record() -> None:
    """After each close: the stock record (client shorts settling next, locates used), then the cover drafts."""
    gw = systems().gateway
    try:
        latest = await gw.stock_record()
    except AdapterError as e:
        if e.status == 404:  # no close yet
            return
        raise
    d = date.fromisoformat(latest["tradeDate"])
    async with db.session() as s:
        have = await shortlist.latest_date(s)
        if have is not None and d <= have:
            return
        for day in shortlist.missed_closes(have, d):
            try:
                rec = latest if day == d else await gw.stock_record(day.isoformat())
            except AdapterError as e:
                if e.status == 404:  # a holiday: no close
                    continue
                raise
            await shortlist.apply_stock_record(s, rec)
        if get_settings().draft_covers:
            await shortlist.draft(s)
        await s.commit()


async def sync_fails() -> None:
    """Client shorts that failed to deliver at CNS: new ones from the gateway, then the Rule 204 pass over every one
    not delivered yet (closed out, delivered, or the deadline missed)."""
    async with db.session() as s:
        after = int(await _cursor(s, "gateway_fails"))
        rows = await systems().gateway.fails(after=after)
        if rows:
            await fails.ingest(s, rows)
            await _set_cursor(s, "gateway_fails", str(max(int(r["seq"]) for r in rows)))
        for note in await fails.assess(s):
            log.info("rule 204: %s", note)
        await s.commit()


async def sync_chat() -> None:
    """Read every room the bot is in. A room seen for the first time starts at its current end, so history
    from before the desk started (yesterday's locates) is never acted on."""
    dc = systems().chat
    for st in await dc.streams():
        name = f"chat:{st['streamId']}"[:32]
        async with db.session() as s:
            if await s.get(SyncCursor, name) is None:
                await _set_cursor(s, name, str(st.get("lastSeq") or 0))
                await s.commit()
                continue
            after = int(await _cursor(s, name))
        if int(st.get("lastSeq") or 0) <= after:
            continue
        page = await dc.messages(st["streamId"], after=after)
        for msg in page.get("messages", []):
            async with db.session() as s:  # one transaction per message: a bad one never blocks the room
                await chat.ingest(s, msg)  # ours too: our quotes and acceptances are half the conversation
                await _set_cursor(s, name, str(msg["seq"]))
                await s.commit()


async def sync_market() -> None:
    va = systems().venue_a
    avail = await va.availability()
    prints = await va.prints(limit=500)
    async with db.session() as s:
        n = await market.upsert_availability(s, "VENUE_A", avail["instruments"])
        await market.add_prints(s, "VENUE_A", prints)
        events.publish_later(s, "market", {"type": "refresh", "instruments": n, "asOf": avail.get("asOf")})
        await s.commit()


async def sync_inventory() -> None:
    """Internal inventory: the processor's positions on our books, netted per name."""
    positions = await systems().processor.positions()
    async with db.session() as s:
        await inventory.apply(s, positions)
        await s.commit()


async def sync_orders() -> None:
    """Follow every open order on Venue A, and record every execution as a trade.

    Trades are pulled on every pass, not only when an order changes: a GC firm bid can fill inside
    the place call, so its order is never open on our side.
    """
    va = systems().venue_a
    async with db.session() as s:
        open_orders = (await s.execute(select(Order).where(Order.status.in_(orders.OPEN),
                                                           Order.venue_order_id.is_not(None)))).scalars().all()
        if open_orders:
            venue = {v["orderId"]: v for v in await va.orders(limit=1000)}
            for o in open_orders:
                v = venue.get(o.venue_order_id)
                if v:
                    await orders.apply_venue(s, o, v)
        since = await _cursor(s, "venue_a_trades", "")
        for t in await va.trades(since=since or None):
            await orders.add_trade(s, t)
            since = max(since, t["executedAt"])
        await _set_cursor(s, "venue_a_trades", since)
        await s.commit()


async def sync_venue_b() -> None:
    """Follow our interest lists on Venue B (dealer counters), record every agreed loan as a trade, and pick up
    re-rates the counterparty answered. Loans from lists sent outside an order (paste) are trades too."""
    vb = systems().venue_b
    matches = await vb.matches()
    list_of = {m["matchId"]: m["listId"] for m in matches}
    async with db.session() as s:
        open_orders = (await s.execute(select(Order).where(Order.venue == "VENUE_B", Order.status.in_(orders.OPEN),
                                                           Order.venue_order_id.is_not(None)))).scalars().all()
        if open_orders:
            lists = {x["listId"]: x for x in await vb.lists()}
            for o in open_orders:
                mine = [m for m in matches if m["listId"] == o.venue_order_id]
                if await orders.accept_within_limit(s, o, mine):
                    mine = await vb.matches(o.venue_order_id)
                await orders.apply_venue_b(s, o, lists.get(o.venue_order_id), mine)
        for loan in await vb.loans():
            t = (await s.execute(select(Trade).where(Trade.venue_trade_id == loan["loanId"]))).scalar()
            if t is None:
                await orders.add_trade_b(s, loan, list_of.get(loan["matchId"]))
            else:
                await lifecycle.apply_loan(s, t, loan)
                rc = loan.get("recall") or {}
                if t.side == "BORROW" and (rc.get("quantity") or 0) > (t.recalled_qty or 0):  # a new recall on it
                    await recalls.on_notice(s, t, rc["quantity"] - (t.recalled_qty or 0),
                                            date.fromisoformat(rc["dueDate"]), ref=loan["loanId"], origin="venue_b")
        await s.commit()


async def sync_recalls() -> None:
    """Lenders' recalls at the processor (Venue A fills and bilateral trades, whose contract there is the loan)."""
    async with db.session() as s:
        cur = await s.get(SyncCursor, "processor_recalls")
        after = int(cur.value) if cur else 0
        for n in await systems().processor.recalls(after):
            after = max(after, int(n["seq"]))
            t = (await s.execute(select(Trade).where(Trade.sfp_contract_id == n["contractId"]))).scalar()
            if t is None or n["status"] != "OPEN":
                continue
            await recalls.on_notice(s, t, int(n["outstanding"]), date.fromisoformat(n["dueDate"]),
                                    ref=n["recallId"], origin="processor")
        await _set_cursor(s, "processor_recalls", str(after))
        await s.commit()


async def sync_settlement() -> None:
    async with db.session() as s:
        pending = (await s.execute(select(Trade.id).where(
            (Trade.settle_ref.is_not(None) & Trade.settle_status.not_in(booking.SETTLED))
            | (Trade.returns_settling > 0)).limit(1))).first()
        if pending is None:
            return
        await booking.apply_settlement(s, await systems().custodian.instructions())
        await s.commit()


async def housekeeping() -> None:
    async with db.session() as s:
        if await locates.expire_due(s):
            await s.commit()


async def _every(name: str, seconds: float, fn: Callable[[], Awaitable[None]]) -> None:
    st = status.setdefault(name, {"ok": None, "error": None, "lastRun": None, "every": seconds})
    while True:
        try:
            await fn()
            st.update(ok=True, error=None)
        except AdapterError as e:
            st.update(ok=False, error=str(e))
        except Exception as e:  # keep the loop alive; the status panel shows it
            log.exception("sync %s failed", name)
            st.update(ok=False, error=f"{type(e).__name__}: {e}")
        st["lastRun"] = clock.iso(clock.now())
        await asyncio.sleep(seconds)


class Syncer:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        cfg = get_settings()
        jobs = {"clock": (5.0, sync_clock), "inbox": (cfg.poll_s, sync_inbox),
                **({"chat": (cfg.poll_s, sync_chat)} if cfg.chat_ingest else {}),
                "market": (cfg.availability_every_s, sync_market), "orders": (cfg.poll_s, sync_orders),
                "venue_b": (cfg.poll_s, sync_venue_b), "settlement": (cfg.poll_s * 2.5, sync_settlement),
                "inventory": (cfg.inventory_every_s, sync_inventory), "recalls": (cfg.poll_s * 2, sync_recalls),
                "stock_record": (cfg.poll_s * 5, sync_stock_record), "fails": (cfg.poll_s * 2, sync_fails),
                "housekeeping": (30.0, housekeeping)}
        self.tasks = [asyncio.create_task(_every(n, sec, fn), name=f"sync-{n}") for n, (sec, fn) in jobs.items()]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
