"""Runs the agent's graphs in the background and keeps their checkpoints.

Each run is one LangGraph thread (``thread_id`` = ``AgentRun.id``). The ``agent_runs`` row is the desk's
index of runs (status, the model holding the case, the gate action it waits on); the graph state itself
lives in the checkpointer: the desk's Postgres in production, a sibling SQLite file locally.

A run that pauses at the gate is resumed by ``action_decided()`` once the trader's verdict is committed.
On start-up ``recover()`` continues runs a restart interrupted, including ones whose card was decided
while the desk was down.

The team: locate reviews (``graph.py``), trade reviews (``trade.py``) and the queue planner (``queue.py``) each
run in a lane of their own, so a burst of one kind never starves the others and the model spend comes in at a
bounded rate. A lane runs up to its size at once; the rest wait their turn (still ``RUNNING`` in the index).
New rule-drafted cards get a trade review as they're proposed; the planner re-plans when the queue has changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select

from .. import clock, db, events, observe
from ..models import Action, AgentRun
from ..settings import Settings, get_settings
from .graph import review_graph
from .queue import plan_graph
from .trade import on as trade_on
from .trade import reviewable, trade_graph

log = logging.getLogger("loandesk.agent")
_DONE = object()


def checkpoint_url(cfg: Settings) -> str:
    if cfg.agent_checkpoint_url:
        return cfg.agent_checkpoint_url
    url = cfg.database_url
    if url.startswith("postgresql"):
        return url.replace("+asyncpg", "")
    path = url.split(":///", 1)[1] if ":///" in url else ""
    if not path or path == ":memory:":
        return "memory"
    return str(Path(path).with_suffix(".agent.db"))


def run_json(r: AgentRun, state: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {"id": r.id, "task": r.task, "subject": r.subject, "status": r.status, "model": r.model,
           "actionId": r.action_id, "error": r.error, "createdAt": clock.iso(r.created_at),
           "updatedAt": clock.iso(r.updated_at), "traceUrl": observe.trace_urls.get(r.id)}
    if state is not None:
        out.update(escalated=state.get("escalated", False), decision=state.get("decision"),
                   checks=state.get("checks"), concerns=state.get("concerns"), advisories=state.get("advisories"),
                   signals=(state.get("facts") or {}).get("signals"), outcome=state.get("outcome"),
                   transcript=state.get("transcript", []))
    return out


class Lane:
    """Up to ``size`` runs of one kind at once."""

    def __init__(self, size: int) -> None:
        self.size, self.running, self.waiting = size, 0, 0
        self._sem = asyncio.Semaphore(size)

    @contextlib.asynccontextmanager
    async def slot(self) -> Any:
        self.waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self.waiting -= 1
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self._sem.release()

    def json(self) -> dict[str, int]:
        return {"size": self.size, "running": self.running, "waiting": self.waiting}


class Runner:
    def __init__(self) -> None:
        cfg = get_settings()
        self._stack = contextlib.AsyncExitStack()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._loops: list[asyncio.Task[None]] = []
        self.graphs: dict[str, Any] = {}
        self.lanes = {"review_locate": Lane(cfg.agent_locate_workers), "review_trade": Lane(cfg.agent_trade_workers),
                      "plan_queue": Lane(1)}
        self._planned: tuple[float, frozenset[str]] = (0.0, frozenset())

    @property
    def graph(self) -> Any:  # the locate review graph (the API's run view, tests)
        return self.graphs["review_locate"]

    async def start(self) -> None:
        url = checkpoint_url(get_settings())
        if url == "memory":
            saver: Any = InMemorySaver()
        elif url.startswith("postgres"):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver = await self._stack.enter_async_context(AsyncPostgresSaver.from_conn_string(url))
            await saver.setup()
        else:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            saver = await self._stack.enter_async_context(AsyncSqliteSaver.from_conn_string(url))
        self.graphs = {"review_locate": review_graph().compile(checkpointer=saver),
                       "review_trade": trade_graph().compile(checkpointer=saver),
                       "plan_queue": plan_graph().compile(checkpointer=saver)}
        await self.recover()
        self._loops = [asyncio.create_task(self._watch(), name="agent-team-watch"),
                       asyncio.create_task(self._planner(), name="agent-team-planner")]

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        for t in self._loops:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        for t in self._tasks.values():
            t.cancel()
        for t in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self._stack.aclose()

    async def idle(self) -> None:
        """Wait for every in-flight run step (tests)."""
        while self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # -- starting and resuming -------------------------------------------------------------------
    async def review_locate(self, locate_id: str) -> AgentRun:
        return await self._start("review_locate", locate_id, get_settings().reviewer_model)

    async def review_trade(self, action_id: str) -> AgentRun:
        """A second pair of eyes on a rule-drafted card; its read lands on the card."""
        return await self._start("review_trade", action_id, get_settings().reviewer_model)

    async def plan_queue(self) -> AgentRun:
        """Read the whole queue: dispatch sub-agents, then rank, batch and note it."""
        return await self._start("plan_queue", "queue", get_settings().orchestrator_model)

    def busy(self, task: str) -> bool:
        lane = self.lanes[task]
        return lane.running + lane.waiting > 0

    def status(self) -> dict[str, Any]:
        return {task: lane.json() for task, lane in self.lanes.items()}

    async def _start(self, task: str, subject: str, model: str) -> AgentRun:
        """One live run per subject: a second ask (the Review button twice, the autopilot and the planner both) gets
        the run already going, not a second card for the same locate."""
        async with db.session() as s:
            live = (await s.execute(select(AgentRun).where(AgentRun.task == task, AgentRun.subject == subject,
                                                           AgentRun.status.in_(("RUNNING", "WAITING")))
                                    .limit(1))).scalar()
            if live is not None:
                return live
            r = AgentRun(id=events.new_id("run"), task=task, subject=subject, status="RUNNING", model=model,
                         created_at=clock.now(), updated_at=clock.now())
            s.add(r)
            events.record(s, "agent_run", r.id, "started", run_json(r), actor=f"agent:{r.model}")
            await s.commit()
        self._spawn(r.id, self._initial(r))
        return r

    # -- the team's own triggers ------------------------------------------------------------------
    async def _watch(self) -> None:
        """A trade review for each rule-drafted card as it's proposed."""
        q = events.hub.subscribe({"action"})
        try:
            while True:
                msg = await q.get()
                d = msg.get("data") if isinstance(msg, dict) else None
                if (msg.get("topic") != "action" or not isinstance(d, dict) or d.get("type") != "proposed"
                        or not trade_on()):
                    continue
                try:
                    async with db.session() as s:
                        a = await s.get(Action, d.get("id"))
                    if a is not None and reviewable(a):
                        await self.review_trade(a.id)
                except Exception:  # one card never stops the watch
                    log.exception("agent team: couldn't start a trade review")
        finally:
            events.hub.unsubscribe(q)

    async def _planner(self) -> None:
        """Re-plan when the queue has changed, at most every ``agent_plan_every_s``."""
        while True:
            await asyncio.sleep(15)
            every = get_settings().agent_plan_every_s
            if every <= 0 or self.busy("plan_queue") or asyncio.get_running_loop().time() - self._planned[0] < every:
                continue
            try:
                async with db.session() as s:
                    ids = frozenset((await s.execute(select(Action.id).where(Action.status == "PENDING"))).scalars())
                if ids and ids != self._planned[1]:
                    self._planned = (asyncio.get_running_loop().time(), ids)
                    await self.plan_queue()
            except Exception:
                log.exception("agent team: couldn't start the queue planner")

    async def action_decided(self, action: Action) -> None:
        """The trader decided a card: resume the run waiting on it, if any."""
        async with db.session() as s:
            r = (await s.execute(select(AgentRun).where(AgentRun.action_id == action.id,
                                                        AgentRun.status == "WAITING"))).scalar_one_or_none()
        if r is not None:
            self._spawn(r.id, Command(resume=_verdict(action)))

    async def recover(self) -> None:
        async with db.session() as s:
            runs = (await s.execute(select(AgentRun).where(AgentRun.status.in_(("RUNNING", "WAITING"))))).scalars()
            todo: list[tuple[str, Any]] = []
            for r in runs:
                if r.task not in self.graphs:
                    continue
                snap = await self.graphs[r.task].aget_state(_config(r.id))
                if r.status == "RUNNING":  # continue from the last checkpoint (or from the top if there is none)
                    todo.append((r.id, None if snap.values else self._initial(r)))
                elif r.action_id and (a := await s.get(Action, r.action_id)) and a.status != "PENDING":
                    todo.append((r.id, Command(resume=_verdict(a))))
        for run_id, inp in todo:
            log.info("agent: recovering run %s", run_id)
            self._spawn(run_id, inp)

    @staticmethod
    def _initial(r: AgentRun) -> dict[str, Any]:
        if r.task == "review_trade":
            return {"run_id": r.id, "action_id": r.subject, "model": r.model}
        if r.task == "plan_queue":
            return {"run_id": r.id, "model": r.model}
        return {"run_id": r.id, "locate_id": r.subject, "model": r.model}

    def _spawn(self, run_id: str, inp: Any) -> None:
        if run_id in self._tasks:  # one driver per run; the next event finds it at its checkpoint
            return
        t = asyncio.create_task(self._drive(run_id, inp), name=f"agent-{run_id}")
        self._tasks[run_id] = t
        t.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def _drive(self, run_id: str, inp: Any) -> None:
        async with db.session() as s:
            known = await s.get(AgentRun, run_id)
        task, subject, action = (known.task, known.subject, known.action_id) if known else ("review_locate", None, None)
        graph = self.graphs[task]
        lane = self.lanes.get(task)
        async with lane.slot() if lane else contextlib.nullcontext():
            await self._steps(run_id, graph, task, subject, action, inp)

    async def _steps(self, run_id: str, graph: Any, task: str, subject: str | None, action: str | None,
                     inp: Any) -> None:
        cfg = _config(run_id)
        while inp is not _DONE:
            error: str | None = None
            try:
                # Each pass (the first, and each resume after the gate) is one trace in the run's session.
                async with observe.run(task, run_id, subject=subject, actionId=action,
                                       resumed="yes" if isinstance(inp, Command) else None) as callbacks:
                    async for update in graph.astream(inp, {**cfg, "callbacks": callbacks} if callbacks else cfg,
                                                           stream_mode="updates"):
                        for node in update:
                            events.hub.publish("agent_run", {"type": "step", "id": run_id, "node": node})
            except Exception as e:  # a bug in a node: the run is marked failed and the locate stays with the trader
                log.exception("agent run %s failed", run_id)
                error = f"{type(e).__name__}: {e}"
            snap = await graph.aget_state(cfg)
            values = snap.values or {}
            error = error or values.get("error")
            status = "FAILED" if error else "WAITING" if snap.next else "DONE"
            inp = _DONE
            async with db.session() as s:
                r = await s.get(AgentRun, run_id)
                if r is None:
                    return
                r.status, r.error, r.updated_at = status, error, clock.now()
                r.model = values.get("model", r.model)
                if task == "review_locate":  # a trade review's action_id is the card it read, not one it made
                    r.action_id = values.get("action_id", r.action_id)
                action = r.action_id
                events.record(s, "agent_run", r.id, status.lower(), run_json(r), actor=f"agent:{r.model}")
                if task == "review_trade" and status == "FAILED" and (a := await s.get(Action, r.subject)) \
                        and a.status == "PENDING" and a.review is None:  # unread: say so, so nothing waits on it
                    a.review = {"verdict": "UNREAD", "notes": f"the trade reviewer couldn't read it: {error}"[:240],
                                "model": r.model, "at": clock.iso(clock.now()), "runId": r.id}
                    from ..gate import action_json

                    events.record(s, "action", a.id, "reviewed", action_json(a), actor=f"agent:{r.model}")
                await s.commit()
                # The trader may have decided the card while this driver was still settling (so action_decided
                # found it busy): resume now rather than leave the run waiting on a decided action.
                if status == "WAITING" and r.action_id and (a := await s.get(Action, r.action_id)):
                    if a.status != "PENDING":
                        inp = Command(resume=_verdict(a))

    async def state(self, run_id: str) -> dict[str, Any]:
        async with db.session() as s:
            r = await s.get(AgentRun, run_id)
        g = self.graphs.get(r.task if r else "review_locate", self.graph)
        return (await g.aget_state(_config(run_id))).values or {}


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


def _verdict(a: Action) -> dict[str, Any]:
    return {"actionId": a.id, "status": a.status, "approver": a.approver, "result": a.result}
