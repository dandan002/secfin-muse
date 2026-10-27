"""Smoke test: play the LoanDesk platform against the mocks through the five-minute demo.

    uv run python scripts/smoke.py                      # against a running stack on 8000-8060
    uv run python scripts/smoke.py --base-port 18000 --fix-port 19878

Stands in for the (not yet built) platform with the simplest possible logic, so it doubles as
a reference for every adapter: bot API, gateway inbox/decisions, FIX, CDM booking, compare/amend,
sese.023, interest lists. Exits non-zero if any step fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from typing import Any

import httpx

from mock_ngt.fixclient import FixClient
from refdata.counterparties import OUR_FIRM

TERMS = {"collateralType": "CASH", "collateralCcy": "USD", "marginPct": 102, "legalAgreement": "MSLA",
         "bookingEntity": "LOANDESK-US"}


class Smoke:
    def __init__(self, urls: dict[str, str], fix_port: int, fix_host: str = "127.0.0.1") -> None:
        self.u = urls
        self.fix_port, self.fix_host = fix_port, fix_host
        token = os.environ.get("MOCK_ADMIN_TOKEN")
        self.h = httpx.Client(timeout=120, headers={"x-admin-token": token} if token else None)
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}")

    def get(self, svc: str, path: str, **kw: Any) -> Any:
        r = self.h.get(self.u[svc] + path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, svc: str, path: str, **kw: Any) -> Any:
        r = self.h.post(self.u[svc] + path, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def advance(self, **kw: Any) -> None:
        self.post("control", "/clock/advance", json=kw)

    def run(self) -> bool:
        run = self.post("control", "/scenarios/demo/run?wait=true", json={"mode": "stepped"})
        self.check("demo scenario runs (stepped)", run["status"] == "completed", run["id"])

        # 1. Chat: PB-ALPHA's 40 locates arrive in Desk Chat; the bot API sees them.
        ib_h = {"authorization": f"Bearer {self.get('ib', '/_admin/credentials')['botToken']}"}
        streams = self.get("ib", "/streams", headers=ib_h)
        msgs = self.get("ib", "/streams/pbalpha-locates/messages?after=0&limit=1000", headers=ib_h)["messages"]
        labels = [x for x in self.get("ib", "/_admin/labels?limit=10000") if x["ts"] >= "2026-10-27T12:55"]
        pba = sum(len(x["label"]["lines"]) for x in labels  # the 09:00 blast (ambient chat starts 09:10)
                  if x["label"]["persona"] == "pb-alpha" and x["ts"] < "2026-10-27T13:10")
        self.check("chat: 40 locate lines from PB-ALPHA", pba == 40,
                   f"{len(msgs)} msgs in room, {len(streams)} streams")
        inj = [x for x in labels if x["label"]["intent"] == "INJECTION"]
        self.check("chat: prompt-injection message present", len(inj) == 1, inj[0]["text"][:60] if inj else "")

        # 2. Gateway: the hedge fund's 25-line batch; approve GC, reject the rest; webhook lands signed.
        gw = self.get("gateway", "/_admin/credentials")
        ph = {"authorization": f"Bearer {gw['platformToken']}"}
        inbox = self.get("gateway", "/v1/inbox?limit=5000", headers=ph)
        batch = next(x for x in inbox if x["requestId"] == "KESTRELHF-DEMO-0001")
        decisions = [{"lineNo": ln["lineNo"], "decision": "APPROVED" if self._tier(ln["isin"]) == "GC" else "REJECTED",
                      "approvedQuantity": ln["quantity"], "regShoBasis": "ETB_LIST@09:00"} for ln in batch["lines"]]
        self.post("gateway", "/v1/decisions", headers=ph, json={"locateId": batch["locateId"], "lines": decisions})
        self.advance(seconds=5)
        got = self.get("gateway", "/cp/KESTRELHF/received")
        self.check("gateway: 25-line batch decided, signed webhook received",
                   len(batch["lines"]) == 25 and bool(got) and got[-1]["signatureOk"],
                   f"{len(inbox)} requests in inbox incl. the 09:00 burst")

        # 3. RFQ: quote 700 on the hot name in chat; the client accepts.
        rfq = next(x for x in labels if x["label"]["intent"] == "RFQ" and x["label"]["persona"] == "kestrel-hf")
        line = rfq["label"]["lines"][0]
        self.post("ib", "/chats/kestrel-desk/messages", headers=ib_h,
                  json={"text": f"can show 700bps open on {line['identifier']}", "clientMsgId": "smoke-quote"})
        self.advance(seconds=60)
        last = self.get("ib", "/streams/kestrel-desk/messages?after=0&limit=1000", headers=ib_h)["messages"][-1]
        self.check("rfq: client accepts 700bps", last["sender"]["handle"] == "rpatel.kestrel" and "700" in last["text"],
                   last["text"])

        # 4. Borrow it on Venue A over FIX.
        avail = self.get("ngt", f"/v1/availability?instrument={line['identifier']}")["instruments"][0]
        qty = min(line["quantity"], avail["quantity"] // 1000 * 1000)
        fill = asyncio.run(self._fix_borrow(avail["isin"], qty, round(avail["bestFeeBps"] * 1.2)))
        self.check("venue A (FIX): borrow filled with UTI", bool(fill.get("uti")),
                   f"{fill.get('qty')} @ {fill.get('fee')}bps (asked {qty})")

        # 5. Book to the processor (CDM); the first booking comes back broken; compare + amend fixes it.
        sfp_h = {"x-api-key": self.get("sfp", "/_admin/credentials")["apiKey"]}
        trade = self.get("ngt", "/v1/trades", headers={"x-participant-key": self._ngt_key()})[-1]
        cdm = {"clientRef": f"BOOK-{trade['tradeId']}", "venue": "VENUE_A", "tradeDate": "2026-10-27",
               "tradeIdentifier": [{"issuer": "VENUE_A", "assignedIdentifier": trade["tradeId"]},
                                   {"identifierType": "UTI", "assignedIdentifier": trade["uti"]}],
               "legalAgreement": trade["legalAgreement"], "bookingEntity": trade["bookingEntity"],
               "party": [{"partyId": OUR_FIRM["lei"], "role": "Borrower"},
                         {"partyId": trade["lender"]["lei"], "role": "Lender"}],
               "economicTerms": {"securityLending": {
                   "security": {"isin": trade["isin"]}, "quantity": trade["quantity"],
                   "feeRate": {"bps": trade["feeBps"]}, "settlementDate": trade["settlementDate"],
                   "collateral": {"type": trade["collateralType"], "currency": trade["collateralCcy"],
                                  "marginPercentage": trade["marginPct"]}}}}
        booked = self.post("sfp", "/trades", headers=sfp_h, json=cdm)
        cmp = self.post("sfp", "/compare", headers=sfp_h, json={"trade": cdm})
        self.check("processor: booking break detected by compare", cmp["status"] == "UNMATCHED",
                   ", ".join(f"{d['field']} {d['ours']}≠{d['processor']}" for d in cmp["diffs"]))
        fix = {d["field"]: d["ours"] for d in cmp["diffs"]}
        am = self.post("sfp", f"/contracts/{booked['contractId']}/amend", headers=sfp_h,
                       json={"fields": fix, "reason": "align to venue fill"})
        self.check("processor: amendment resolves the break", bool(am["resolvedBreaks"]), str(am["resolvedBreaks"]))

        # 6. Settle the borrow at the custodian (sese.023 -> sese.025).
        cus = self.get("custodian", "/_admin/credentials")
        cus_h = {"x-api-key": cus["apiKey"]}
        self.post("custodian", "/instructions", headers=cus_h, json={
            "txId": f"SET-{trade['tradeId']}", "movement": "RECE", "txType": "SECB", "instrument": trade["isin"],
            "quantity": trade["quantity"], "settlementDate": trade["settlementDate"], "commonId": trade["uti"][:35],
            "counterpartyBic": "MKHAUS33XXX"})
        settle = date.fromisoformat(trade["settlementDate"])
        for day_offset in range(0, 4):  # a seeded fail is retried on the next business day
            self.advance(to=f"{date.fromordinal(settle.toordinal() + day_offset).isoformat()}T10:05:00-04:00")
            st = self.get("custodian", f"/instructions/SET-{trade['tradeId']}", headers=cus_h)["status"]
            if st == "SETTLED":
                break
        out = self.get("custodian", "/iso/outbox", headers=cus_h)
        self.check("custodian: sese.025 settlement confirmation", st == "SETTLED",
                   " → ".join(m["status"] for m in out if m["instructionId"]))

        # 7. Paste-a-sheet moment: 15 names as a Venue B interest list.
        wm_h = {"x-api-key": self.get("wematch", "/_admin/credentials")["apiKey"]}
        dealer = self.get("wematch", "/_admin/dealer-lines?side=LEND&limit=15")
        lines = [{"instrument": self._isin(d["instrumentId"]), "side": "BORROW", "quantity": d["quantity"],
                  "feeBps": d["feeMax"]} for d in dealer]
        lst = self.post("wematch", "/v1/interest-lists", headers=wm_h,
                        json={"name": "pasted Bloomberg sheet", "autoAccept": True, "lines": lines})
        opt = self.post("wematch", "/v1/optimiser/run", headers=wm_h, json={"listId": lst["listId"]})
        self.advance(seconds=60)
        loans = self.get("wematch", "/v1/loans", headers=wm_h)
        self.check("venue B: 15-line list matched into loans", opt["matchesProposed"] >= 15 and len(loans) > 0,
                   f"{opt['matchesProposed']} proposals, {len(loans)} agreed loans")

        ok = all(r[1] for r in self.results)
        print(f"\n{sum(r[1] for r in self.results)}/{len(self.results)} checks passed")
        return ok

    # -- helpers ---------------------------------------------------------------------------
    def _ngt_key(self) -> str:
        return self.get("ngt", "/_admin/credentials")["participantKey"]

    def _tier(self, isin: str) -> str:
        return self.get("ib", f"/api/instruments/resolve?q={isin}")["tier"]

    def _isin(self, instrument_id: str) -> str:
        from mockcore import rng
        from refdata.universe import instrument_by_id

        rng.set_seed(self.get("control", "/clock")["seed"])
        return instrument_by_id(instrument_id).isin

    async def _fix_borrow(self, isin: str, qty: int, fee: float) -> dict[str, Any]:
        cred = self.get("ngt", "/_admin/credentials")["fix"]
        c = FixClient(self.fix_host, self.fix_port, password=cred["password"])
        await c.connect()
        await c.send("D", [(11, "SMOKE-1"), (48, isin), (22, 4), (54, 1), (38, qty), (20008, "BID"), (20001, fee),
                           (20003, "CASH"), (15, "USD"), (20004, 102), (64, "20261028"), (20006, "MSLA"),
                           (20007, "LOANDESK-US")])
        await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) in ("0", "8"))
        await asyncio.to_thread(self.advance, seconds=30)
        m = await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) in ("F", "8", "D"))
        if m.get(150) == "D":  # lender countered (e.g. partial size): take it
            await c.send("U1", [(41, "SMOKE-1"), (11, "SMOKE-1-ACC")])
            m = await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) in ("F", "8"))
        out: dict[str, Any] = {"fee": m.get(31), "qty": m.get(32), "status": m.get(150), "text": m.get(58)}
        if m.get(150) == "F":
            out["uti"] = (await c.wait_for(lambda m: m.get(35) == "8" and m.get(150) == "I"))[20013]
        await c.close()
        return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-port", type=int, default=8000)
    ap.add_argument("--fix-port", type=int, default=9878)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--url-template", help="e.g. https://{name}.mocks.example.com (hosted stack; overrides ports)")
    a = ap.parse_args(argv)
    names = ("control", "ib", "gateway", "ngt", "wematch", "sfp", "custodian")
    if a.url_template:
        urls = {n: a.url_template.format(name=n) for n in names}
    else:
        urls = {n: f"http://{a.host}:{a.base_port + 10 * i}" for i, n in enumerate(names)}
    return 0 if Smoke(urls, a.fix_port, a.host).run() else 1


if __name__ == "__main__":
    sys.exit(main())
