import asyncio
from datetime import UTC, datetime, timedelta

from mockcore import auth, clock, rng
from mockcore.clock import ClockState, SimClock
from mockcore.faults import FaultConfig, Faults
from mockcore.sched import Scheduler


def test_rng_streams_are_independent_and_reproducible():
    rng.set_seed(5)
    a1 = [rng.stream("a").random() for _ in range(3)]
    rng.set_seed(5)
    rng.stream("b").random()  # a new consumer must not shift "a"
    a2 = [rng.stream("a").random() for _ in range(3)]
    assert a1 == a2
    rng.set_seed(6)
    assert [rng.stream("a").random() for _ in range(3)] != a1


def test_clock_live_speed_and_pause():
    epoch = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    s = ClockState(sim_epoch=epoch, wall_anchor=1000.0, speed=10.0, paused=False)
    assert s.now(1001.0) == epoch + timedelta(seconds=10)
    assert ClockState(sim_epoch=epoch, paused=True).now(5000.0) == epoch
    assert ClockState(sim_epoch=epoch, paused=False, mode="stepped").now(5000.0) == epoch


def test_clock_pinning():
    c = SimClock(ClockState())
    old = clock.clock
    clock.clock = c
    try:
        t = datetime(2030, 1, 1, tzinfo=UTC)
        with clock.at(t):
            assert clock.now() == t
        assert clock.now() == c.state.sim_epoch
    finally:
        clock.clock = old


def test_scheduler_runs_in_due_order_with_pinned_time():
    old = clock.clock
    clock.clock = SimClock(ClockState(mode="stepped"))
    try:
        s = Scheduler()
        seen: list[tuple[str, datetime]] = []

        async def job(tag: str) -> None:
            seen.append((tag, clock.now()))

        t0 = clock.now()
        s.at(t0 + timedelta(seconds=5), "b", job, "b")
        s.at(t0 + timedelta(seconds=1), "a", job, "a")
        s.at(t0 + timedelta(seconds=5), "c", job, "c")
        s.every(2, "tick", lambda: job("tick"))
        s.arm_periodic()
        asyncio.run(s.run_due(t0 + timedelta(seconds=5)))
        tags = [t for t, _ in seen]
        assert tags[0] == "a" and tags.count("tick") >= 2
        assert tags.index("b") < tags.index("c")
        assert seen[0][1] == t0 + timedelta(seconds=1)
    finally:
        clock.clock = old


def test_faults_drop_dup_reorder_are_seeded():
    rng.set_seed(1)
    f = Faults("x", FaultConfig(drop_pct=30, dup_pct=30, reorder_pct=20))
    out1 = [m for i in range(50) for m in f.outbound(i)]
    rng.set_seed(1)
    f2 = Faults("x", FaultConfig(drop_pct=30, dup_pct=30, reorder_pct=20))
    out2 = [m for i in range(50) for m in f2.outbound(i)]
    assert out1 == out2
    assert len(set(out1)) < 50  # some dropped
    assert len(out1) != len(set(out1))  # some duplicated


def test_webhook_signature():
    sig = auth.sign("s3cret", b'{"a":1}')
    assert auth.verify("s3cret", b'{"a":1}', sig)
    assert not auth.verify("s3cret", b'{"a":2}', sig)
    assert not auth.verify("other", b'{"a":1}', sig)


def test_admin_token_guards_every_admin_route():
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from mockcore.app import _SimTimeAndFaults

    app = FastAPI()

    @app.get("/_admin/credentials")  # registered on the app, not the shared admin router
    async def creds() -> dict:
        return {"apiKey": "k"}

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    ctx = SimpleNamespace(cfg=SimpleNamespace(admin_token="s3cret"), faults=Faults("t"))
    guarded = _SimTimeAndFaults(app, ctx)  # type: ignore[arg-type]

    async def go() -> list[int]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://t") as c:
            return [
                (await c.get("/_admin/credentials")).status_code,
                (await c.get("/_admin/credentials", headers={"X-Admin-Token": "wrong"})).status_code,
                (await c.get("/_admin/credentials", headers={"X-Admin-Token": "s3cret"})).status_code,
                (await c.get("/health")).status_code,
            ]

    assert asyncio.run(go()) == [401, 401, 200, 200]
    ctx.cfg.admin_token = ""  # empty token = local dev, no check
    assert asyncio.run(go())[0] == 200
