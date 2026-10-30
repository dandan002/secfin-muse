"""Tools the agent may call (spec: *Agent tool list*). Each is the same service code the trader's screens use.

All are read-only except ``create_action``, which can only propose to the approval gate.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock, gate, secmaster
from ..models import Counterparty, Event, Locate
from ..secmaster import resolve
from ..services import locates, market, orders

Handler = Callable[[AsyncSession, dict[str, Any], "Ctx"], Awaitable[Any]]


@dataclass
class Ctx:
    actor: str  # agent:<model>
    transcript: list[Any]
    source_ref: str | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, **props: dict[str, Any]) -> Callable[[Handler], Handler]:
    required = [k for k, v in props.items() if not v.pop("optional", False)]

    def deco(fn: Handler) -> Handler:
        REGISTRY[name] = Tool(name, description, {"type": "object", "properties": props, "required": required,
                                                  "additionalProperties": False}, fn)
        return fn

    return deco


def schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    return [t.schema() for n, t in REGISTRY.items() if names is None or n in names]


async def call(s: AsyncSession, name: str, args_json: str, ctx: Ctx) -> str:
    """Run one tool call; errors go back to the model as data, never raise out of the loop."""
    t = REGISTRY.get(name)
    if t is None:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        args = json.loads(args_json or "{}")
        out = await t.handler(s, args, ctx)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    ctx.transcript.append({"step": "tool", "tool": name, "args": args_json, "result": out})
    return json.dumps(out, default=str)


STR = {"type": "string"}
INT = {"type": "integer"}
NUM = {"type": "number"}


@tool("resolve_instrument", "Resolve a ticker, ISIN, SEDOL, CUSIP or FIGI to one instrument with live data.",
      identifier=STR)
async def _resolve(s: AsyncSession, a: dict[str, Any], ctx: Ctx) -> Any:
    return (await resolve(s, a["identifier"])).to_json()


@tool("get_market", "Live availability by venue, last prints, fee trend, utilization and tier for one name.",
      identifier=STR)
async def _market(s: AsyncSession, a: dict[str, Any], ctx: Ctx) -> Any:
    res = await resolve(s, a["identifier"])
    if res.instrument is None:
        return {"error": res.error, "suggestions": res.suggestions}
    return await market.market_for(s, res.instrument)


@tool("get_fee_history", "Daily close fee, utilization and tier for one name over the last N business days "
      "(default 20, max 60), oldest first.", identifier=STR, days={"type": "integer", "optional": True})
async def _fee_history(s: AsyncSession, a: dict[str, Any], ctx: Ctx) -> Any:
    res = await resolve(s, a["identifier"])
    if res.instrument is None:
        return {"error": res.error, "suggestions": res.suggestions}
    return secmaster.history(res.instrument.id, min(max(int(a.get("days") or 20), 1), 60))


@tool("get_counterparty", "Counterparty reference data: codes, LEI, credit limit, agreement, collateral schedule.",
      code=STR)
async def _cp(s: AsyncSession, a: dict[str, Any], ctx: Ctx) -> Any:
    cp = await s.get(Counterparty, a["code"].upper())
    if cp is None:
        return {"error": "unknown counterparty"}
    return {"code": cp.code, "name": cp.name, "kind": cp.kind, "lei": cp.lei, "creditLimitUsd": cp.credit_limit_usd,
            "exposureUsd": await orders.exposure_usd(s, cp.code), "agreement": cp.agreement,
            "collateralSchedule": cp.collateral_schedule}


@tool("check_limits", "Run the deterministic pre-trade checks and routing for a proposed order (sends nothing).",
      side={"type": "string", "enum": ["BORROW", "LEND"]}, identifier=STR, quantity=INT, feeBps=NUM,
      counterparty={"type": "string", "optional": True})
async def _limits(s: AsyncSession, a: dict[str, Any], ctx: Ctx) -> Any:
    return await orders.preview(s, orders.OrderIn(side=a["side"], instrument=a["identifier"], quantity=a["quantity"],
                                                  feeBps=a["feeBps"], counterparty=a.get("counterparty")))


@tool("get_locate", "One locate line with its routing reasons, live availability and suggested quantity.",
      locateId=STR)
async def _locate(s: AsyncSession, a: dict[str, Any], ctx: Ctx) -> Any:
    loc = await s.get(Locate, a["locateId"])
    if loc is None:
        return {"error": "unknown locate"}
    rows = [r for r in await locates.listing(s, limit=2000) if r["id"] == loc.id]
    return rows[0] if rows else locates.locate_json(loc)


@tool("search_history", "Recent events for an entity (locate, order, trade, action) or a counterparty's locates.",
      entity={"type": "string", "enum": ["locate", "order", "trade", "action", "counterparty"]}, id=STR,
      limit={"type": "integer", "optional": True})
async def _history(s: AsyncSession, a: dict[str, Any], ctx: Ctx) -> Any:
    n = min(int(a.get("limit") or 20), 100)
    if a["entity"] == "counterparty":
        rows = (await s.execute(select(Locate).where(Locate.requester_cp == a["id"].upper())
                                .order_by(Locate.received_at.desc()).limit(n))).scalars()
        return [locates.locate_json(x) for x in rows]
    rows = (await s.execute(select(Event).where(Event.entity == a["entity"], Event.entity_id == a["id"])
                            .order_by(Event.id.desc()).limit(n))).scalars()
    return [{"ts": clock.iso(e.ts), "type": e.type, "actor": e.actor, "after": e.after} for e in rows]


@tool("create_action", "Propose an action for the trader's approval queue. Nothing executes until a human approves.",
      kind={"type": "string", "enum": ["locate_decision", "order", "chat_send", "booking", "amend", "cancel"]},
      title=STR, payload={"type": "object"}, rationale=STR,
      riskFlags={"type": "array", "items": STR, "optional": True})
async def _create_action(s: AsyncSession, a: dict[str, Any], ctx: Ctx) -> Any:
    act = await gate.propose(s, kind=a["kind"], title=a["title"], payload=a["payload"], proposed_by=ctx.actor,
                             rationale=a["rationale"], risk_flags=a.get("riskFlags"), transcript=list(ctx.transcript),
                             source_ref=ctx.source_ref)
    return {"actionId": act.id, "status": act.status}
