"""FIX 4.4 initiator for Venue A, built on simplefix (an independent FIX codec).

Session layer: Logon/Logout, heartbeats and TestRequest replies, sequence numbers (optionally
persisted across restarts), gap detection with ResendRequest, and gap-filling the venue's
ResendRequests (stale orders are never re-sent, as is usual for order flow).

Application layer: NewOrderSingle (D), counter via OrderCancelReplaceRequest (G), OrderCancelRequest
(F), SLAccept (U1), OrderStatusRequest (H) and MarketDataRequest (V). Incoming messages are parsed
into typed events. Tag reference: ../../API.md#fix-44-session.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import simplefix

from .models import OrderRequest

log = logging.getLogger(__name__)

BEGIN_STRING = "FIX.4.4"
ID_SOURCE = {"1": "CUSIP", "2": "SEDOL", "4": "ISIN", "8": "TICKER"}
EXEC_TYPE = {"0": "NEW", "4": "CANCELLED", "5": "REPLACED", "8": "REJECTED", "C": "EXPIRED", "D": "RESTATED",
             "F": "TRADE", "I": "ORDER_STATUS"}
ORD_STATUS = {"0": "NEW", "2": "FILLED", "4": "CANCELLED", "8": "REJECTED", "C": "EXPIRED"}
ADMIN_TYPES = {"0", "1", "2", "3", "4", "5", "A"}


# -- typed incoming messages -------------------------------------------------------------------
@dataclass
class FixMessage:
    msg_type: str
    seq: int
    poss_dup: bool
    fields: dict[int, str]  # last value per tag; use ``pairs`` for repeating groups
    pairs: list[tuple[int, str]]
    raw: bytes

    def get(self, tag: int, default: str = "") -> str:
        return self.fields.get(tag, default)

    def opt(self, tag: int) -> str | None:
        return self.fields.get(tag)


@dataclass
class ExecutionReport:
    """35=8. ``exec_type``: NEW, RESTATED (lender counter), REPLACED, TRADE, CANCELLED, EXPIRED,
    REJECTED or ORDER_STATUS (reply to an OrderStatusRequest)."""

    order_id: str
    cl_ord_id: str
    exec_id: str
    exec_type: str
    ord_status: str
    isin: str | None
    side: str | None
    quantity: int
    fee_bps: float | None
    counter_fee_bps: float | None
    counter_quantity: int | None
    last_qty: int | None
    last_fee_bps: float | None
    venue_trade_id: str | None
    status_req_id: str | None
    text: str | None
    message: FixMessage

    @property
    def reject_code(self) -> str | None:
        """The venue's error code from ``58 Text`` (``CODE: reason``) on rejects."""
        if self.exec_type == "REJECTED" or self.ord_status == "REJECTED":
            return (self.text or "").split(":", 1)[0] or None
        return None


@dataclass
class TradeCaptureReport:
    """35=AE: the venue's trade confirmation, with the UTI (tag 20013)."""

    trade_report_id: str
    venue_trade_id: str
    uti: str
    order_id: str
    cl_ord_id: str
    side: str
    isin: str
    quantity: int
    fee_bps: float
    trade_date: str
    settle_date: str
    contra_party: str | None
    message: FixMessage


@dataclass
class MarketDataEntry:
    lender_code: str
    fee_bps: float
    quantity: int
    action: str = "NEW"  # incremental only: NEW, CHANGE, DELETE
    isin: str | None = None


@dataclass
class MarketData:
    """35=W (snapshot) or 35=X (incremental): lendable supply per lender."""

    req_id: str
    snapshot: bool
    isin: str | None
    entries: list[MarketDataEntry]
    message: FixMessage


@dataclass
class Rejected:
    """35=3 session reject, 35=9 OrderCancelReject, or 35=Y MarketDataRequestReject."""

    kind: str
    ref: str | None
    text: str | None
    message: FixMessage


class FixLogonError(ConnectionError):
    pass


# -- sequence number store -----------------------------------------------------------------------
@dataclass
class SeqNums:
    next_out: int = 1
    next_in: int = 1


class FileSeqStore:
    """Keeps sequence numbers across process restarts, as a production FIX engine does."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> SeqNums:
        if self.path.exists():
            return SeqNums(**json.loads(self.path.read_text()))
        return SeqNums()

    def save(self, s: SeqNums) -> None:
        self.path.write_text(json.dumps({"next_out": s.next_out, "next_in": s.next_in}))


class MemorySeqStore:
    def __init__(self) -> None:
        self.s = SeqNums()

    def load(self) -> SeqNums:
        return SeqNums(self.s.next_out, self.s.next_in)

    def save(self, s: SeqNums) -> None:
        self.s = SeqNums(s.next_out, s.next_in)


# -- session -------------------------------------------------------------------------------------
@dataclass
class _Waiter:
    pred: Callable[[Any], bool]
    fut: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class FixSession:
    """One FIX session with the venue.

    >>> fix = FixSession(host, 9878, password=pw, store=FileSeqStore("venue_a.seq"))
    >>> await fix.logon()
    >>> await fix.new_order_single(OrderRequest(...))
    >>> fill = await fix.wait_for(lambda m: isinstance(m, ExecutionReport) and m.exec_type == "TRADE")
    >>> async for msg in fix.messages(): ...   # or consume everything as it arrives
    """

    def __init__(self, host: str, port: int, password: str, sender_comp_id: str = "LOANDESK",
                 target_comp_id: str = "VENUEA", heartbeat_s: int = 30,
                 store: FileSeqStore | MemorySeqStore | None = None, history: int = 10_000) -> None:
        self.host, self.port, self.password = host, port, password
        self.sender, self.target, self.heartbeat_s = sender_comp_id, target_comp_id, heartbeat_s
        self.store = store or MemorySeqStore()
        self.seq = self.store.load()
        self.history: deque[Any] = deque(maxlen=history)  # recent application messages, for wait_for
        self._subscribers: list[asyncio.Queue[Any]] = []
        self._waiters: list[_Waiter] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task] = []
        self._send_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._last_out = 0.0
        self._last_in = 0.0
        self.logged_on = False
        self.closed_reason: str | None = None

    # -- lifecycle ---------------------------------------------------------------------------------
    async def logon(self, reset_seq_nums: bool = False, timeout: float = 10.0) -> None:  # noqa: ASYNC109
        """Connect and log on. With ``reset_seq_nums`` both sides restart at 1 (start of day);
        otherwise sequence numbers continue and anything missed while offline is recovered."""
        if reset_seq_nums:
            self.seq = SeqNums()
            self.store.save(self.seq)
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self.closed_reason = None
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._heartbeat_loop())]
        ack = asyncio.get_running_loop().create_future()
        self._logon_ack = ack
        await self._send("A", [(98, 0), (108, self.heartbeat_s), (141, "Y" if reset_seq_nums else None),
                               (554, self.password)])
        try:
            await asyncio.wait_for(ack, timeout)
        except TimeoutError:
            await self.disconnect()
            raise FixLogonError("no Logon reply from venue") from None
        self.logged_on = True

    async def logout(self, text: str = "bye") -> None:
        if self._writer and not self._writer.is_closing() and self.logged_on:
            with contextlib.suppress(Exception):
                await self._send("5", [(58, text)])
                await asyncio.sleep(0.1)
        await self.disconnect()

    async def disconnect(self) -> None:
        self.logged_on = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        if self._writer:
            with contextlib.suppress(Exception):
                self._writer.close()
        self.store.save(self.seq)

    async def __aenter__(self) -> FixSession:
        await self.logon()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.logout()

    # -- application messages ----------------------------------------------------------------------
    async def new_order_single(self, order: OrderRequest) -> None:
        """35=D. ``order.type`` BID/FIRM_BID borrow (54=1), OFFER lends (54=2)."""
        side = "2" if order.type == "OFFER" else "1"
        await self._send("D", [
            (11, order.clientOrderId), *_security(order.instrument), (54, side), (38, order.quantity),
            (40, "2"), (44, order.feeBps), (20008, order.type), (20001, order.feeBps),
            (20010, order.counterpartyCode), (20002, order.term), (20005, _fix_date(order.termEndDate)),
            (20003, order.collateralType), (15, order.collateralCcy), (20004, order.marginPct),
            (64, _fix_date(order.settlementDate)), (20006, order.legalAgreement), (20007, order.bookingEntity),
            (60, _now()),
        ])

    async def counter(self, orig_cl_ord_id: str, cl_ord_id: str, fee_bps: float, quantity: int | None = None) -> None:
        """35=G: answer a lender's counter with new terms."""
        await self._send("G", [(41, orig_cl_ord_id), (11, cl_ord_id), (20001, fee_bps), (44, fee_bps),
                               (38, quantity), (40, "2"), (60, _now())])

    async def accept(self, orig_cl_ord_id: str, cl_ord_id: str) -> None:
        """35=U1 (venue-defined SLAccept): take the lender's counter as it stands."""
        await self._send("U1", [(41, orig_cl_ord_id), (11, cl_ord_id), (60, _now())])

    async def cancel(self, orig_cl_ord_id: str, cl_ord_id: str) -> None:
        """35=F."""
        await self._send("F", [(41, orig_cl_ord_id), (11, cl_ord_id), (60, _now())])

    async def order_status(self, *, order_id: str | None = None, cl_ord_id: str | None = None,
                           req_id: str | None = None) -> ExecutionReport:
        """35=H by venue OrderID (37) or ClOrdID (11); returns the ExecutionReport with ExecType I.
        An unknown order comes back REJECTED with OrdRejReason 5."""
        if not (order_id or cl_ord_id):
            raise ValueError("give order_id or cl_ord_id")
        req_id = req_id or f"OSR-{next(self._ids)}"
        await self._send("H", [(37, order_id), (11, cl_ord_id), (790, req_id)])
        return await self.wait_for(lambda m: isinstance(m, ExecutionReport) and m.status_req_id == req_id)

    async def market_data(self, instruments: Iterable[str], subscribe: bool = False,
                          req_id: str | None = None) -> list[MarketData]:
        """35=V for lendable supply. Returns one snapshot (W) per instrument; with ``subscribe`` the
        venue then streams incrementals (X) for them into ``events``."""
        refs = list(instruments)
        req_id = req_id or f"MDR-{next(self._ids)}"
        fields: list[tuple[int, Any]] = [(262, req_id), (263, "1" if subscribe else "0"), (264, 0), (265, 1),
                                         (267, 1), (269, 1), (146, len(refs))]
        for ref in refs:
            fields += _security(ref)
        await self._send("V", fields)
        def answers(m: Any) -> bool:
            if isinstance(m, MarketData):
                return m.snapshot and m.req_id == req_id
            return isinstance(m, Rejected) and m.kind == "MARKET_DATA_REJECT" and m.ref == req_id

        out: list[MarketData] = []
        while len(out) < len(refs):
            m = await self.wait_for(lambda m: answers(m) and m not in out)
            if isinstance(m, Rejected):
                raise ValueError(f"market data rejected: {m.text}")
            out.append(m)
        return out

    async def unsubscribe_market_data(self, req_id: str) -> None:
        await self._send("V", [(262, req_id), (263, "2"), (264, 0), (146, 0)])

    async def messages(self) -> AsyncIterator[Any]:
        """Every application message from now on, typed, in sequence order."""
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._subscribers.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.remove(q)

    async def wait_for(self, pred: Callable[[Any], bool], timeout: float = 30.0) -> Any:  # noqa: ASYNC109
        """First application message matching ``pred``: from recent history, else the next to arrive."""
        for m in list(self.history):
            if pred(m):
                return m
        w = _Waiter(pred)
        self._waiters.append(w)
        try:
            return await asyncio.wait_for(w.fut, timeout)
        finally:
            with contextlib.suppress(ValueError):
                self._waiters.remove(w)

    # -- wire --------------------------------------------------------------------------------------
    async def _send(self, msg_type: str, fields: Iterable[tuple[int, Any]], seq: int | None = None,
                    poss_dup: bool = False) -> None:
        async with self._send_lock:
            m = simplefix.FixMessage()
            m.append_pair(8, BEGIN_STRING, header=True)
            m.append_pair(35, msg_type, header=True)
            m.append_pair(49, self.sender, header=True)
            m.append_pair(56, self.target, header=True)
            m.append_pair(34, seq if seq is not None else self.seq.next_out, header=True)
            m.append_pair(52, _now(), header=True)
            if poss_dup:
                m.append_pair(43, "Y", header=True)
            for tag, value in fields:
                if value is not None and value != "":
                    m.append_pair(tag, value)
            if seq is None:
                self.seq.next_out += 1
                self.store.save(self.seq)
            assert self._writer is not None, "not connected"
            self._writer.write(m.encode())
            await self._writer.drain()
            self._last_out = asyncio.get_running_loop().time()

    async def _read_loop(self) -> None:
        parser = simplefix.FixParser()
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(65536)
                if not data:
                    self._closed("venue closed the connection")
                    return
                self._last_in = asyncio.get_running_loop().time()
                parser.append_buffer(data)
                while (raw := parser.get_message()) is not None:
                    await self._on_message(_wrap(raw))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - surfaced to waiters
            log.exception("FIX read loop failed")
            self._closed(str(e))

    async def _on_message(self, m: FixMessage) -> None:
        mt = m.msg_type
        if mt == "4" and m.opt(123) != "Y":  # SequenceReset-Reset
            self.seq.next_in = int(m.fields[36])
        elif mt == "4":  # gap fill
            self.seq.next_in = max(self.seq.next_in, int(m.fields[36]))
        elif m.seq > self.seq.next_in and not m.poss_dup and mt != "5":
            # We missed messages (e.g. reports raised while we were offline): ask for them again.
            await self._send("2", [(7, self.seq.next_in), (16, 0)])
            self.seq.next_in = m.seq + 1
        elif not m.poss_dup or m.seq >= self.seq.next_in:
            self.seq.next_in = max(self.seq.next_in, m.seq + 1)
        self.store.save(self.seq)

        if mt == "A":
            if not self._logon_ack.done():
                self._logon_ack.set_result(m)
        elif mt == "5":
            text = m.opt(58) or "logged out"
            if not self._logon_ack.done():
                self._logon_ack.set_exception(FixLogonError(text))
            self._closed(text)
        elif mt == "1":
            await self._send("0", [(112, m.opt(112))])
        elif mt == "2":
            # The venue missed some of ours: gap-fill rather than replay stale orders.
            begin = int(m.get(7, "1") or 1)
            await self._send("4", [(123, "Y"), (36, self.seq.next_out)], seq=begin, poss_dup=True)
        elif mt not in ADMIN_TYPES or mt == "3":
            event = _parse(m)
            self.history.append(event)
            for q in self._subscribers:
                q.put_nowait(event)
            for w in list(self._waiters):
                if not w.fut.done() and w.pred(event):
                    w.fut.set_result(event)
                    break

    async def _heartbeat_loop(self) -> None:
        loop = asyncio.get_running_loop()
        test_sent = False
        while True:
            await asyncio.sleep(min(self.heartbeat_s / 4, 1.0))
            now = loop.time()
            if self.logged_on and now - self._last_out >= self.heartbeat_s:
                await self._send("0", [])
            silence = now - self._last_in
            if self.logged_on and silence > self.heartbeat_s * 1.2 and not test_sent:
                await self._send("1", [(112, f"T{int(now)}")])
                test_sent = True
            elif silence < self.heartbeat_s:
                test_sent = False

    def _closed(self, reason: str) -> None:
        self.logged_on = False
        self.closed_reason = reason
        for w in self._waiters:
            if not w.fut.done():
                w.fut.set_exception(ConnectionError(f"FIX session closed: {reason}"))


# -- parsing -------------------------------------------------------------------------------------
def _wrap(raw: simplefix.FixMessage) -> FixMessage:
    pairs = [(int(t), v.decode()) for t, v in raw.pairs]
    fields = dict(pairs)
    return FixMessage(msg_type=fields.get(35, ""), seq=int(fields.get(34, "0")), poss_dup=fields.get(43) == "Y",
                      fields=fields, pairs=pairs, raw=raw.encode(raw=True))


def _f(m: FixMessage, tag: int) -> float | None:
    v = m.get(tag)
    return float(v) if v not in (None, "") else None


def _i(m: FixMessage, tag: int) -> int | None:
    v = _f(m, tag)
    return int(v) if v is not None else None


def _parse(m: FixMessage) -> Any:
    mt = m.msg_type
    if mt == "8":
        return ExecutionReport(
            order_id=m.get(37, ""), cl_ord_id=m.get(11, ""), exec_id=m.get(17, ""),
            exec_type=EXEC_TYPE.get(m.get(150, ""), m.get(150, "")),
            ord_status=ORD_STATUS.get(m.get(39, ""), m.get(39, "")), isin=m.opt(48),
            side={"1": "BORROW", "2": "LEND"}.get(m.get(54, "")), quantity=_i(m, 38) or 0, fee_bps=_f(m, 20001),
            counter_fee_bps=_f(m, 20009), counter_quantity=_i(m, 20011), last_qty=_i(m, 32),
            last_fee_bps=_f(m, 31), venue_trade_id=m.opt(20012), status_req_id=m.opt(790), text=m.opt(58),
            message=m)
    if mt == "AE":
        return TradeCaptureReport(
            trade_report_id=m.get(571, ""), venue_trade_id=m.get(20012, ""), uti=m.get(20013, ""),
            order_id=m.get(37, ""), cl_ord_id=m.get(11, ""), side={"1": "BORROW", "2": "LEND"}.get(m.get(54, ""), ""),
            isin=m.get(48, ""), quantity=_i(m, 32) or 0, fee_bps=_f(m, 31) or 0.0, trade_date=m.get(75, ""),
            settle_date=m.get(64, ""), contra_party=m.opt(448), message=m)
    if mt in ("W", "X"):
        return _market_data(m)
    if mt in ("3", "9", "Y"):
        ref = {"3": m.opt(45), "9": m.opt(41), "Y": m.opt(262)}[mt]
        kind = {"3": "SESSION", "9": "CANCEL_REJECT", "Y": "MARKET_DATA_REJECT"}[mt]
        return Rejected(kind=kind, ref=ref, text=m.opt(58), message=m)
    return m


def _market_data(m: FixMessage) -> MarketData:
    entries: list[MarketDataEntry] = []
    cur: dict[int, str] = {}
    starts = 279 if m.msg_type == "X" else 269  # first tag of each repeating MDEntry

    def flush() -> None:
        if cur:
            entries.append(MarketDataEntry(
                lender_code=cur.get(20010, ""), fee_bps=float(cur.get(270, "0")),
                quantity=int(float(cur.get(271, "0"))),
                action={"0": "NEW", "1": "CHANGE", "2": "DELETE"}.get(cur.get(279, ""), "NEW"), isin=cur.get(48)))

    in_group = False
    for tag, value in m.pairs:
        if tag == 268:
            in_group = True
            continue
        if not in_group or tag == 10:
            continue
        if tag == starts:
            flush()
            cur = {}
        cur[tag] = value
    flush()
    return MarketData(req_id=m.get(262, ""), snapshot=m.msg_type == "W",
                      isin=m.opt(48) if m.msg_type == "W" else (entries[0].isin if entries else None),
                      entries=entries, message=m)


def _security(ref: str) -> list[tuple[int, Any]]:
    """SecurityID + SecurityIDSource from the identifier's shape, or Symbol for 'TICK XX'."""
    ref = ref.strip()
    if " " in ref:
        return [(55, ref)]
    if len(ref) == 12 and ref[:2].isalpha():
        return [(48, ref), (22, 4)]
    if len(ref) == 9:
        return [(48, ref), (22, 1)]
    if len(ref) == 7:
        return [(48, ref), (22, 2)]
    return [(48, ref), (22, 8)]


def _fix_date(d: date | None) -> str | None:
    return d.strftime("%Y%m%d") if d else None


def _now() -> str:
    from datetime import UTC, datetime

    ts = datetime.now(UTC)
    return ts.strftime("%Y%m%d-%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
