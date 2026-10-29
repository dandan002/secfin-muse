# LoanDesk AI: the desk platform

Our side of the wire: the securities-lending desk that talks to the mock market in [`../mocks`](../mocks/README.md).
This first cut is the **manual desk** (the trader drives everything) with the **bases for the agent** in place
but switched off.

> Synthetic data only. Not affiliated with or endorsed by EquiLend, Wematch, FIS or Bloomberg.

```
platform/
  api/   loandesk-platform: FastAPI, SQLAlchemy (SQLite locally, Postgres in prod), port 8100
  web/   trader workspace: React + TypeScript + Vite + Tailwind + Zustand, port 5180
```

## Run it

```bash
# 1. the market (see mocks/README.md); any Postgres works, e.g. a throwaway one:
docker run -d --rm --name loandesk-pg -p 127.0.0.1:55432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16.6-alpine
(cd mocks/harness && uv run python run_all.py --db 'postgresql+asyncpg://postgres@127.0.0.1:55432/loandesk_{service}')

# 2. the desk
(cd platform/api && uv run uvicorn loandesk.app:app --port 8100)
(cd platform/web && npm ci && npm run dev)          # http://localhost:5180

# 3. start the sim clock and the demo flow
KEY=$(curl -s localhost:8000/_admin/credentials | jq -r .operatorKey)
curl -XPOST localhost:8000/scenarios/demo/run -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"mode":"live","speed":10}'
```

The desk works with the mocks down too: reference data, rules, locates and the paste parser are local; anything
that needs a venue fails visibly (order `FAILED`, reply `FAILED`) instead of hanging.

## What's built (spec module → code)

| Spec | Backend | Screen |
| --- | --- | --- |
| 1. Security master | `secmaster.py`: resolve ticker/ISIN/SEDOL/CUSIP/FIGI with check-digit validation and "did you mean"; tiers from the desk's own bands | ticket autocomplete, instrument card |
| 2. Live market | `services/market.py`, `sync.py`: Venue A availability and prints, live availability ledger (supply less approved locates) | **Market** (`4`): tier filter, fee flash, 5d sparkline, watchlist |
| 3. Locates | `services/locates.py`, `rules.locate_route`: AUTO / REVIEW / HOLD, ETB auto-approve policy, Reg SHO 203(b) basis, gateway replies, end-of-day expiry | **Locates** (`2`): inbox grouped by requester, `a` `p` `x` `⇧A`; actioned, release, export |
| 4. Trading | `services/orders.py`, `rules.pretrade_checks`, `rules.route_order`: limits, restricted, concentration, fat finger; GC → firm bid, specials → negotiated bid | **Trade** (`3`): ticket with live checks, `ctrl+↵`; **Blotter** (`1`): accept counter `a`, re-rate `r`, cancel `x` |
| 5. Booking | `services/booking.py`: CDM trade to the processor, compare, amend-to-fill for breaks | Blotter fills: book, fix break |
| 6. Approval gate | `gate.py`: propose → approve / edit / reject / take over, executors per kind, kill switch | **Agent** (`5`): cards, diff, transcript, `space` `e` `x` `t` |
| Paste anything | `ingest/paste.py`: sheet ranges, Bloomberg output, chat shorthand, per-cell confidence | **Paste** (`6`): preview, apply as locates / Venue B list / watchlist |
| Audit | `events.py`: append-only `events` table, pushed to the UI after commit | **Audit** (`7`) |

`⌘K` / `ctrl+K` opens the command palette: `locate 500k VOD LN for PB-ALPHA`, `borrow 100k AAPL @ 30 from HARBORAL`,
`show hot names`, or any name.

## Agent (off by default)

```
gather ─► decide ⇄ tools ─► check ─(low confidence / turned Hot / failed checks, once)─► escalate ─► decide
                              │
                           propose ─► await_trader (paused until the card is decided) ─► done
```

* `agent/graph.py`: the agent's tasks as LangGraph graphs. The first is the warm-locate review: Spark 1.3 decides,
  the rules engine checks the answer (`rules.locate_decision_checks`), and low confidence, a name that turned Hot or
  a failed check hands the case to Wonder once. Hot names start with Wonder. The model only reads and judges: the
  graph, not the model, turns a checked answer into a gate action. Counterparty text in any tool result is fenced in
  `<untrusted>` tags (`fence()`).
* `agent/runner.py`: runs graphs in the background. Each run is a LangGraph thread with an `agent_runs` row; state is
  checkpointed after every node (the desk's Postgres, or a sibling `*.agent.db` next to SQLite;
  `LOANDESK_AGENT_CHECKPOINT_URL` overrides). Approving or rejecting the card resumes the run; on start-up, runs a
  restart interrupted carry on, including ones whose card was decided while the desk was down.
* `agent/models.py`: the model gateway. `LOANDESK_AGENT_MODE=off|replay|record|live`, OpenAI SDK against
  `api.meta.ai/v1`, a cassette for record/replay (keys ignore timestamps and generated ids), a per-model spend meter
  and a hard budget.
* `agent/tools.py`: the spec's tool list over the same services the screens use.
* `uv sync --extra agent` installs LangGraph and the SDK; without it the desk runs and the agent endpoints say so.
  API: `POST /agent/locates/{id}/review` (202, runs in the background), `GET /agent/runs`, `GET /agent/runs/{id}`.

Not built yet: chat ingest (parser subagent on Desk Chat), RFQ quoting,
plain-language policies, Venue B negotiation, FIX transport, and logins for the desk itself (the `X-Trader`
header is a placeholder).

## Tests

```bash
cd platform/api && uv run pytest -q && uv run ruff check .
cd platform/web && npm run build
```
