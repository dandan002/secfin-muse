"""FIX 4.4-style session acceptor for Venue A.

This is our own stand-in (the real venue protocol is proprietary): standard FIX 4.4 session
handling (Logon, Heartbeat, TestRequest, ResendRequest, SequenceReset, Logout, Reject) and
standard order messages, with securities-lending economics in user-defined tags 20001+.
See ../API.md for the full tag list.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from mockcore import clock
from mockcore.app import ServiceContext
from refdata.counterparties import OUR_FIRM
from refdata.universe import instrument_by_id, resolve

from . import engine
from .models import FixMessage, FixSession

log = logging.getLogger(__name__)
SOH = "\x01"
BEGIN = "FIX.4.4"
VENUE_COMP_ID = "VENUEA"

# Tags
T = {
    "BeginString": 8, "BodyLength": 9, "MsgType": 35, "SenderCompID": 49, "TargetCompID": 56, "MsgSeqNum": 34,
    "SendingTime": 52, "CheckSum": 10, "PossDupFlag": 43, "OrigSendingTime": 122, "EncryptMethod": 98,
    "HeartBtInt": 108, "ResetSeqNumFlag": 141, "Password": 554, "TestReqID": 112, "BeginSeqNo": 7,
    "EndSeqNo": 16, "NewSeqNo": 36, "GapFillFlag": 123, "RefSeqNum": 45, "SessionRejectReason": 373,
    "Text": 58, "ClOrdID": 11, "OrigClOrdID": 41, "OrderID": 37, "ExecID": 17, "ExecType": 150, "OrdStatus": 39,
    "Symbol": 55, "SecurityID": 48, "SecurityIDSource": 22, "Side": 54, "OrderQty": 38, "OrdType": 40,
    "Price": 44, "Currency": 15, "SettlDate": 64, "TransactTime": 60, "LastQty": 32, "LastPx": 31,
    "CumQty": 14, "LeavesQty": 151, "AvgPx": 6, "OrdRejReason": 103, "CxlRejResponseTo": 434,
    "CxlRejReason": 102, "ExpireTime": 126, "ExecRestatementReason": 378,
    # User-defined (Venue A stand-in)
    "FeeBps": 20001, "Term": 20002, "CollateralType": 20003, "MarginPct": 20004, "TermEndDate": 20005,
    "LegalAgreement": 20006, "BookingEntity": 20007, "SLOrderType": 20008, "CounterFeeBps": 20009,
    "CounterpartyCode": 20010, "CounterQty": 20011, "VenueTradeID": 20012, "UTI": 20013,
}
ID_SOURCE = {"4": "ISIN", "2": "SEDOL", "1": "CUSIP", "8": "TICKER"}
STATUS = {  # our status -> (ExecType, OrdStatus)
    "PENDING": ("0", "0"),
    "COUNTERED": ("D", "0"),
    "EXECUTED": ("F", "2"),
    "CANCELLED": ("4", "4"),
    "EXPIRED": ("C", "C"),
    "REJECTED": ("8", "8"),
}


def fix_time(ts: datetime | None = None) -> str:
    ts = (ts or clock.now()).astimezone(UTC)
    return ts.strftime("%Y%m%d-%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


def checksum(s: str) -> str:
    return f"{sum(s.encode()) % 256:03d}"


def encode(msg_type: str, fields: Iterable[tuple[int, Any]], sender: str, target: str, seq: int,
           sending_time: str | None = None, extra_header: Iterable[tuple[int, Any]] = ()) -> str:
    header = [(35, msg_type), (49, sender), (56, target), (34, seq), (52, sending_time or fix_time()),
              *extra_header]
    body = "".join(f"{t}={v}{SOH}" for t, v in [*header, *fields] if v is not None and v != "")
    head = f"8={BEGIN}{SOH}9={len(body.encode())}{SOH}"
    return f"{head}{body}10={checksum(head + body)}{SOH}"


class FixParseError(Exception):
    pass


def decode(raw: str) -> list[tuple[int, str]]:
    if not raw.startswith(f"8={BEGIN}{SOH}"):
        raise FixParseError("bad BeginString")
    pairs = []
    for part in raw.split(SOH):
        if not part:
            continue
        tag, _, val = part.partition("=")
        if not tag.isdigit():
            raise FixParseError(f"bad field {part!r}")
        pairs.append((int(tag), val))
    if pairs[-1][0] != 10:
        raise FixParseError("missing checksum")
    body_start = raw.index(SOH, raw.index(f"{SOH}9=") + 1) + 1
    body_end = raw.rindex("10=")
    if int(pairs[1][1]) != len(raw[body_start:body_end].encode()):
        raise FixParseError("BodyLength mismatch")
    if checksum(raw[:body_end]) != pairs[-1][1]:
        raise FixParseError("CheckSum mismatch")
    return pairs


def as_dict(pairs: list[tuple[int, str]]) -> dict[int, str]:
    return {t: v for t, v in pairs}


class FrameReader:
    """Splits a TCP byte stream into complete FIX messages."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, data: str) -> list[str]:
        self.buf += data
        out = []
        while True:
            start = self.buf.find(f"8={BEGIN}")
            if start < 0:
                self.buf = ""
                return out
            end = self.buf.find(f"{SOH}10=", start)
            if end < 0 or len(self.buf) < end + 8:
                self.buf = self.buf[start:]
                return out
            stop = self.buf.find(SOH, end + 4)
            if stop < 0:
                self.buf = self.buf[start:]
                return out
            out.append(self.buf[start : stop + 1])
            self.buf = self.buf[stop + 1 :]


def participant_password(ctx: ServiceContext, comp_id: str) -> str:
    """Derived from the key ring's master secret, like the REST keys (see mockcore.keys)."""
    return ctx.gate.derive("fix-password", comp_id, n=24)


ALLOWED_CLIENTS = {"LOANDESK": "0001"}  # SenderCompID -> venue participant code


async def queue_out(ctx: ServiceContext, session_id: str, msg_type: str, fields: list[tuple[int, Any]],
                    persist: bool = True, writer: asyncio.StreamWriter | None = None) -> None:
    """Allocate the next outbound seq, store the message, and write it if the client is connected.

    Application messages raised while the client is logged off are still stored, so the client
    sees a sequence gap on its next logon and recovers them with a ResendRequest.
    """
    client = session_id.split("-", 1)[0]
    async with ctx.session() as s:
        st = await s.get(FixSession, session_id, with_for_update=True)
        seq = st.next_out
        st.next_out += 1
        raw = encode(msg_type, fields, VENUE_COMP_ID, client, seq)
        if persist:
            s.add(FixMessage(session_id=session_id, direction="OUT", seq=seq, msg_type=msg_type, raw=raw,
                             ts=clock.now()))
        await s.commit()
    if writer is None:
        return
    out = ctx.faults.outbound(raw) if msg_type in ("8", "AE") else [raw]  # chaos drills hit order/trade flow
    for m in out:
        writer.write(m.encode())
    await writer.drain()


class Session:
    def __init__(self, ctx: ServiceContext, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.ctx = ctx
        self.reader = reader
        self.writer = writer
        self.client: str | None = None
        self.session_id: str | None = None
        self.hb_int = 30
        self.last_out = asyncio.get_running_loop().time()
        self.last_in = self.last_out
        self.closed = False
        self._send_lock = asyncio.Lock()
        self.md_subs: dict[str, set[str] | None] = {}  # MDReqID -> instrument ids (None = all)

    # -- output ------------------------------------------------------------------------------
    async def send(self, msg_type: str, fields: list[tuple[int, Any]], persist: bool = True) -> None:
        async with self._send_lock:
            await queue_out(self.ctx, self.session_id, msg_type, fields, persist, writer=self.writer)
            self.last_out = asyncio.get_running_loop().time()

    async def reject(self, ref_seq: int, reason: int, text: str) -> None:
        await self.send("3", [(45, ref_seq), (373, reason), (58, text)])

    # -- main loop -----------------------------------------------------------------------------
    async def run(self) -> None:
        frames = FrameReader()
        hb = asyncio.create_task(self._heartbeats())
        try:
            while not self.closed:
                data = await self.reader.read(65536)
                if not data:
                    break
                self.last_in = asyncio.get_running_loop().time()
                for raw in frames.feed(data.decode(errors="replace")):
                    await self.handle(raw)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            hb.cancel()
            await self._mark_logged_off()
            with contextlib.suppress(Exception):
                self.writer.close()

    async def _heartbeats(self) -> None:
        loop = asyncio.get_running_loop()
        test_sent = False
        while not self.closed:
            await asyncio.sleep(min(self.hb_int / 4, 1.0))
            if not self.session_id:
                continue
            if loop.time() - self.last_out >= self.hb_int:
                await self.send("0", [], persist=False)
            silence = loop.time() - self.last_in
            if silence > self.hb_int * 1.2 and not test_sent:
                await self.send("1", [(112, f"TEST-{int(loop.time())}")], persist=False)
                test_sent = True
            elif silence > self.hb_int * 2.5:
                await self.send("5", [(58, "heartbeat timeout")], persist=False)
                self.closed = True
            elif silence < self.hb_int:
                test_sent = False

    async def _mark_logged_off(self) -> None:
        if self.session_id:
            async with self.ctx.session() as s:
                st = await s.get(FixSession, self.session_id)
                if st:
                    st.logged_on = False
                    await s.commit()

    # -- input -----------------------------------------------------------------------------
    async def handle(self, raw: str) -> None:
        try:
            pairs = decode(raw)
        except FixParseError as e:
            log.info("dropping garbled FIX message: %s", e)
            return  # garbled messages are ignored per FIX session rules
        f = as_dict(pairs)
        msg_type = f.get(35, "")
        seq = int(f.get(34, "0"))
        if self.session_id is None:
            if msg_type != "A":
                self.writer.write(encode("5", [(58, "first message must be Logon")], VENUE_COMP_ID,
                                         f.get(49, "?"), 1).encode())
                self.closed = True
                return
            await self.logon(f, raw)
            return
        if self.ctx.faults.config.outage:
            await self.send("5", [(58, "venue unavailable")], persist=False)
            self.closed = True
            return
        async with self.ctx.session() as s:
            st = await s.get(FixSession, self.session_id, with_for_update=True)
            expected = st.next_in
            poss_dup = f.get(43) == "Y"
            if msg_type == "4" and f.get(123) != "Y":  # SequenceReset-Reset
                st.next_in = int(f[36])
                await s.commit()
                return
            if seq > expected and msg_type != "2":
                await s.commit()
                await self.send("2", [(7, expected), (16, 0)], persist=False)
                return
            if seq < expected:
                await s.commit()
                if not poss_dup:
                    await self.send("5", [(58, f"MsgSeqNum too low, expecting {expected} but received {seq}")],
                                    persist=False)
                    self.closed = True
                return
            st.next_in = (int(f[36]) if msg_type == "4" else seq + 1)
            s.add(FixMessage(session_id=self.session_id, direction="IN", seq=seq, msg_type=msg_type, raw=raw,
                             ts=clock.now()))
            await s.commit()
        await self.dispatch(msg_type, f, seq, pairs)

    async def logon(self, f: dict[int, str], raw: str) -> None:
        client = f.get(49, "")
        if f.get(56) != VENUE_COMP_ID or client not in ALLOWED_CLIENTS:
            self.writer.write(encode("5", [(58, "unknown CompID")], VENUE_COMP_ID, client or "?", 1).encode())
            self.closed = True
            return
        if not hmac.compare_digest(f.get(554, ""), participant_password(self.ctx, client)):
            self.writer.write(encode("5", [(58, "bad password")], VENUE_COMP_ID, client, 1).encode())
            self.closed = True
            return
        if self.ctx.faults.config.outage:
            self.writer.write(encode("5", [(58, "venue unavailable")], VENUE_COMP_ID, client, 1).encode())
            self.closed = True
            return
        self.client = client
        self.session_id = f"{client}-{VENUE_COMP_ID}"
        self.hb_int = max(int(f.get(108, "30")), 1)
        seq = int(f.get(34, "1"))
        async with self.ctx.session() as s:
            st = await s.get(FixSession, self.session_id, with_for_update=True)
            if st is None:
                st = FixSession(session_id=self.session_id, next_in=1, next_out=1)
                s.add(st)
            if f.get(141) == "Y":
                st.next_in, st.next_out = 1, 1
            gap = seq > st.next_in
            st.next_in = st.next_in if gap else seq + 1
            st.logged_on = True
            st.updated_at = clock.now()
            s.add(FixMessage(session_id=self.session_id, direction="IN", seq=seq, msg_type="A", raw=raw,
                             ts=clock.now()))
            expected = st.next_in
            await s.commit()
        await self.send("A", [(98, 0), (108, self.hb_int), (141, f.get(141))], persist=False)
        if gap:
            await self.send("2", [(7, expected), (16, 0)], persist=False)
        self.ctx.fix_sessions[self.session_id] = self

    async def dispatch(self, msg_type: str, f: dict[int, str], seq: int,
                       pairs: list[tuple[int, str]] | None = None) -> None:
        if msg_type == "0":
            return
        if msg_type == "1":
            await self.send("0", [(112, f.get(112))], persist=False)
        elif msg_type == "2":
            await self.resend(int(f.get(7, "1")), int(f.get(16, "0")))
        elif msg_type == "5":
            await self.send("5", [(58, "bye")], persist=False)
            self.closed = True
        elif msg_type == "4":
            return
        elif msg_type == "D":
            await self.new_order(f, seq)
        elif msg_type == "G":
            await self.replace(f, seq)
        elif msg_type == "F":
            await self.cancel(f, seq)
        elif msg_type == "U1":
            await self.accept(f, seq)
        elif msg_type == "H":
            await self.order_status(f)
        elif msg_type == "V":
            await self.market_data_request(f, pairs or [])
        else:
            await self.reject(seq, 11, f"unsupported MsgType {msg_type}")

    async def resend(self, begin: int, end: int) -> None:
        async with self.ctx.session() as s:
            st = await s.get(FixSession, self.session_id)
            last = st.next_out - 1
            end = last if end == 0 else min(end, last)
            rows = (await s.execute(select(FixMessage).where(
                FixMessage.session_id == self.session_id, FixMessage.direction == "OUT",
                FixMessage.seq >= begin, FixMessage.seq <= end).order_by(FixMessage.seq))).scalars().all()
        by_seq = {m.seq: m for m in rows}
        n = begin
        async with self._send_lock:
            while n <= end:
                m = by_seq.get(n)
                if m is None:  # admin message or not stored: gap-fill to the next stored one
                    nxt = min([q for q in by_seq if q > n], default=end + 1)
                    raw = encode("4", [(123, "Y"), (36, nxt)], VENUE_COMP_ID, self.client, n,
                                 extra_header=[(43, "Y")])
                    n = nxt
                else:
                    f = as_dict(decode(m.raw))
                    body = [(t, v) for t, v in decode(m.raw) if t not in (8, 9, 10, 35, 49, 56, 34, 52)]
                    raw = encode(m.msg_type, body, VENUE_COMP_ID, self.client, n,
                                 extra_header=[(43, "Y"), (122, f[52])])
                    n += 1
                self.writer.write(raw.encode())
            await self.writer.drain()

    # -- application messages --------------------------------------------------------------------
    def _order_request(self, f: dict[int, str]) -> engine.OrderRequest:
        sl_type = f.get(20008) or {"1": "BID", "2": "OFFER"}.get(f.get(54, ""), "")
        instrument = f.get(48) or f.get(55, "")
        settle = f.get(64, "")
        term_end = f.get(20005)
        return engine.OrderRequest(
            clientOrderId=f.get(11, ""),
            type=sl_type,
            instrument=instrument,
            quantity=int(float(f.get(38, "0"))),
            feeBps=float(f.get(20001, f.get(44, "0"))),
            counterpartyCode=f.get(20010),
            term=f.get(20002, "OPEN"),
            termEndDate=_fix_date(term_end) if term_end else None,
            collateralType=f.get(20003, ""),
            collateralCcy=f.get(15, ""),
            marginPct=float(f.get(20004, "0")),
            settlementDate=_fix_date(settle) if settle else None,
            legalAgreement=f.get(20006, ""),
            bookingEntity=f.get(20007, ""),
        )

    async def new_order(self, f: dict[int, str], seq: int) -> None:
        try:
            req = self._order_request(f)
        except (ValidationError, ValueError) as e:
            await self._exec_reject(f, f"invalid order: {_first_error(e)}")
            return
        async with self.ctx.session() as s:
            try:
                await engine.place(self.ctx, s, req, ALLOWED_CLIENTS[self.client], "FIX", self.session_id)
                await s.commit()
            except engine.VenueError as e:
                await s.rollback()
                await self._exec_reject(f, f"{e.code}: {e}")

    async def replace(self, f: dict[int, str], seq: int) -> None:
        async with self.ctx.session() as s:
            try:
                o = await engine.get_order(s, f.get(41, ""), lock=True)
                o.client_order_id = f.get(11, o.client_order_id)
                await engine.counter(self.ctx, s, o, float(f.get(20001, f.get(44, "0"))),
                                     int(float(f[38])) if 38 in f else None)
                await s.commit()
            except engine.VenueError as e:
                await s.rollback()
                await self._cancel_reject(f, "2", str(e))

    async def cancel(self, f: dict[int, str], seq: int) -> None:
        async with self.ctx.session() as s:
            try:
                o = await engine.get_order(s, f.get(41, ""), lock=True)
                await engine.cancel(self.ctx, s, o)
                await s.commit()
            except engine.VenueError as e:
                await s.rollback()
                await self._cancel_reject(f, "1", str(e))

    async def accept(self, f: dict[int, str], seq: int) -> None:
        async with self.ctx.session() as s:
            try:
                o = await engine.get_order(s, f.get(41, ""), lock=True)
                await engine.accept(self.ctx, s, o)
                await s.commit()
            except engine.VenueError as e:
                await s.rollback()
                await self.reject(seq, 99, str(e))

    async def order_status(self, f: dict[int, str]) -> None:
        """OrderStatusRequest (H) -> ExecutionReport with ExecType I (Order Status)."""
        ref = f.get(37) or f.get(11, "")
        async with self.ctx.session() as s:
            o = await engine._get(s, ref)
            data = engine.order_json(o) if o else None
        if data is None:
            await self.send("8", [(37, "NONE"), (11, f.get(11)), (17, f"STATUS-{ref}"), (150, "I"), (39, "8"),
                                  (790, f.get(790)), (14, 0), (151, 0), (6, 0), (103, 5),
                                  (58, f"UNKNOWN_ORDER: no order {ref}"), (60, fix_time())], persist=False)
            return
        fields = order_fields({"type": "status", "eventId": f"STATUS-{f.get(790) or ref}", "data": data})
        fields = [(150, "I") if t == 150 else (t, v) for t, v in fields] + [(790, f.get(790))]
        await self.send("8", fields, persist=False)

    async def market_data_request(self, f: dict[int, str], pairs: list[tuple[int, str]]) -> None:
        """MarketDataRequest (V) for lendable supply: W snapshot, X incrementals on subscribe, Y reject."""
        req_id = f.get(262, "")
        sub_type = f.get(263, "0")
        if sub_type == "2":  # unsubscribe
            self.md_subs.pop(req_id, None)
            return
        refs = [v for t, v in pairs if t in (48, 55)] if f.get(146, "0") != "0" else []
        wanted: list[str] = []
        for ref in refs:
            inst = resolve(ref)
            if inst is None:
                await self.send("Y", [(262, req_id), (281, 0), (58, f"unknown instrument {ref}")], persist=False)
                return
            wanted.append(inst.id)
        if not wanted and sub_type == "0":
            await self.send("Y", [(262, req_id), (281, 0), (58, "snapshot needs at least one instrument")],
                            persist=False)
            return
        async with self.ctx.session() as s:
            for iid in wanted:
                rows = await engine.availability(s, iid)
                await self.send("W", md_snapshot(req_id, rows), persist=False)
        if sub_type == "1":
            self.md_subs[req_id] = set(wanted) or None

    async def on_availability(self, env: dict[str, Any]) -> None:
        data = env["data"]
        for req_id, ids in list(self.md_subs.items()):
            if ids is None or data["instrumentId"] in ids:
                await self.send("X", md_incremental(req_id, data), persist=False)

    async def _exec_reject(self, f: dict[int, str], text: str) -> None:
        await self.send("8", [(37, "NONE"), (11, f.get(11)), (17, f"REJ-{f.get(11)}"), (150, "8"), (39, "8"),
                              (55, f.get(55)), (48, f.get(48)), (54, f.get(54)), (38, f.get(38)), (14, 0),
                              (151, 0), (6, 0), (103, 99), (58, text[:200]), (60, fix_time())])

    async def _cancel_reject(self, f: dict[int, str], response_to: str, text: str) -> None:
        await self.send("9", [(37, "NONE"), (11, f.get(11)), (41, f.get(41)), (39, "8"), (434, response_to),
                              (102, 99), (58, text[:200])])

    async def on_order_event(self, env: dict[str, Any]) -> None:
        await self.send("8", order_fields(env))

    async def on_trade_event(self, env: dict[str, Any]) -> None:
        await self.send("AE", trade_fields(env))


def order_fields(env: dict[str, Any]) -> list[tuple[int, Any]]:
    o = env["data"]
    exec_type, ord_status = STATUS[o["status"]]
    if env["type"] == "order_replaced":
        exec_type = "5"
    fields: list[tuple[int, Any]] = [
        (37, o["orderId"]), (11, o["clientOrderId"]), (17, f"{o['orderId']}-{env['eventId']}"),
        (150, exec_type), (39, ord_status), (55, o["composite"]), (48, o["isin"]), (22, 4),
        (54, "1" if o["side"] == "BORROW" else "2"), (38, o["quantity"]), (20001, o["feeBps"]),
        (20008, o["type"]), (20010, o["counterpartyCode"]), (60, fix_time()),
    ]
    if o["status"] == "EXECUTED":
        last = o["history"][-1]
        fields += [(32, o["quantity"]), (31, o["feeBps"]), (14, o["quantity"]), (151, 0), (6, o["feeBps"]),
                   (20012, last.get("tradeId"))]
    else:
        fields += [(14, 0), (151, 0 if o["status"] in engine.TERMINAL else o["quantity"]), (6, 0)]
    if o["status"] == "COUNTERED":
        fields += [(378, 99), (20009, o["counterFeeBps"]), (20011, o["counterQuantity"]),
                   (58, f"lender counter {o['counterFeeBps']}bps")]
    if o["rejectReason"]:
        fields += [(103, 99), (58, o["rejectReason"][:200])]
    return fields


def trade_fields(env: dict[str, Any]) -> list[tuple[int, Any]]:
    """TradeCaptureReport (AE): the venue's trade confirmation, carrying the UTI.

    FIX 4.4 has no UTI tag (FIX 5.0 SP2 adds RegulatoryTradeID 1903), so it rides in 20013.
    """
    t = env["data"]
    we_borrow = t["borrower"]["code"] == OUR_FIRM["ngt_code"]
    contra = t["lender"] if we_borrow else t["borrower"]
    executed = clock.parse_ts(t["executedAt"])
    return [
        (571, t["tradeId"]), (487, 0), (856, 0), (570, "N"),
        (55, instrument_by_id(t["instrumentId"]).composite), (48, t["isin"]), (22, 4),
        (32, t["quantity"]), (31, t["feeBps"]), (75, executed.astimezone(UTC).strftime("%Y%m%d")),
        (60, fix_time(executed)), (64, t["settlementDate"].replace("-", "")),
        (15, t["collateralCcy"]), (20001, t["feeBps"]), (20002, t["term"]),
        (20005, (t["termEndDate"] or "").replace("-", "")), (20003, t["collateralType"]),
        (20004, t["marginPct"]), (20006, t["legalAgreement"]), (20007, t["bookingEntity"]),
        (20012, t["tradeId"]), (20013, t["uti"]),
        (552, 1), (54, "1" if we_borrow else "2"), (37, t["orderId"]), (11, t["clientOrderId"]),
        (453, 1), (448, contra["code"]), (447, "D"), (452, 17),
    ]


def md_snapshot(req_id: str, rows: list[dict[str, Any]]) -> list[tuple[int, Any]]:
    """MarketDataSnapshotFullRefresh (W): one Offer entry per lender (size = lendable qty, px = fee bps)."""
    first = rows[0] if rows else None
    fields: list[tuple[int, Any]] = [(262, req_id)]
    if first:
        fields += [(55, first["composite"]), (48, first["isin"]), (22, 4)]
    live = [r for r in rows if r["quantity"] > 0]
    fields.append((268, len(live)))
    for r in live:
        fields += [(269, 1), (270, r["indicativeFeeBps"]), (271, r["quantity"]), (20010, r["lenderCode"])]
    return fields


def md_incremental(req_id: str, data: dict[str, Any]) -> list[tuple[int, Any]]:
    """MarketDataIncrementalRefresh (X): every lender's line for the instrument (1 change, 2 delete)."""
    rows = data["byLender"]
    fields: list[tuple[int, Any]] = [(262, req_id), (268, len(rows))]
    for r in rows:
        fields += [(279, 1 if r["quantity"] > 0 else 2), (269, 1), (55, r["composite"]), (48, r["isin"]),
                   (22, 4), (270, r["indicativeFeeBps"]), (271, r["quantity"]), (20010, r["lenderCode"])]
    return fields


def _fix_date(s: str) -> date:
    s = s.strip()
    return date(int(s[:4]), int(s[4:6]), int(s[6:8])) if len(s) == 8 and s.isdigit() else date.fromisoformat(s)


def _first_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        err = e.errors()[0]
        return f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
    return str(e)


async def router(ctx: ServiceContext) -> None:
    """Route order/trade events to the FIX session that owns the order, and availability to
    market-data subscribers."""
    sub = ctx.hub.subscribe(["orders", "trades", "availability"])
    try:
        while True:
            env = await sub.get()
            if env["topic"] == "availability":
                for sess in list(ctx.fix_sessions.values()):
                    if sess.md_subs and not sess.closed:
                        with contextlib.suppress(Exception):
                            await sess.on_availability(env)
                continue
            data = env.get("data", {})
            order_id = data.get("orderId")
            if not order_id:
                continue
            async with ctx.session() as s:
                o = await engine._get(s, order_id)
            if o is None or o.source != "FIX" or not o.fix_session:
                continue
            msg_type, fields = ("8", order_fields(env)) if env["topic"] == "orders" else ("AE", trade_fields(env))
            sess = ctx.fix_sessions.get(o.fix_session)
            try:
                if sess is not None and not sess.closed:
                    await sess.send(msg_type, fields)
                else:  # stored for the client to recover via ResendRequest after its next logon
                    await queue_out(ctx, o.fix_session, msg_type, fields)
            except Exception:
                log.exception("failed to deliver FIX execution report")
    finally:
        sub.close()


async def serve(ctx: ServiceContext, host: str, port: int) -> asyncio.base_events.Server:
    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await Session(ctx, reader, writer).run()

    return await asyncio.start_server(on_connect, host, port)
