"""The demo scenario runs end to end, and a stepped run is reproducible event for event."""

from __future__ import annotations

SERVICES = ("ib", "gateway", "ngt", "wematch", "sfp", "custodian", "analytics")


def _run(mocks) -> dict:
    r = mocks.post("control", "/scenarios/demo/run?wait=true", json={"mode": "stepped"}, timeout=600)
    r.raise_for_status()
    run = mocks.get("control", f"/runs/{r.json()['id']}").json()
    assert run["status"] == "completed", run
    failed = [x for x in run["log"] if not x["ok"]]
    assert not failed, failed
    return run


def _events(mocks) -> dict[str, list]:
    out = {}
    for svc in SERVICES:
        evs, after = [], 0
        while True:
            page = mocks.get(svc, f"/_admin/events?after={after}&limit=5000").json()
            if not page:
                break
            evs += [(e["type"], e["entityId"], e["simTs"], e["payload"]) for e in page]
            after = page[-1]["eventId"]
        out[svc] = evs
    return out


def test_demo_scenario_inputs(mocks):
    _run(mocks)
    labels = mocks.get("ib", "/_admin/labels?limit=10000").json()
    today = [x for x in labels if x["ts"] >= "2026-10-27T12:55"]
    # Chat carries borrows and loans; locates come through the API.
    intents = {x["label"]["intent"] for x in today}
    assert {"IOI", "RFQ", "RECALL", "INTERNAL_BORROW", "INJECTION"} <= intents
    assert "LOCATE" not in {x["label"]["intent"] for x in today if x["ts"] < "2026-10-27T13:10"}
    ph = {"authorization": f"Bearer {mocks.creds('gateway')['platformToken']}"}
    inbox = mocks.get("gateway", "/v1/inbox?limit=5000", headers=ph).json()
    pba = [x for x in inbox if x["requestId"] == "PBALPHA-DEMO-0001"]
    assert len(pba) == 1 and len(pba[0]["lines"]) == 40
    demo_batch = [x for x in inbox if x["requestId"] == "KESTRELHF-DEMO-0001"]
    assert len(demo_batch) == 1 and len(demo_batch[0]["lines"]) == 25
    assert len(inbox) > 100  # the 09:00-09:45 burst
    assert mocks.get("sfp", "/_admin/flags").json()["force_break_next"] is True


def test_stepped_runs_are_deterministic(mocks):
    _run(mocks)
    first = _events(mocks)
    _run(mocks)
    second = _events(mocks)
    for svc in SERVICES:
        assert len(first[svc]) == len(second[svc]), svc
        assert first[svc] == second[svc], svc
    assert sum(len(v) for v in first.values()) > 200
