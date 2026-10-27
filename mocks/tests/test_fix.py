"""FIX 4.4-style session tests: codec, logon auth, order flow, and recovery of reports missed offline."""

from __future__ import annotations

import asyncio

import pytest

from mock_ngt.fix import FixParseError, FrameReader, as_dict, checksum, decode, encode
from mock_ngt.fixclient import FixClient

ORDER_TAGS = [(20003, "CASH"), (15, "USD"), (20004, 102), (64, "20261028"), (20006, "MSLA"), (20007, "LOANDESK-US")]


def test_codec_round_trip_and_framing():
    raw = encode("D", [(11, "A1"), (38, 100)], "LOANDESK", "VENUEA", 7, sending_time="20261027-13:00:00.000")
    f = as_dict(decode(raw))
    assert f[35] == "D" and f[34] == "7" and f[11] == "A1"
    assert raw.endswith(f"10={checksum(raw[: raw.rindex('10=')])}\x01")
    with pytest.raises(FixParseError):
        decode(raw.replace("A1", "A2"))  # checksum no longer matches
    r = FrameReader()
    assert r.feed(raw[:20]) == [] and r.feed(raw[20:] + raw) == [raw, raw]


def _client(mocks, **kw) -> FixClient:
    cred = mocks.creds("ngt")["fix"]
    return FixClient("127.0.0.1", mocks.stack.fix_port, password=cred["password"], **kw)


def test_logon_rejects_bad_password(mocks):
    async def go():
        c = FixClient("127.0.0.1", mocks.stack.fix_port, password="wrong")
        with pytest.raises((ConnectionError, asyncio.TimeoutError)):
            await c.connect()
        await c.close(logout=False)

    asyncio.run(go())


def test_bid_counter_accept_over_fix(mocks):
    mocks.verb("ngt", "config", hot_spread=[1.2, 1.2], warm_spread=[1.1, 1.1], counter_band=0.5)
    hot = mocks.get("ngt", "/v1/availability?instrument=@hot:1:US").json()["instruments"][0]

    async def go():
        c = _client(mocks)
        await c.connect()
        await c.send("D", [(11, "FIX-B1"), (48, hot["isin"]), (22, 4), (54, 1), (38, 20000), (20008, "BID"),
                           (20001, round(hot["bestFeeBps"] * 0.9)), *ORDER_TAGS])
        new = await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) == "0")
        await asyncio.to_thread(mocks.advance, 30)
        counter = await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) == "D")
        assert float(counter[20009]) > 0 and counter[37] == new[37]
        await c.send("U1", [(41, "FIX-B1"), (11, "FIX-B1-ACC")])
        fill = await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) == "F")
        uti = await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) == "I")
        assert fill[31] == counter[20009] and uti[20013].startswith("MOCK00VENUEANGT")
        await c.close()

    asyncio.run(go())
    trades = mocks.get("ngt", "/v1/trades", headers={"x-participant-key": mocks.creds("ngt")["participantKey"]}).json()
    assert trades[-1]["clientOrderId"] == "FIX-B1"


def test_reports_raised_while_offline_are_recovered(mocks):
    hot = mocks.get("ngt", "/v1/availability?instrument=@hot:2:US").json()["instruments"][0]

    async def go():
        c = _client(mocks)
        await c.connect()
        await c.send("D", [(11, "FIX-OFF"), (48, hot["isin"]), (22, 4), (54, 1), (38, 1000), (20008, "BID"),
                           (20001, 1), *ORDER_TAGS])  # far below any ask -> rejected once the lender replies
        await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) == "0")
        out_seq, in_seq = c.out_seq, c.expected_in
        await c.close()  # log off before the lender responds
        await asyncio.sleep(0.3)
        await asyncio.to_thread(mocks.advance, 60)
        c2 = _client(mocks)
        c2.out_seq, c2.expected_in = out_seq + 1, in_seq + 1  # +1: the Logout exchange
        await c2.connect(reset=False)
        rej = await c2.wait_for(lambda m: m.get(35) == "8" and m.get(150) == "8", timeout=10)
        assert rej[43] == "Y" and "FEE_TOO_LOW" in rej[58] and rej[11] == "FIX-OFF"
        await c2.close()

    asyncio.run(go())
