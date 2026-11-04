"""Bulk work and targeting: per-line batch decisions, several orders on one confirm, and Venue A's lender ladder."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from loandesk import db, secmaster
from loandesk.adapters import AdapterError, systems
from loandesk.models import Counterparty
from loandesk.services import market


async def _supply(instrument_id: str, qty: int) -> None:
    async with db.session() as s:
        await market.upsert_availability(s, "VENUE_A", [{"instrumentId": instrument_id, "quantity": qty, "lenders": 3,
                                                         "bestFeeBps": 20.0, "asOf": "2026-10-27T13:59:00Z"}])
        await s.commit()


async def _id(q: str) -> str:
    async with db.session() as s:
        return (await secmaster.resolve(s, q)).instrument.id


async def _venue_code(code: str) -> str:
    async with db.session() as s:
        return (await s.execute(select(Counterparty.ngt_code).where(Counterparty.code == code))).scalar_one()


async def test_batch_decides_each_line_at_its_own_size_and_fee(client: httpx.AsyncClient) -> None:
    await _supply("I00039", 10_000)  # DELL, WARM: goes to review
    a, b = (await client.post("/locates", json={"counterparty": "PBBRAVO", "lines": [
        {"identifier": "DELL US", "quantity": 4000}, {"identifier": "DELL US", "quantity": 2000}]})).json()
    waiting = {x["id"]: x for x in (await client.get("/locates", params={"status": "RECEIVED"})).json()}
    floor = waiting[a["id"]]["feeFloorBps"]
    r = (await client.post("/locates/decisions", json={"decision": "APPROVED", "lines": [
        {"id": a["id"], "quantity": 3000, "feeBps": floor + 5}, {"id": b["id"]}]})).json()
    assert len(r["decided"]) == 2 and not r["failed"]
    got = {x["id"]: x for x in (await client.get("/locates", params={"status": "APPROVED,PARTIAL"})).json()}
    assert (got[a["id"]]["status"], got[a["id"]]["qtyApproved"], got[a["id"]]["feeBps"]) == ("PARTIAL", 3000, floor + 5)
    assert (got[b["id"]]["status"], got[b["id"]]["qtyApproved"]) == ("APPROVED", 2000)


async def test_batch_needs_ids_or_lines(client: httpx.AsyncClient) -> None:
    r = await client.post("/locates/decisions", json={"decision": "APPROVED"})
    assert r.status_code == 422


async def test_order_batch_sends_what_passes_and_reports_the_rest(client: httpx.AsyncClient) -> None:
    r = await client.post("/orders/batch", json={"orders": [
        {"instrument": "AAPL US", "quantity": 1000, "feeBps": 35, "override": True},
        {"instrument": "AAPL US", "quantity": 100_000, "feeBps": 900},  # fat finger: blocked
        {"instrument": "MSFT US", "quantity": 2000, "feeBps": 30, "override": True},
    ]}, headers={"x-trader": "sam"})
    body = r.json()
    assert r.status_code == 200
    assert [o["instrumentId"] for o in body["placed"]] == [await _id("AAPL US"), await _id("MSFT US")]
    assert all(o["createdBy"] == "trader:sam" for o in body["placed"])  # the venue is down: they fail visibly
    assert [(f["line"], "fat finger" in f["error"]) for f in body["failed"]] == [(1, True)]


async def test_order_batch_names_the_lender(client: httpx.AsyncClient) -> None:
    body = (await client.post("/orders/batch", json={"orders": [
        {"instrument": "AAPL US", "quantity": 1000, "feeBps": 35, "counterparty": "HARBORAL", "venue": "VENUE_A",
         "type": "BID", "override": True}]})).json()
    assert body["placed"][0]["counterparty"] == "HARBORAL"


@pytest.fixture
def ladder(monkeypatch: pytest.MonkeyPatch, setup: None) -> dict[str, Any]:
    answers: dict[str, Any] = {}

    async def availability(instrument: str | None = None, limit: int = 5000) -> dict[str, Any]:
        a = answers.get(instrument)
        if isinstance(a, Exception):
            raise a
        return {"instruments": [], "byLender": a or []}

    monkeypatch.setattr(systems().venue_a, "availability", availability)
    return answers


async def test_lender_ladder_is_cheapest_first_with_our_codes(client: httpx.AsyncClient, ladder: dict) -> None:
    harbor, rhine = await _venue_code("HARBORAL"), await _venue_code("RHINEAL")
    ladder["US9988883099"] = [  # AAPL US
        {"lenderCode": harbor, "lenderName": "x", "quantity": 500_000, "indicativeFeeBps": 31.0},
        {"lenderCode": rhine, "lenderName": "y", "quantity": 200_000, "indicativeFeeBps": 28.5},
        {"lenderCode": "ZZZ", "lenderName": "Gone", "quantity": 0, "indicativeFeeBps": 10.0},  # nothing to lend
    ]
    r = (await client.get(f"/instruments/{await _id('AAPL US')}/lenders")).json()
    assert r["ok"] and [(x["counterparty"], x["feeBps"], x["quantity"]) for x in r["lenders"]] == [
        ("RHINEAL", 28.5, 200_000), ("HARBORAL", 31.0, 500_000)]


async def test_lenders_for_several_names_say_which_failed(client: httpx.AsyncClient, ladder: dict) -> None:
    ladder["US9988883099"] = [{"lenderCode": await _venue_code("HARBORAL"), "quantity": 1000, "indicativeFeeBps": 30.0}]
    ladder["US9950368293"] = AdapterError("venue_a", None, "unreachable (ConnectError)")  # MSFT US
    aapl, msft = await _id("AAPL US"), await _id("MSFT US")
    r = (await client.get("/lenders", params={"ids": f"{aapl},{msft}"})).json()
    assert r[aapl]["ok"] and r[aapl]["lenders"][0]["counterparty"] == "HARBORAL"
    assert not r[msft]["ok"] and "unreachable" in r[msft]["error"]


async def test_order_commands_take_an_empty_body(client: httpx.AsyncClient) -> None:
    """The desk posts {} for accept and cancel; only a counter needs a fee."""
    o = (await client.post("/orders", json={"instrument": "AAPL US", "quantity": 1000, "feeBps": 35,
                                            "override": True})).json()
    assert (await client.post(f"/orders/{o['id']}/counter", json={})).status_code == 422
    r = await client.post(f"/orders/{o['id']}/cancel", json={})
    assert r.status_code != 422 or "feeBps" not in r.text  # past validation: the venue's answer, not ours
