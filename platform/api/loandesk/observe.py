"""Model observability: every model call and agent run traced to Langfuse, when it's configured.

On only with Langfuse keys (``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, optionally ``LANGFUSE_HOST``) and
the agent calling real models (``record`` or ``live``): replay, CI and ``off`` never trace. Off, every helper here
is a no-op and nothing is imported.

* A **run** is one LangGraph pass of an agent task: a trace whose session is the run id, so a run that pauses at
  the gate and resumes reads as one session. LangGraph's nodes are spans inside it (via the LangChain callback).
* A **generation** is one model call through the gateway: model, messages, tokens, the desk's own cost.
* A **read** is the chat parser reading one message: a trace whose session is the chat room.

Traces carry the desk's ids (run, locate, gate action, chat message) as metadata, so a trace can be found from
the card and the card from the trace. This is engineering and model-risk visibility; the audit trail of record
stays the desk's own append-only event log.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

from .settings import get_settings

log = logging.getLogger("loandesk.observe")
_client: Any = None
_failed = False
# The last trace each agent run wrote, for the desk's run list to link to. In memory: a restart loses the link,
# not the trace (the run id finds it as a Langfuse session).
trace_urls: dict[str, str] = {}


def enabled() -> bool:
    cfg = get_settings()
    return bool(cfg.langfuse_public_key and cfg.langfuse_secret_key) and cfg.agent_mode in ("record", "live") \
        and client() is not None


def client() -> Any:
    """The Langfuse client, made once; None when it isn't configured or installed."""
    global _client, _failed
    if _client is not None or _failed:
        return _client
    cfg = get_settings()
    if not (cfg.langfuse_public_key and cfg.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        log.warning("Langfuse keys are set but the package isn't installed: uv sync --extra observe")
        _failed = True
        return None
    _client = Langfuse(public_key=cfg.langfuse_public_key, secret_key=cfg.langfuse_secret_key,
                       host=cfg.langfuse_host, environment=cfg.langfuse_environment)
    return _client


def status() -> dict[str, Any]:
    cfg = get_settings()
    return {"langfuse": "on" if enabled() else "off", "host": cfg.langfuse_host if enabled() else None}


class Generation:
    """One model call's observation; ``done`` and ``failed`` are no-ops when tracing is off."""

    def __init__(self, obs: Any = None) -> None:
        self._obs = obs

    def done(self, resp: dict[str, Any], cost_usd: float) -> None:
        if self._obs is None:
            return
        usage = resp.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        self._obs.update(
            output=(resp.get("choices") or [{}])[0].get("message"),
            usage_details={"input": int(usage.get("prompt_tokens") or 0) - cached, "input_cached": cached,
                           "output": int(usage.get("completion_tokens") or 0)},
            cost_details={"total": cost_usd})

    def failed(self, err: BaseException) -> None:
        if self._obs is not None:
            self._obs.update(level="ERROR", status_message=f"{type(err).__name__}: {err}")


@contextlib.contextmanager
def generation(req: dict[str, Any]) -> Iterator[Generation]:
    """Wrap one model call: the request's model, messages and parameters in; the answer, tokens and cost out."""
    if not enabled():
        yield Generation()
        return
    params = {k: v for k, v in req.items() if k not in ("model", "messages", "tools", "response_format")}
    if req.get("tools"):
        params["tools"] = [t.get("function", {}).get("name") for t in req["tools"]]
    if req.get("response_format"):
        params["response_format"] = req["response_format"].get("type")
    with client().start_as_current_observation(as_type="generation", name=f"chat {req['model']}",
                                               model=req["model"], input=req["messages"],
                                               model_parameters=params) as obs:
        yield Generation(obs)


@contextlib.asynccontextmanager
async def run(task: str, run_id: str, **ids: str | None) -> AsyncIterator[list[Any]]:
    """Trace one pass of an agent run. Yields the LangGraph callbacks to pass in the run's config."""
    if not enabled():
        yield []
        return
    from langfuse import propagate_attributes
    from langfuse.langchain import CallbackHandler

    lf = client()
    meta = {"runId": run_id, **{k: v for k, v in ids.items() if v}}
    with lf.start_as_current_observation(as_type="agent", name=task, metadata=meta), \
            propagate_attributes(session_id=run_id, trace_name=task, tags=["agent", task], metadata=meta):
        try:
            yield [CallbackHandler(public_key=get_settings().langfuse_public_key)]  # this client, never another
        finally:
            with contextlib.suppress(Exception):  # the link is a convenience; never fail a run for it
                if url := lf.get_trace_url():
                    trace_urls[run_id] = url


@contextlib.contextmanager
def read(room: str, message_id: str, counterparty: str | None) -> Iterator[None]:
    """Trace the chat parser reading one message, in a session per chat room."""
    if not enabled():
        yield
        return
    from langfuse import propagate_attributes

    meta = {"room": room, "messageId": message_id, "counterparty": counterparty or "unknown"}
    with client().start_as_current_observation(as_type="span", name="read_chat", metadata=meta), \
            propagate_attributes(session_id=f"room:{room}", trace_name="read_chat", tags=["chat-parser"],
                                 metadata=meta):
        yield


def flush() -> None:
    """Send what's buffered (on shutdown)."""
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.flush()
