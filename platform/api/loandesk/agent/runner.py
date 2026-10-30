"""Runs the agent's graphs in the background and keeps their checkpoints.

Each run is one LangGraph thread (``thread_id`` = ``AgentRun.id``). The ``agent_runs`` row is the desk's
index of runs (status, the model holding the case, the gate action it waits on); the graph state itself
lives in the checkpointer: the desk's Postgres in production, a sibling SQLite file locally.

A run that pauses at the gate is resumed by ``action_decided()`` once the trader's verdict is committed.
On start-up ``recover()`` continues runs a restart interrupted, including ones whose card was decided
while the desk was down.
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

from .. import clock, db, events
from ..models import Action, AgentRun
from ..settings import Settings, get_settings
from .graph import review_graph

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
           "updatedAt": clock.iso(r.updated_at)}
    if state is not None:
        out.update(escalated=state.get("escalated", False), decision=state.get("decision"),
                   checks=state.get("checks"), concerns=state.get("concerns"), advisories=state.get("advisories"),
                   signals=(state.get("facts") or {}).get("signals"), outcome=state.get("outcome"),
                   transcript=state.get("transcript", []))
    return out


class Runner:
    def __init__(self) -> None:
        self._stack = contextlib.AsyncExitStack()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.graph: Any = None

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
        self.graph = review_graph().compile(checkpointer=saver)
        await self.recover()

    async def stop(self) -> None:
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
        cfg = get_settings()
        async with db.session() as s:
            r = AgentRun(id=events.new_id("run"), task="review_locate", subject=locate_id, status="RUNNING",
                         model=cfg.reviewer_model, created_at=clock.now(), updated_at=clock.now())
            s.add(r)
            events.record(s, "agent_run", r.id, "started", run_json(r), actor=f"agent:{r.model}")
            await s.commit()
        self._spawn(r.id, self._initial(r))
        return r

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
                snap = await self.graph.aget_state(_config(r.id))
                if r.status == "RUNNING":  # continue from the last checkpoint (or from the top if there is none)
                    todo.append((r.id, None if snap.values else self._initial(r)))
                elif r.action_id and (a := await s.get(Action, r.action_id)) and a.status != "PENDING":
                    todo.append((r.id, Command(resume=_verdict(a))))
        for run_id, inp in todo:
            log.info("agent: recovering run %s", run_id)
            self._spawn(run_id, inp)

    @staticmethod
    def _initial(r: AgentRun) -> dict[str, Any]:
        return {"run_id": r.id, "locate_id": r.subject, "model": r.model}

    def _spawn(self, run_id: str, inp: Any) -> None:
        if run_id in self._tasks:  # one driver per run; the next event finds it at its checkpoint
            return
        t = asyncio.create_task(self._drive(run_id, inp), name=f"agent-{run_id}")
        self._tasks[run_id] = t
        t.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def _drive(self, run_id: str, inp: Any) -> None:
        cfg = _config(run_id)
        while inp is not _DONE:
            error: str | None = None
            try:
                async for update in self.graph.astream(inp, cfg, stream_mode="updates"):
                    for node in update:
                        events.hub.publish("agent_run", {"type": "step", "id": run_id, "node": node})
            except Exception as e:  # a bug in a node: the run is marked failed and the locate stays with the trader
                log.exception("agent run %s failed", run_id)
                error = f"{type(e).__name__}: {e}"
            snap = await self.graph.aget_state(cfg)
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
                r.action_id = values.get("action_id", r.action_id)
                events.record(s, "agent_run", r.id, status.lower(), run_json(r), actor=f"agent:{r.model}")
                await s.commit()
                # The trader may have decided the card while this driver was still settling (so action_decided
                # found it busy): resume now rather than leave the run waiting on a decided action.
                if status == "WAITING" and r.action_id and (a := await s.get(Action, r.action_id)):
                    if a.status != "PENDING":
                        inp = Command(resume=_verdict(a))

    async def state(self, run_id: str) -> dict[str, Any]:
        return (await self.graph.aget_state(_config(run_id))).values or {}


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


def _verdict(a: Action) -> dict[str, Any]:
    return {"actionId": a.id, "status": a.status, "approver": a.approver, "result": a.result}
