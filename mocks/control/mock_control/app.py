"""mock-control: the simulated clock, resets, fault fan-out and the scenario runner.

Its own API (clock, reset, faults, scenarios) is gated like every mock's business API: an
operator key (``sim:operate``) changes things, an observer key (``sim:read``) only looks.

It owns the ``control.clock`` row in its own database and pushes the clock to every mock
(``PUT /_admin/clock``) on each change and on a heartbeat, so the mocks share nothing but
HTTP. In ``stepped`` mode it advances the clock and tells each service to run its due jobs
before every scenario step, which makes a run reproducible event-for-event for a given seed.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from mockcore import clock
from mockcore.app import ServiceContext, ServiceSpec, create_app
from mockcore.db import make_base
from mockcore.faults import FaultConfig
from mockcore.keys import AuthSpec, Grant

from . import scenarios as sc

Base = make_base("control")


class ScenarioRun(Base):
    __tablename__ = "scenario_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    log: Mapped[list[Any]] = mapped_column(default=list)


SCOPES = {
    "sim:read": "Clock, services, faults, scenarios and runs",
    "sim:operate": "Move the clock, reset every mock, set faults, run and stop scenarios",
}
spec = ServiceSpec(
    name="mock-control",
    label="Simulation Control",
    schema="control",
    base=Base,
    description="Sim clock, seeded resets, fault toggles and scenario runner for every mock.",
    owns_clock=True,
    on_startup=lambda c: _start_heartbeat(c),
    auth=AuthSpec(SCOPES, lambda: [
        Grant("OPERATOR", tuple(SCOPES), "partner", "Simulation operator"),
        Grant("OBSERVER", ("sim:read",), "standard", "Observer (read-only)"),
    ]),
)
ctx = ServiceContext(spec)
app = create_app(ctx)
READ = [Depends(ctx.require("sim:read"))]
OPERATE = [Depends(ctx.require("sim:operate"))]

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "scenarios"  # mocks/control/scenarios
# Order matters for stepped ticks and resets; keep it fixed for determinism.
SERVICE_ORDER = ("ib", "gateway", "ngt", "wematch", "sfp", "custodian", "analytics")


def service_urls() -> dict[str, str]:
    c = ctx.cfg
    return {
        "ib": c.ib_url,
        "gateway": c.gateway_url,
        "ngt": c.ngt_url,
        "wematch": c.wematch_url,
        "sfp": c.sfp_url,
        "custodian": c.custodian_url,
        "analytics": c.analytics_url,
    }


def _admin_headers(sim_time: datetime | None = None) -> dict[str, str]:
    h = {}
    if ctx.cfg.admin_token:
        h["x-admin-token"] = ctx.cfg.admin_token
    if sim_time is not None:
        h["x-sim-time"] = clock.iso(sim_time)
    return h


_push_lock = asyncio.Lock()


async def _push_clock() -> dict[str, Any]:
    """Send the current clock to every mock. Serialised so an older push never lands last."""
    async with _push_lock:
        body = {"state": clock.clock.state.to_json(), "rev": time.time_ns()}

        async def one(name: str) -> Any:
            try:
                r = await ctx.http.put(service_urls()[name] + "/_admin/clock", json=body,
                                       headers=_admin_headers(), timeout=10)
                return r.json() if r.status_code < 400 else {"error": r.status_code, "body": r.text[:300]}
            except Exception as e:  # a mock that's down catches up on the next heartbeat
                return {"error": type(e).__name__, "detail": str(e)}

        results = await asyncio.gather(*(one(n) for n in SERVICE_ORDER))
    return dict(zip(SERVICE_ORDER, results, strict=True))


async def _heartbeat() -> None:
    while True:
        await _push_clock()
        await asyncio.sleep(ctx.cfg.clock_push_s)


async def _start_heartbeat(c: ServiceContext) -> None:
    c._tasks.append(asyncio.create_task(_heartbeat(), name="mock-control-clock-push"))


async def _save_clock(state: clock.ClockState) -> clock.ClockState:
    clock.clock.state = state
    await clock.save_clock(ctx.db.engine, state)
    await _push_clock()
    return state


async def _fan_out(method: str, path: str, json: Any = None, sim_time: datetime | None = None) -> dict:
    out: dict[str, Any] = {}
    for name in SERVICE_ORDER:
        url = service_urls()[name]
        try:
            r = await ctx.http.request(method, url + path, json=json, headers=_admin_headers(sim_time), timeout=60)
            out[name] = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            if r.status_code >= 400:
                out[name] = {"error": r.status_code, "body": out[name]}
        except Exception as e:  # report unreachable services instead of failing the whole call
            out[name] = {"error": type(e).__name__, "detail": str(e)}
    return out


# -- clock ----------------------------------------------------------------------------------
class ClockUpdate(BaseModel):
    speed: float | None = Field(None, gt=0, le=10_000)
    paused: bool | None = None
    mode: Literal["live", "stepped"] | None = None


class JumpRequest(BaseModel):
    to: str = Field(..., description="ISO 8601 timestamp, e.g. 2026-10-27T09:00:00-04:00")


class AdvanceRequest(BaseModel):
    to: str | None = None
    seconds: float | None = None


@app.get("/clock", tags=["clock"], dependencies=READ)
async def get_clock() -> dict:
    clock.clock.state = await clock.load_clock(ctx.db.engine)
    return clock.clock.state.to_json()


@app.put("/clock", tags=["clock"], dependencies=OPERATE)
async def update_clock(req: ClockUpdate) -> dict:
    clock.clock.state = await clock.load_clock(ctx.db.engine)
    changes = {k: v for k, v in req.model_dump().items() if v is not None}
    state = clock.clock.set(**changes)
    await _save_clock(state)
    return state.to_json()


@app.post("/clock/start", tags=["clock"], dependencies=OPERATE)
async def start_clock() -> dict:
    return await update_clock(ClockUpdate(paused=False))


@app.post("/clock/pause", tags=["clock"], dependencies=OPERATE)
async def pause_clock() -> dict:
    return await update_clock(ClockUpdate(paused=True))


@app.post("/clock/jump", tags=["clock"], dependencies=OPERATE)
async def jump_clock(req: JumpRequest) -> dict:
    clock.clock.state = await clock.load_clock(ctx.db.engine)
    return (await _save_clock(clock.clock.jump(clock.parse_ts(req.to)))).to_json()


async def advance_to(target: datetime) -> dict:
    state = replace(clock.clock.state, sim_epoch=target, wall_anchor=time.time())
    await _save_clock(state)
    ticks = await _fan_out("POST", f"/_admin/tick?until={clock.iso(target)}")
    return {"now": clock.iso(target), "ticks": ticks}


@app.post("/clock/advance", tags=["clock"], dependencies=OPERATE)
async def advance(req: AdvanceRequest) -> dict:
    """Stepped mode: move sim time forward and run every service's due jobs up to it."""
    clock.clock.state = await clock.load_clock(ctx.db.engine)
    if req.to:
        target = clock.parse_ts(req.to)
    elif req.seconds is not None:
        target = clock.clock.now() + timedelta(seconds=req.seconds)
    else:
        raise HTTPException(422, "give 'to' or 'seconds'")
    if target < clock.clock.now():
        raise HTTPException(409, "cannot advance backwards; use /clock/jump")
    return await advance_to(target)


# -- reset / services / faults -------------------------------------------------------------
class ResetAll(BaseModel):
    seed: int | None = None
    epoch: str | None = Field(None, description="Sim start time; default 2026-10-27T08:55-04:00")
    mode: Literal["live", "stepped"] = "live"
    speed: float = 1.0
    paused: bool = True


@app.post("/reset", tags=["control"], dependencies=OPERATE)
async def reset_all(req: ResetAll) -> dict:
    current = await clock.load_clock(ctx.db.engine)
    seed = req.seed if req.seed is not None else current.seed
    epoch = clock.parse_ts(req.epoch) if req.epoch else clock.DEFAULT_EPOCH
    state = clock.ClockState(
        sim_epoch=epoch, wall_anchor=time.time(), speed=req.speed, paused=req.paused, mode=req.mode, seed=seed
    )
    await _save_clock(state)
    await ctx.reset(seed)
    results = await _fan_out("POST", "/_admin/reset", json={"seed": seed})
    return {"seed": seed, "clock": state.to_json(), "services": results}


@app.get("/services", tags=["control"], dependencies=READ)
async def services() -> dict:
    out = {}
    for name, url in service_urls().items():
        try:
            r = await ctx.http.get(url + "/", timeout=3)
            out[name] = {"url": url, "up": r.status_code == 200, **r.json()}
        except Exception as e:
            out[name] = {"url": url, "up": False, "error": type(e).__name__}
    return out


@app.get("/faults", tags=["faults"], dependencies=READ)
async def all_faults() -> dict:
    return await _fan_out("GET", "/_admin/faults")


@app.put("/faults/{service}", tags=["faults"], dependencies=OPERATE)
async def set_fault(service: str, cfg: FaultConfig) -> dict:
    url = service_urls().get(service)
    if url is None:
        raise HTTPException(404, f"unknown service {service}; have {list(service_urls())}")
    r = await ctx.http.put(url + "/_admin/faults", json=cfg.model_dump(), headers=_admin_headers())
    return r.json()


@app.delete("/faults", tags=["faults"], dependencies=OPERATE)
async def clear_faults() -> dict:
    return await _fan_out("PUT", "/_admin/faults", json=FaultConfig().model_dump())


# -- scenarios ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    mode: Literal["live", "stepped"] = "stepped"
    speed: float | None = Field(None, gt=0, description="Live mode only; default from the file")
    seed: int | None = None
    reset: bool = True


_runs: dict[str, dict[str, Any]] = {}
_run_tasks: dict[str, asyncio.Task] = {}
_run_ids = itertools.count(1)


@app.get("/scenarios", tags=["scenarios"], dependencies=READ)
async def list_scenarios() -> list[dict]:
    return [sc.load(p).summary() for p in sc.discover(SCENARIO_DIR).values()]


@app.get("/scenarios/{name}", tags=["scenarios"], dependencies=READ)
async def get_scenario(name: str) -> dict:
    path = sc.discover(SCENARIO_DIR).get(name)
    if path is None:
        raise HTTPException(404, "no such scenario")
    s = sc.load(path)
    return {
        **s.summary(),
        "steps": [{"at": st.at.isoformat(), "do": st.do, "args": st.args, "label": st.label} for st in s.steps],
    }


async def _execute(step: sc.Step) -> Any:
    if step.do == "wait":
        return None
    if step.do == "http":
        a = step.args
        url = service_urls()[a["service"]] + a["path"]
        r = await ctx.http.request(
            a.get("method", "POST"), url, json=a.get("json"), headers=_admin_headers(step.at), timeout=60
        )
        r.raise_for_status()
        return r.json() if r.content else None
    service, _, verb = step.do.partition(".")
    url = service_urls().get(service)
    if url is None or not verb:
        raise ValueError(f"bad step 'do': {step.do}")
    r = await ctx.http.post(
        f"{url}/_admin/scenario/{verb}", json=step.args, headers=_admin_headers(step.at), timeout=120
    )
    if r.status_code >= 400:
        raise RuntimeError(f"{step.do} -> {r.status_code}: {r.text[:500]}")
    return r.json()


async def _run(run_id: str, scenario: sc.Scenario, req: RunRequest) -> None:
    run = _runs[run_id]
    try:
        if req.reset:
            await reset_all(
                ResetAll(
                    seed=req.seed if req.seed is not None else scenario.seed,
                    epoch=scenario.start.isoformat(),
                    mode=req.mode,
                    speed=req.speed or scenario.speed,
                    paused=req.mode == "stepped",
                )
            )
        if req.mode == "live":
            await update_clock(ClockUpdate(paused=False, speed=req.speed or scenario.speed))
        for i, step in enumerate(scenario.steps):
            if req.mode == "stepped":
                await advance_to(step.at)
            else:
                while clock.clock.live_now() < step.at:  # noqa: ASYNC110 - polling the shared sim clock
                    await asyncio.sleep(0.02)
            run["current"] = i
            try:
                result = await _execute(step)
                run["log"].append({"i": i, "at": clock.iso(step.at), "do": step.do, "ok": True, "result": result})
            except Exception as e:
                run["log"].append({"i": i, "at": clock.iso(step.at), "do": step.do, "ok": False, "error": str(e)})
        if scenario.end:
            if req.mode == "stepped":
                await advance_to(scenario.end)
            else:
                while clock.clock.live_now() < scenario.end:  # noqa: ASYNC110
                    await asyncio.sleep(0.05)
        run["status"] = "completed"
    except asyncio.CancelledError:
        run["status"] = "stopped"
        raise
    except Exception as e:
        run["status"] = "failed"
        run["error"] = str(e)
    finally:
        async with ctx.session() as s:
            s.add(
                ScenarioRun(
                    id=run_id,
                    scenario=scenario.name,
                    mode=req.mode,
                    status=run["status"],
                    started_at=scenario.start,
                    log=run["log"],
                )
            )
            await s.commit()


@app.post("/scenarios/{name}/run", tags=["scenarios"], dependencies=OPERATE)
async def run_scenario(name: str, req: RunRequest | None = None, wait: bool = False) -> dict:
    req = req or RunRequest()
    path = sc.discover(SCENARIO_DIR).get(name)
    if path is None:
        raise HTTPException(404, "no such scenario")
    if any(not t.done() for t in _run_tasks.values()):
        raise HTTPException(409, "a scenario is already running")
    scenario = sc.load(path)
    run_id = f"run-{next(_run_ids):04d}"
    _runs[run_id] = {"id": run_id, "scenario": name, "mode": req.mode, "status": "running", "current": None,
                     "steps": len(scenario.steps), "log": []}
    task = asyncio.create_task(_run(run_id, scenario, req))
    _run_tasks[run_id] = task
    if wait:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return _runs[run_id]


@app.get("/runs", tags=["scenarios"], dependencies=READ)
async def list_runs() -> list[dict]:
    return [{k: v for k, v in r.items() if k != "log"} for r in _runs.values()]


@app.get("/runs/{run_id}", tags=["scenarios"], dependencies=READ)
async def get_run(run_id: str) -> dict:
    if run_id not in _runs:
        raise HTTPException(404, "no such run")
    return _runs[run_id]


@app.post("/runs/{run_id}/stop", tags=["scenarios"], dependencies=OPERATE)
async def stop_run(run_id: str) -> dict:
    task = _run_tasks.get(run_id)
    if task is None:
        raise HTTPException(404, "no such run")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return _runs[run_id]


@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {"header": "Authorization: Bearer", "operatorKey": ctx.gate.bootstrap_key("OPERATOR"),
            "observerKey": ctx.gate.bootstrap_key("OBSERVER")}
