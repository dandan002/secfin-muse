"""Session layer and parsing against a tiny in-process acceptor (no venue code involved)."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
import simplefix
from ngt_connector import (
    ExecutionReport,
    FileSeqStore,
    FixLogonError,
    FixSession,
    MarketData,
    OrderRequest,
    TradeCaptureReport,
)


class Acceptor:
    """Answers Logon, records what it receives, and lets a test push raw messages."""

    def __init__(self, password: str = "pw") -> None:
        self.password = password
        self.received: list[dict[int, str]] = []
        self.writer: asyncio.StreamWriter | None = None
        self.out_seq = 1

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        parser = simplefix.FixParser()
        while data := await reader.read(65536):
            parser.append_buffer(data)
            while (m := parser.get_message()) is not None:
                f = {int(t): v.decode() for t, v in m.pairs}
                self.received.append(f)
                if f[35] == "A":
                    if f.get(554) != self.password:
                        await self.send("5", [(58, "bad password")])
                    else:
                        await self.send("A", [(98, 0), (108, f[108])])

    async def send(self, msg_type: str, fields: list[tuple[int, object]], seq: int | None = None) -> None:
        m = simplefix.FixMessage()
        for t, v in [(8, "FIX.4.4"), (35, msg_type), (49, "VENUEA"), (56, "LOANDESK"),
                     (34, seq if seq is not None else self.out_seq)]:
            m.append_pair(t, v, header=True)
        if seq is None:
            self.out_seq += 1
        for t, v in fields:
            m.append_pair(t, v)
        assert self.writer is not None
        self.writer.write(m.encode())
        await self.writer.drain()

    async def wait(self, msg_type: str, timeout: float = 3.0) -> dict[int, str]:  # noqa: ASYNC109
        async def poll() -> dict[int, str]:
            while True:
                for f in self.received:
                    if f[35] == msg_type:
                        return f
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout)


ORDER = OrderRequest(clientOrderId="C1", type="BID", instrument="XS0000000001", quantity=250_000, feeBps=25,
                     counterpartyCode="4074", collateralType="CASH", collateralCcy="USD", marginPct=102,
                     settlementDate=date(2026, 10, 28), legalAgreement="MSLA", bookingEntity="LOANDESK-US")


async def test_logon_order_and_typed_reports(tmp_path):
    acc = Acceptor()
    port = await acc.start()
    store = FileSeqStore(tmp_path / "seq.json")
    fix = FixSession("127.0.0.1", port, password="pw", heartbeat_s=1, store=store)
    await fix.logon(reset_seq_nums=True)
    assert acc.received[0][141] == "Y"

    await fix.new_order_single(ORDER)
    d = await acc.wait("D")
    assert (d[11], d[48], d[22], d[54], d[38], d[20008], d[20010], d[64]) == (
        "C1", "XS0000000001", "4", "1", "250000", "BID", "4074", "20261028")

    await acc.send("8", [(37, "NGT-O-00000001"), (11, "C1"), (17, "E1"), (150, "F"), (39, "2"), (48, "XS0000000001"),
                         (54, 1), (38, 250000), (32, 250000), (31, 25), (20012, "NGT-T-00000001")])
    await acc.send("AE", [(571, "NGT-T-00000001"), (487, 0), (856, 0), (48, "XS0000000001"), (22, 4), (32, 250000),
                          (31, 25), (75, "20261027"), (64, "20261028"), (20012, "NGT-T-00000001"),
                          (20013, "UTI123"), (552, 1), (54, 1), (37, "NGT-O-00000001"), (11, "C1"), (453, 1),
                          (448, "4074"), (447, "D"), (452, 17)])
    fill = await fix.wait_for(lambda m: isinstance(m, ExecutionReport) and m.exec_type == "TRADE")
    tcr = await fix.wait_for(lambda m: isinstance(m, TradeCaptureReport))
    assert (fill.last_qty, fill.last_fee_bps, fill.ord_status) == (250000, 25.0, "FILLED")
    assert (tcr.uti, tcr.side, tcr.contra_party, tcr.cl_ord_id) == ("UTI123", "BORROW", "4074", "C1")

    await fix.logout()
    assert store.load().next_out == 4  # Logon, D, Logout
    assert store.load().next_in == 4  # Logon ack, 8, AE


async def test_test_request_gap_and_gap_fill():
    acc = Acceptor()
    port = await acc.start()
    fix = FixSession("127.0.0.1", port, password="pw", heartbeat_s=30)
    await fix.logon(reset_seq_nums=True)

    await acc.send("1", [(112, "ping")])
    assert (await acc.wait("0"))[112] == "ping"

    await acc.send("8", [(37, "X"), (11, "C9"), (150, "0"), (39, "0")], seq=9)  # we expected 3
    rr = await acc.wait("2")
    assert (rr[7], rr[16]) == ("3", "0")

    await acc.send("2", [(7, 1), (16, 0)])  # venue wants ours again: we gap-fill, never replay orders
    gf = await acc.wait("4")
    assert gf[123] == "Y" and gf[43] == "Y" and int(gf[36]) == fix.seq.next_out
    await fix.logout()


async def test_bad_password_raises():
    acc = Acceptor(password="right")
    port = await acc.start()
    fix = FixSession("127.0.0.1", port, password="wrong")
    with pytest.raises(FixLogonError, match="bad password"):
        await fix.logon()
    await fix.disconnect()


async def test_market_data_snapshot_and_incremental_parsing():
    acc = Acceptor()
    port = await acc.start()
    fix = FixSession("127.0.0.1", port, password="pw")
    await fix.logon(reset_seq_nums=True)

    async def venue() -> None:
        v = await acc.wait("V")
        assert (v[263], v[146], v[48]) == ("1", "1", "XS0000000001")
        await acc.send("W", [(262, v[262]), (55, "ABC US"), (48, "XS0000000001"), (22, 4), (268, 2),
                             (269, 1), (270, 30.5), (271, 100000), (20010, "4074"),
                             (269, 1), (270, 31.0), (271, 50000), (20010, "4101")])
        await acc.send("X", [(262, v[262]), (268, 1), (279, 2), (269, 1), (48, "XS0000000001"), (22, 4),
                             (270, 31.0), (271, 0), (20010, "4101")])

    task = asyncio.create_task(venue())
    [snap] = await fix.market_data(["XS0000000001"], subscribe=True, req_id="MD1")
    await task
    inc = await fix.wait_for(lambda m: isinstance(m, MarketData) and not m.snapshot)
    assert [(e.lender_code, e.fee_bps, e.quantity) for e in snap.entries] == [("4074", 30.5, 100000),
                                                                             ("4101", 31.0, 50000)]
    assert [(e.lender_code, e.action) for e in inc.entries] == [("4101", "DELETE")]
    await fix.logout()
