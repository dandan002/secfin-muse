"""Minimal FIX initiator for tests, the smoke script and as a reference for our venue adapter."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from .fix import SOH, FrameReader, as_dict, decode, encode


class FixClient:
    def __init__(self, host: str, port: int, sender: str = "LOANDESK", target: str = "VENUEA",
                 password: str = "", hb_int: int = 30) -> None:
        self.host, self.port = host, port
        self.sender, self.target, self.password, self.hb_int = sender, target, password, hb_int
        self.out_seq = 1
        self.expected_in = 1
        self.inbox: asyncio.Queue[dict[int, str]] = asyncio.Queue()
        self.received: list[dict[int, str]] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    async def connect(self, reset: bool = True) -> dict[int, str]:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._task = asyncio.create_task(self._read_loop())
        if reset:
            self.out_seq, self.expected_in = 1, 1
        await self.send("A", [(98, 0), (108, self.hb_int), (141, "Y" if reset else None), (554, self.password)])
        return await self.wait_for(lambda m: m[35] == "A", timeout=5)

    async def send(self, msg_type: str, fields: list[tuple[int, Any]], seq: int | None = None) -> None:
        raw = encode(msg_type, fields, self.sender, self.target, seq or self.out_seq)
        if seq is None:
            self.out_seq += 1
        assert self._writer is not None
        self._writer.write(raw.encode())
        await self._writer.drain()

    async def _read_loop(self) -> None:
        frames = FrameReader()
        assert self._reader is not None
        while True:
            data = await self._reader.read(65536)
            if not data:
                await self.inbox.put({35: "__closed__"})
                return
            for raw in frames.feed(data.decode()):
                m = as_dict(decode(raw))
                m[0] = raw  # type: ignore[assignment]
                seq = int(m.get(34, "0"))
                if m.get(35) == "4" and m.get(123) == "Y":
                    self.expected_in = int(m[36])
                elif m.get(43) != "Y" and seq > self.expected_in and m.get(35) != "2":
                    # Gap: ask the venue to resend what we missed.
                    await self.send("2", [(7, self.expected_in), (16, 0)])
                    self.expected_in = seq + 1
                else:
                    self.expected_in = max(self.expected_in, seq + 1)
                if m.get(35) == "1":
                    await self.send("0", [(112, m.get(112))])
                self.received.append(m)
                await self.inbox.put(m)

    async def wait_for(self, pred, timeout: float = 10.0) -> dict[int, str]:
        async def _wait() -> dict[int, str]:
            while True:
                m = await self.inbox.get()
                if m.get(35) == "__closed__":
                    raise ConnectionError("venue closed the session")
                if pred(m):
                    return m

        return await asyncio.wait_for(_wait(), timeout)

    async def close(self, logout: bool = True) -> None:
        if logout and self._writer and not self._writer.is_closing():
            with contextlib.suppress(Exception):
                await self.send("5", [(58, "bye")])
                await asyncio.sleep(0.05)
        if self._task:
            self._task.cancel()
        if self._writer:
            self._writer.close()


def pretty(m: dict[int, str]) -> str:
    return "|".join(f"{k}={v}" for k, v in m.items() if k != 0).replace(SOH, "|")
