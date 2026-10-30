"""Score chat ingest against mock-ib's labelled corpus (spec: *Evals and safety*, the labelled chat set).

    (cd mocks/ib && uv run mock-ib-corpus --out out/chat_corpus.jsonl --n 300)
    (cd platform/api && uv run python -m loandesk.ingest.chat_eval ../../mocks/ib/out/chat_corpus.jsonl)
    (cd platform/api && LOANDESK_AGENT_MODE=record uv run python -m loandesk.ingest.chat_eval \\
        ../../mocks/ib/out/chat_corpus.jsonl --model --conversations 40)

Each corpus line is a conversation: the counterparty's messages and ours, each with a gold label.

* Rules (default): the counterparty's messages, one at a time, context-free. No model is called.
* ``--model``: every message, ours and theirs, replayed in order through the parser model with the
  conversation so far, exactly as live ingest reads it (injections are still flagged by the rules first).
  Costs a model call per message; ``--conversations`` takes a sample.

Reported: intent accuracy per intent; how much is read alone (confidence at or over ``chat_confident``)
versus handed to the trader; for messages that name securities, whether every line (instrument, quantity)
is exactly right, and for quotes and acceptances whether every fee (or GC) is too. Misses that matter:
injections not flagged, and messages read alone with the wrong intent or lines.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from refdata.counterparties import counterparties

from .. import db, secmaster
from ..models import Counterparty
from ..settings import get_settings
from .chat import NAMED, Reading, read_model, read_rules

OURS = ("srivera", "kli", "loandesk-agent")
PRICED = ("QUOTE", "ACCEPT", "OFFER")


def _key(ln: dict[str, Any]) -> tuple[Any, Any]:
    return ln.get("instrumentId"), ln.get("quantity")


def _price(ln: dict[str, Any]) -> Any:
    return "GC" if ln.get("rate") == "GC" else round(float(ln["feeBps"])) if ln.get("feeBps") is not None else None


def _tally(by: dict[str, Counter[str]], misses: list[dict[str, Any]], text: str, gold: dict[str, Any],
           r: Reading, confident: float) -> None:
    acted = r.confidence >= confident or r.intent == "INJECTION"
    c = by[gold["intent"]]
    c["n"] += 1
    c["right"] += r.intent == gold["intent"]
    c["acted"] += acted
    c["toTrader"] += not acted
    if acted and gold["intent"] in NAMED and gold["lines"]:
        want, got = sorted(map(_key, gold["lines"])), sorted(map(_key, r.lines))
        if r.intent == gold["intent"] and got == want:
            c["linesExact"] += 1
            if gold["intent"] in PRICED:
                c["priced"] += 1
                wp = {ln["instrumentId"]: _price(ln) for ln in gold["lines"]}
                c["pricesExact"] += all(_price(ln) == wp.get(ln["instrumentId"]) for ln in r.lines)
        else:
            misses.append({"text": text, "gold": gold["intent"], "read": r.intent, "want": want, "got": got})
    elif acted and r.intent != gold["intent"] and "INJECTION" in (r.intent, gold["intent"]):
        misses.append({"text": text, "gold": gold["intent"], "read": r.intent})


def _summary(by: dict[str, Counter[str]], misses: list[dict[str, Any]]) -> dict[str, Any]:
    n = sum(c["n"] for c in by.values())
    return {"messages": n, "intentAccuracy": round(sum(c["right"] for c in by.values()) / max(n, 1), 4),
            "actedAlone": round(sum(c["acted"] for c in by.values()) / max(n, 1), 4),
            "byIntent": {k: dict(v) for k, v in sorted(by.items())}, "unsafe": misses}


def score(messages: list[dict[str, Any]], confident: float) -> dict[str, Any]:
    """Rules only: the counterparty's messages, context-free."""
    kinds = {c.code: c.kind for c in counterparties()}
    by: dict[str, Counter[str]] = defaultdict(Counter)
    misses: list[dict[str, Any]] = []
    for m in messages:
        if m["label"].get("side") == "US":
            continue
        _tally(by, misses, m["text"], m["label"], read_rules(m["text"], kinds.get(m["label"]["counterparty"])),
               confident)
    return _summary(by, misses)


async def score_model(convs: list[dict[str, Any]], confident: float) -> dict[str, Any]:
    """Every message in order through the parser model, with the conversation so far."""
    cfg = get_settings()
    secmaster.set_seed(cfg.seed)
    with tempfile.TemporaryDirectory() as tmp:
        db.configure(f"sqlite+aiosqlite:///{tmp}/eval.db")
        await db.create_all()
        by: dict[str, Counter[str]] = defaultdict(Counter)
        misses: list[dict[str, Any]] = []
        async with db.session() as s:
            await secmaster.load(s)
            for conv in convs:
                cp = await s.get(Counterparty, conv["counterparty"])
                at = datetime.fromisoformat(conv["ts"])
                context: list[Any] = []
                for seq, m in enumerate(conv["messages"], start=1):
                    side = "US" if m["from"] in OURS else "THEM"
                    msg = {"messageId": f"{conv['conversationId']}-{seq}", "streamId": conv["room"], "seq": seq,
                           "text": m["text"], "ts": (at + timedelta(minutes=seq)).isoformat(),
                           "sender": {"handle": m["from"], "kind": "human" if side == "US" else "persona"}}
                    r = read_rules(m["text"], cp.kind if cp else None)
                    if not (side == "THEM" and r.intent == "INJECTION"):
                        r = await read_model(s, msg, side, cp, context)
                    if "label" in m:
                        _tally(by, misses, m["text"], m["label"], r, confident)
                    context.append(SimpleNamespace(side=side, seq=seq, sender=m["from"], text=m["text"],
                                                   sent_at=at + timedelta(minutes=seq)))
        await db.dispose()
    return _summary(by, misses)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="chat-eval")
    ap.add_argument("corpus", type=Path, help="JSONL from mock-ib-corpus")
    ap.add_argument("--model", action="store_true", help="read every message with the parser model, in context")
    ap.add_argument("--conversations", type=int, default=None, help="score only the first N conversations")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args(argv)
    convs = [json.loads(line) for line in args.corpus.read_text().splitlines() if line.strip()][:args.conversations]
    confident = get_settings().chat_confident
    if args.model:
        out = asyncio.run(score_model(convs, confident))
    else:
        out = score([m for c in convs for m in c["messages"] if "label" in m], confident)
    if args.json:
        print(json.dumps(out, indent=2))
        return
    print(f"{out['messages']} labelled messages | intent accuracy {out['intentAccuracy']:.1%} | "
          f"read alone {out['actedAlone']:.1%} | misses {len(out['unsafe'])}")
    print(f"{'intent':16} {'n':>4} {'right':>6} {'alone':>6} {'to trader':>10} {'lines exact':>12} {'prices':>7}")
    for k, c in out["byIntent"].items():
        exact = str(c.get("linesExact", 0)) if k in NAMED else "-"
        priced = f"{c.get('pricesExact', 0)}/{c.get('priced', 0)}" if k in PRICED else "-"
        print(f"{k:16} {c['n']:>4} {c.get('right', 0):>6} {c.get('acted', 0):>6} {c.get('toTrader', 0):>10} "
              f"{exact:>12} {priced:>7}")
    for u in out["unsafe"][:12]:
        print("miss:", json.dumps(u)[:240])


if __name__ == "__main__":
    main()
