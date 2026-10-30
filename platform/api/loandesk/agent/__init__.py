"""The agentic layer: a record/replay model gateway, the tool registry, and LangGraph task graphs.

Nothing here can cause an external side effect except through the approval gate. With
``LOANDESK_AGENT_MODE=off`` (the default) no model is ever called. LangGraph comes with the ``agent``
extra; without it the desk runs and the agent endpoints say so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import Runner

_runner: Runner | None = None
missing: str | None = None  # why there is no runner, for the status panel


async def start() -> Runner | None:
    global _runner, missing
    try:
        from .runner import Runner
    except ImportError as e:
        missing = f"install the agent extra (uv sync --extra agent): {e.name} is missing"
        return None
    _runner = Runner()
    await _runner.start()
    return _runner


async def stop() -> None:
    global _runner
    if _runner is not None:
        await _runner.stop()
        _runner = None


def current() -> Runner | None:
    return _runner
