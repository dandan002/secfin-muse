"""Trading on both venues and post-trade: booking, breaks, settlement and the loan's lifecycle.

The outside systems are scripted fakes on the adapters, the way ``test_chat`` scripts Desk Chat; Venue A stays
down (nothing listens on its URL), which is the failover case.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from loandesk import db, sync
from loandesk.adapters import systems
from loandesk.models import Action, Counterparty, Trade
from loandesk.services import booking, lifecycle, orders
from refdata.counterparties import OUR_FIRM

_n = itertools.count(1)


class Processor:
    def __init__(self) -> None:
        self.booked: list[dict[str, Any]] = []
        self.breaks: dict[str, dict[str, Any]] = {}  # contract -> {field: processor value}
        self.calls: list[tuple[Any, ...]] = []
        self.break_next: dict[str, Any] | None = None

    async def book(self, cdm: dict[str, Any]) -> dict[str, Any]:
        cid = f"SFP-C-{next(_n):08d}"
        self.booked.append(cdm)
        if self.break_next:
            self.breaks[cid], self.break_next = self.break_next, None
        return {"contractId": cid, "status": "BOOKED"}

    async def compare(self, cdm: dict[str, Any], contract_id: str | None = None) -> dict[str, Any]:
        lend = cdm["economicTerms"]["securityLending"]
        ours = {"feeBps": lend["feeRate"]["bps"], "quantity": lend["quantity"]}
        diffs = [{"field": f, "ours": ours[f], "processor": v} for f, v in self.breaks.get(contract_id, {}).items()]
        return {"contractId": contract_id, "status": "UNMATCHED" if diffs else "MATCHED", "diffs": diffs,
                **({"breakId": "SFP-B-000001"} if diffs else {})}

    async def amend(self, contract_id: str, fields: dict[str, Any], reason: str) -> dict[str, Any]:
        self.calls.append(("amend", contract_id, fields))
        self.breaks.pop(contract_id, None)
        return {"changes": fields}

    async def lifecycle(self, contract_id: str, event: str, quantity: int | None = None,
                        fee_bps: float | None = None, price: float | None = None) -> dict[str, Any]:
        self.calls.append((event, contract_id, quantity, fee_bps) + ((price,) if price else ()))
        return {"contract": {"contractId": contract_id}}

    async def run_marks(self) -> dict[str, Any]:
        return {"marked": 1, "marginCalls": 0, "calls": []}


class Custodian:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.status: dict[str, str] = {}

    async def instruct(self, body: dict[str, Any]) -> dict[str, Any]:
        iid = f"CUST-{len(self.sent) + 1:06d}"
        self.sent.append({**body, "instructionId": iid})
        self.status[iid] = "ACCEPTED"
        return {"instruction": {**body, "instructionId": iid, "status": "ACCEPTED"}}

    async def instructions(self, status: str | None = None) -> list[dict[str, Any]]:
        return [{"instructionId": k, "status": v} for k, v in self.status.items()]


class VenueB:
    """One dealer, HARBORAL's Venue B entity; the test drives what it does."""

    def __init__(self, entity: str) -> None:
        self.entity = entity
        self.by_id: dict[str, dict[str, Any]] = {}
        self.match_rows: list[dict[str, Any]] = []
        self.loan_rows: list[dict[str, Any]] = []
        self.calls: list[tuple[Any, ...]] = []

    async def upload_list(self, name: str, lines: list[dict[str, Any]], auto_accept: bool = False,
                          client_ref: str | None = None) -> dict[str, Any]:
        lid = f"WM-L-{next(_n):06d}"  # unique across tests: they share one desk database
        self.by_id[lid] = {"listId": lid, "status": "ACTIVE", "lines": lines, "autoAccept": auto_accept}
        return self.by_id[lid]

    async def run_optimiser(self, list_id: str) -> dict[str, Any]:
        return {"matchesProposed": 0}

    async def lists(self) -> list[dict[str, Any]]:
        return list(self.by_id.values())

    async def interest_list(self, list_id: str) -> dict[str, Any]:
        return self.by_id[list_id]

    async def matches(self, list_id: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        return [m for m in self.match_rows if not list_id or m["listId"] == list_id]

    def dealer_counters(self, list_id: str, fee: float, qty: int) -> None:
        self.match_rows.append({"matchId": f"WM-M-{next(_n):08d}", "listId": list_id,
                                "status": "PROPOSED", "ourAccept": False, "feeBps": fee, "quantity": qty,
                                "counterparty": {"entity": self.entity}})

    async def accept(self, match_id: str) -> dict[str, Any]:
        m = next(x for x in self.match_rows if x["matchId"] == match_id)
        m.update(status="AGREED", ourAccept=True)
        line = self.by_id[m["listId"]]["lines"][0]
        self.loan_rows.append({
            "loanId": f"WM-T-{next(_n):08d}", "matchId": match_id, "instrumentId": "I00001",
            "side": line["side"], "counterparty": {"entity": self.entity}, "quantityOriginal": m["quantity"],
            "quantityOpen": m["quantity"], "feeBps": m["feeBps"], "pendingRerateBps": None, "priceAtTrade": 180.0,
            "ccy": "USD", "term": "OPEN", "collateralType": "CASH", "collateralCcy": "USD",
            "tradeDate": "2026-10-27", "settlementDate": "2026-10-28",
            "history": [{"ts": "2026-10-27T14:00:00Z", "event": "BOOKED"}]})
        return m

    async def propose(self, match_id: str, fee_bps: float, quantity: int | None = None) -> dict[str, Any]:
        self.calls.append(("propose", match_id, fee_bps, quantity))
        return {}

    async def reject(self, match_id: str) -> dict[str, Any]:
        self.calls.append(("reject", match_id))
        next(x for x in self.match_rows if x["matchId"] == match_id)["status"] = "REJECTED"
        return {}

    async def withdraw(self, list_id: str) -> dict[str, Any]:
        self.by_id[list_id]["status"] = "WITHDRAWN"
        return self.by_id[list_id]

    async def loans(self) -> list[dict[str, Any]]:
        return self.loan_rows

    async def rerate(self, loan_id: str, fee_bps: float) -> dict[str, Any]:
        self.calls.append(("rerate", loan_id, fee_bps))
        next(x for x in self.loan_rows if x["loanId"] == loan_id)["pendingRerateBps"] = fee_bps
        return {}

    async def return_qty(self, loan_id: str, quantity: int | None = None) -> dict[str, Any]:
        self.calls.append(("return", loan_id, quantity))
        return {}


@pytest.fixture
def post_trade(monkeypatch: pytest.MonkeyPatch, setup: None) -> SimpleNamespace:
    p, c = Processor(), Custodian()
    sy = systems()
    for name in ("book", "compare", "amend", "lifecycle", "run_marks"):
        monkeypatch.setattr(sy.processor, name, getattr(p, name))
    for name in ("instruct", "instructions"):
        monkeypatch.setattr(sy.custodian, name, getattr(c, name))
    return SimpleNamespace(processor=p, custodian=c)


@pytest.fixture
async def venue_b(monkeypatch: pytest.MonkeyPatch, setup: None) -> VenueB:
    async with db.session() as s:
        harbor = await s.get(Counterparty, "HARBORAL")
    vb = VenueB(harbor.wematch_entity)
    for name in ("upload_list", "run_optimiser", "lists", "interest_list", "matches", "accept", "propose", "reject",
                 "withdraw", "loans", "rerate", "return_qty"):
        monkeypatch.setattr(systems().venue_b, name, getattr(vb, name))
    return vb


async def _venue_a_fill(side: str = "BORROW", qty: int = 50_000, fee: float = 25.0) -> Trade:
    async with db.session() as s:
        harbor = await s.get(Counterparty, "HARBORAL")
        ours, theirs = {"code": "0001"}, {"code": harbor.ngt_code}
        n = next(_n)
        t = await orders.add_trade(s, {
            "tradeId": f"NGT-T-{n:08d}", "uti": f"UTI{n:020d}", "instrumentId": "I00001", "quantity": qty,
            "feeBps": fee, "price": 180.0, "ccy": "USD", "settlementDate": "2026-10-28",
            "executedAt": "2026-10-27T14:00:00Z", "borrower": ours if side == "BORROW" else theirs,
            "lender": theirs if side == "BORROW" else ours, "term": "OPEN", "collateralType": "CASH",
            "collateralCcy": "USD", "marginPct": 102.0, "legalAgreement": "MSLA", "bookingEntity": "LOANDESK-US"})
        await s.commit()
    return t


# -- booking ------------------------------------------------------------------------------------------
async def test_a_fill_books_itself_captures_10c1a_and_settles(post_trade: SimpleNamespace) -> None:
    t = await _venue_a_fill()
    assert (t.compare_status, t.open_qty, t.loan_status) == ("MATCHED", 50_000, "OPEN")
    assert t.reg_10c1a["securityId"] == "US9930506731" and t.reg_10c1a["platform"] == "Venue A (NGT-style)"
    assert t.reg_10c1a["borrowerType"] == "BROKER_DEALER" and t.reg_10c1a["rateType"] == "REBATE"
    sent = post_trade.custodian.sent[-1]  # we borrowed: we receive the shares and pay cash collateral at 102%
    assert (sent["movement"], sent["txType"], sent["payment"]) == ("RECE", "SECB", "APMT")
    assert sent["amount"] == round(50_000 * 180.0 * 1.02, 2) and t.settle_status == "PENDING"
    post_trade.custodian.status[t.settle_ref] = "SETTLED"
    await sync.sync_settlement()
    async with db.session() as s:
        assert (await s.get(Trade, t.id)).settle_status == "SETTLED"


async def test_a_break_drafts_its_fix_for_the_gate(client: httpx.AsyncClient, post_trade: SimpleNamespace) -> None:
    post_trade.processor.break_next = {"feeBps": 30.0}
    t = await _venue_a_fill(fee=25.0)
    assert t.compare_status == "UNMATCHED" and t.settle_ref is None  # nothing settles on a break
    async with db.session() as s:
        a = (await s.execute(select(Action).where(Action.source_ref == t.id))).scalar_one()
    assert (a.kind, a.status, a.payload["fields"], a.original) == ("amend", "PENDING", {"feeBps": 25.0},
                                                                   {"feeBps": 30.0})
    r = (await client.post(f"/actions/{a.id}/approve", headers={"x-trader": "sam"})).json()
    assert r["status"] == "EXECUTED" and r["result"]["compareStatus"] == "MATCHED"
    assert post_trade.processor.calls[-1][0] == "amend"
    assert post_trade.custodian.sent[-1]["txId"].startswith(t.id)  # matched now, so it settles


# -- Venue B and failover -----------------------------------------------------------------------------
async def test_venue_a_down_fails_over_to_a_venue_b_list(client: httpx.AsyncClient, post_trade: SimpleNamespace,
                                                         venue_b: VenueB) -> None:
    o = (await client.post("/orders", json={"instrument": "NVDA US", "quantity": 40_000, "feeBps": 30,
                                            "override": True})).json()
    assert (o["venue"], o["type"], o["status"]) == ("VENUE_B", "INTEREST_LIST", "PENDING")
    assert o["routeReason"].startswith("failover: Venue A down")
    assert venue_b.by_id[o["venueOrderId"]]["autoAccept"] is True
    venue_b.dealer_counters(o["venueOrderId"], 34.0, 40_000)  # outside our 30 bps limit: back to the trader
    await sync.sync_venue_b()
    got = {x["id"]: x for x in (await client.get("/orders")).json()}[o["id"]]
    assert (got["status"], got["counterFeeBps"], got["counterparty"]) == ("COUNTERED", 34.0, "HARBORAL")
    assert (await client.post(f"/orders/{o['id']}/accept")).json()["status"] == "PENDING"  # agreed; the loan is next
    await sync.sync_venue_b()
    trades = [x for x in (await client.get("/trades")).json() if x["orderId"] == o["id"]]
    assert len(trades) == 1 and trades[0]["venue"] == "VENUE_B" and trades[0]["compareStatus"] == "MATCHED"
    assert trades[0]["uti"].endswith(trades[0]["venueTradeId"].replace("-", ""))
    got = {x["id"]: x for x in (await client.get("/orders")).json()}[o["id"]]
    assert (got["status"], got["filledQty"]) == ("EXECUTED", 40_000)


async def test_a_bid_the_venue_rejects_on_placing_keeps_its_reason(client: httpx.AsyncClient,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejected inside the place call, the order is never open, so never polled: the reason is read there."""
    async def place(body: dict) -> dict:
        return {"orderId": "NGT-O-REJ1", "status": "REJECTED", "rejectReason": "BELOW_FLOOR: firm bid below GC "
                                                                              "floor 38.9bps"}

    monkeypatch.setattr(systems().venue_a, "place", place)
    o = (await client.post("/orders", json={"instrument": "NVDA US", "quantity": 1000, "feeBps": 30,
                                            "venue": "VENUE_A", "override": True})).json()
    assert (o["status"], o["rejectReason"]) == ("REJECTED", "BELOW_FLOOR: firm bid below GC floor 38.9bps")


async def test_a_named_counterparty_never_fails_over(client: httpx.AsyncClient) -> None:
    o = (await client.post("/orders", json={"instrument": "NVDA US", "quantity": 1000, "feeBps": 30,
                                            "counterparty": "HARBORAL", "override": True})).json()
    assert (o["venue"], o["status"]) == ("VENUE_A", "FAILED")
    p = (await client.post("/orders/preview", json={"instrument": "NVDA US", "quantity": 1000, "feeBps": 30,
                                                    "counterparty": "HARBORAL", "venue": "VENUE_B"})).json()
    assert any(c["name"] == "venue B is anonymous" and not c["ok"] for c in p["checks"])


async def test_counter_and_cancel_on_venue_b(client: httpx.AsyncClient, venue_b: VenueB) -> None:
    o = (await client.post("/orders", json={"instrument": "NVDA US", "quantity": 20_000, "feeBps": 30,
                                            "venue": "VENUE_B", "override": True})).json()
    assert o["routeReason"].startswith("trader chose Venue B")
    venue_b.dealer_counters(o["venueOrderId"], 36.0, 20_000)
    await sync.sync_venue_b()
    r = (await client.post(f"/orders/{o['id']}/counter", json={"feeBps": 32})).json()
    assert venue_b.calls[-1][0] == "propose" and venue_b.calls[-1][2] == 32 and r["feeBps"] == 32
    r = (await client.post(f"/orders/{o['id']}/cancel")).json()
    assert r["status"] == "CANCELLED" and venue_b.calls[-1][0] == "reject"


async def test_a_dealer_counter_inside_the_limit_is_accepted(client: httpx.AsyncClient, post_trade: SimpleNamespace,
                                                             venue_b: VenueB) -> None:
    o = (await client.post("/orders", json={"instrument": "NVDA US", "quantity": 15_000, "feeBps": 30,
                                            "venue": "VENUE_B", "override": True})).json()
    venue_b.dealer_counters(o["venueOrderId"], 19.2, 15_000)  # cheaper than our 30 bps limit to borrow
    await sync.sync_venue_b()  # accepts it; the loan lands on the next pass
    await sync.sync_venue_b()
    got = {x["id"]: x for x in (await client.get("/orders")).json()}[o["id"]]
    assert (got["status"], got["filledQty"]) == ("EXECUTED", 15_000)


# -- lifecycle ------------------------------------------------------------------------------------------
async def test_return_recall_and_rerate_a_venue_a_loan(client: httpx.AsyncClient,
                                                       post_trade: SimpleNamespace) -> None:
    t = await _venue_a_fill(qty=60_000)
    assert (await client.post(f"/trades/{t.id}/recall")).status_code == 409  # we borrowed it: return, not recall
    r = (await client.post(f"/trades/{t.id}/return", json={"quantity": 20_000})).json()
    assert (r["openQty"], r["loanStatus"]) == (40_000, "OPEN")
    assert post_trade.processor.calls[-1] == ("RETURN", t.sfp_contract_id, 20_000, None)
    leg = post_trade.custodian.sent[-1]  # return leg: we deliver the shares back
    assert (leg["movement"], leg["txType"], leg["quantity"]) == ("DELI", "SBRE", 20_000)
    assert (await client.post(f"/trades/{t.id}/return", json={"quantity": 50_000})).status_code == 409
    r = (await client.post(f"/trades/{t.id}/rerate", json={"feeBps": 40})).json()
    assert r["feeBps"] == 40 and post_trade.processor.calls[-1] == ("RERATE", t.sfp_contract_id, None, 40)
    r = (await client.post(f"/trades/{t.id}/return")).json()  # the rest
    assert (r["openQty"], r["loanStatus"]) == (0, "CLOSED")
    assert [e["event"] for e in r["lifecycle"]] == ["RETURN", "RERATE", "RETURN"]


async def test_a_venue_b_rerate_waits_for_the_counterparty(client: httpx.AsyncClient, post_trade: SimpleNamespace,
                                                          venue_b: VenueB) -> None:
    o = (await client.post("/orders", json={"instrument": "NVDA US", "quantity": 10_000, "feeBps": 30,
                                            "venue": "VENUE_B", "override": True})).json()
    venue_b.dealer_counters(o["venueOrderId"], 30.0, 10_000)
    await client.post(f"/orders/{o['id']}/accept")
    await sync.sync_venue_b()
    t = next(x for x in (await client.get("/trades")).json() if x["orderId"] == o["id"])
    r = (await client.post(f"/trades/{t['id']}/rerate", json={"feeBps": 26})).json()
    assert (r["feeBps"], r["pendingRerateBps"]) == (30.0, 26)
    loan = next(x for x in venue_b.loan_rows if x["loanId"] == t["venueTradeId"])
    loan.update(feeBps=26.0, pendingRerateBps=None)  # the dealer agreed
    await sync.sync_venue_b()
    r = next(x for x in (await client.get("/trades")).json() if x["id"] == t["id"])
    assert (r["feeBps"], r["pendingRerateBps"]) == (26.0, None)
    assert post_trade.processor.calls[-1] == ("RERATE", t["contractId"], None, 26.0)


async def test_a_venue_b_buy_in_closes_the_loan_and_is_booked_at_the_processor(
        client: httpx.AsyncClient, post_trade: SimpleNamespace, venue_b: VenueB) -> None:
    """The lender recalls on the venue and, not returned in time, buys in there: the loan closes for those shares
    at the venue, so we book it at the processor, and the return card for the recall goes."""
    o = (await client.post("/orders", json={"instrument": "NVDA US", "quantity": 30_000, "feeBps": 30,
                                            "venue": "VENUE_B", "override": True})).json()
    venue_b.dealer_counters(o["venueOrderId"], 30.0, 30_000)
    await client.post(f"/orders/{o['id']}/accept")
    await sync.sync_venue_b()
    t = next(x for x in (await client.get("/trades")).json() if x["orderId"] == o["id"])
    loan = next(x for x in venue_b.loan_rows if x["loanId"] == t["venueTradeId"])
    loan["recall"] = {"quantity": 12_000, "dueDate": "2026-10-28", "overdue": False}
    await sync.sync_venue_b()
    async with db.session() as s:
        ref = f"recall:{loan['loanId']}"
        [card] = (await s.execute(select(Action).where(Action.source_ref == ref))).scalars().all()
    loan.update(recall=None, quantityOpen=18_000, buyIns=[{
        "buyInId": f"{loan['loanId']}-B1", "quantity": 12_000, "price": 183.5, "ccy": "USD", "cost": 2_202_000.0,
        "collateralKept": 2_203_200.0, "amountDue": -1_200.0, "remaining": 18_000, "ts": "2026-10-29T14:05:00Z"}])
    await sync.sync_venue_b()
    await sync.sync_venue_b()  # once
    r = next(x for x in (await client.get("/trades")).json() if x["id"] == t["id"])
    assert (r["openQty"], r["recalledQty"], r["recallDue"], r["boughtIn"]) == (18_000, 0, None, 12_000)
    assert [e["event"] for e in r["lifecycle"]].count("BOUGHT_IN") == 1
    assert ("BUY_IN", t["contractId"], 12_000, None, 183.5) in post_trade.processor.calls
    async with db.session() as s:
        assert (await s.get(Action, card.id)).status == "SUPERSEDED"
    [b] = [x for x in (await client.get("/buy-ins")).json() if x["tradeId"] == t["id"]]
    assert (b["kind"], b["origin"], b["counterparty"], b["amountDue"]) == ("RECALL", "venue_b", "HARBORAL", -1_200.0)
    async with db.session() as s:  # the collateral kept was more than the cost: the lender owes us the rest
        [claim] = (await s.execute(select(Action).where(Action.source_ref == f"buyin:{b['id']}"))).scalars().all()
    assert (claim.payload["direction"], claim.payload["amount"]) == ("RECEIVE", 1_200.0)
    assert claim.title.startswith("claim $1,200 from HARBORAL") and b["cashStatus"] == "TO_RECEIVE"


# -- chat -------------------------------------------------------------------------------------------------
async def test_a_trade_agreed_in_chat_becomes_an_order_card(client: httpx.AsyncClient,
                                                            post_trade: SimpleNamespace) -> None:
    m = SimpleNamespace(id="chat-agreed-1", our_side="BORROW", counterparty="HARBORAL", parser="model:muse-spark-1.3",
                        text="Sure, send 500. ref HAR-55501", lines=[{"identifier": "NVDA US", "instrumentId": "I00001",
                                                                      "quantity": 500, "rate": "GC"}])
    async with db.session() as s:
        notes = await orders.propose_from_chat(s, m)
        await s.commit()
        a = (await s.execute(select(Action).where(Action.source_ref == m.id))).scalar_one()
    assert notes == [f"NVDA US: order card {a.id}"]
    assert (a.kind, a.proposed_by) == ("order", "chat:model:muse-spark-1.3")
    assert (a.payload["side"], a.payload["counterparty"], a.payload["quantity"]) == ("BORROW", "HARBORAL", 500)
    assert a.payload["feeBps"] > 0 and "market fee" in a.rationale
    assert a.payload["venue"] == "BILATERAL" and a.payload["externalRef"] == "HAR-55501"
    r = (await client.post(f"/actions/{a.id}/approve", headers={"x-trader": "sam"})).json()
    # Agreed already, with Harbor: approving books it directly (no venue), and it settles like any fill.
    assert r["status"] == "EXECUTED" and r["result"]["status"] == "EXECUTED"
    [t] = [x for x in (await client.get("/trades")).json() if x["orderId"] == r["result"]["orderId"]]
    assert (t["venue"], t["counterparty"], t["quantity"], t["compareStatus"]) == ("BILATERAL", "HARBORAL", 500,
                                                                                  "MATCHED")
    assert t["uti"].startswith(OUR_FIRM["lei"]) and t["reg10c1a"]["platform"] == "Bilateral (agreed in chat)"
    assert post_trade.custodian.sent[-1]["txId"].startswith(t["id"])


async def test_chat_agreement_without_sides_is_left_to_the_trader(setup: None) -> None:
    m = SimpleNamespace(id="chat-agreed-2", our_side=None, counterparty="HARBORAL", parser="rules", text="Send",
                        lines=[])
    async with db.session() as s:
        assert "trader books it" in (await orders.propose_from_chat(s, m))[0]


async def test_lifecycle_needs_a_booked_open_loan(setup: None) -> None:
    async with db.session() as s:
        harbor = await s.get(Counterparty, "HARBORAL")
        t = await orders.add_trade(s, {
            "tradeId": "NGT-T-UNBOOKED", "instrumentId": "I00001", "quantity": 100, "feeBps": 20.0, "price": 1.0,
            "ccy": "USD", "settlementDate": "2026-10-28", "executedAt": "2026-10-27T14:00:00Z",
            "borrower": {"code": "0001"}, "lender": {"code": harbor.ngt_code}})
        assert t.compare_status == "FAILED"  # the processor is down in tests
        with pytest.raises(lifecycle.LifecycleError, match="isn't booked"):
            await lifecycle.close(s, t, None, by="trader:sam")
        await s.rollback()
    assert booking.open_qty(t) == 100
