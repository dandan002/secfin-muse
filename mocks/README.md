# LoanDesk AI: external-system mocks

The "market side" of LoanDesk AI: stateful simulators of every outside system the desk talks to.
Each mock is a separate system with its own Postgres database and ledger, like the external systems it
stands in for: the mocks share no database and talk only over HTTP, so the platform makes real network
calls and reconciliation can genuinely disagree. `mock-control` pushes one simulated clock to all of them,
and every mock derives its data from one seed, so a demo replays event for event.

> **Disclaimer.** Simulators for demonstration only. Not affiliated with or endorsed by EquiLend,
> Wematch, FIS or Bloomberg; all names are trademarks of their respective owners. All data is
> synthetic: instruments, identifiers, counterparties and LEIs are fictional. API shapes, the FIX
> dialect, and the CDM/ISO 20022 payloads are our own simplified designs, not the vendors' specs.

| Project | Stands in for | Label in UIs | Port | What it does |
| --- | --- | --- | --- | --- |
| `control` | — | Simulation Control | 8000 | Sim clock (live/stepped), seeded resets, fault fan-out, scenario runner |
| `ib` + Desk Chat | Instant Bloomberg | Desk Chat | 8010 (+5173 UI) | Rooms, Blast, @mentions, search, unread, typing; bot API; 6 scripted/LLM personas with gold labels |
| `gateway` | Counterparty "smart API" | Counterparty Gateway | 8020 | Locates/orders (CDM-shaped, flat, CSV), idempotency, rate limits, signed webhooks, load generator |
| `ngt` | EquiLend NGT | Venue A (NGT-style) | 8030, FIX 9878 | Availability feed, prints, BID/OFFER/COUNTER/ACCEPT/CANCEL/FIRM_BID, ledger with UTIs, FIX 4.4-style session |
| `wematch` | Wematch.live | Venue B (Wematch-style) | 8040 | Interest lists (≤10k lines), optimiser, negotiation, re-rate/return lifecycle |
| `sfp` | FIS Securities Finance Processor | Processor (FIS-style) | 8050 | Booking (CDM-shaped), positions, compare with field diffs, amend, marks/margin calls, EOD file, ~3% seeded breaks |
| `custodian` | Custodian | Custodian | 8060 | ISO 20022 `sese.023` in (XSD-validated); `sese.024` status and `sese.025` confirmations out; seeded fails |

Every service serves OpenAPI docs at `/docs`.

## Layout: separate projects

Each system is its own project, with its own `pyproject.toml`, `uv.lock`, Dockerfile, image, tests and
dependencies. Only ib pulls in `openai`, only custodian `lxml`, only control `pyyaml`. No system imports
another. They build on two small versioned libraries, the way vendors build on internal SDKs.

```
mocks/
  control/  ib/  gateway/  ngt/  wematch/  sfp/  custodian/
                     one external system each: loandesk-mock-<name>, with its own pyproject, uv.lock,
                     Dockerfile, tests and README-level docs (e.g. ngt/FIX_SPEC.md)
  ib/desk-chat/      mock-ib's terminal UI (its own npm project)
  libs/mockcore/     loandesk-mockcore: sim clock, scheduler, faults, event log, FastAPI factory
  libs/refdata/      loandesk-refdata: synthetic instruments, identifiers, counterparties, market
  harness/           runs every system from its own environment and plays the platform against them
  ruff.toml          shared house style
```

The libs are path dependencies today. Publishing them to a package index (e.g. CodeArtifact) is what lets
each project move to its own repository unchanged.

## Run it

**Docker** (each mock with its own Postgres container):

```bash
cd mocks
docker compose up --build
open http://localhost:5173        # Desk Chat
open http://localhost:8000/docs   # sim control
```

**Without Docker** (Python 3.13, [uv](https://docs.astral.sh/uv/), Node 22 and any Postgres 14+):

```bash
cd mocks/harness
# {service} becomes each mock's own database (created if missing); this is the default:
export MOCK_DATABASE_URL='postgresql+asyncpg://postgres@localhost:5432/loandesk_{service}'
uv run python run_all.py      # syncs each system's own environment, starts all seven; logs in harness/out/logs/
cd ../ib/desk-chat && npm ci && npm run dev   # Desk Chat on http://localhost:5173
```

One system on its own: `cd ngt && uv run uvicorn mock_ngt.app:app --port 8030`.

**AWS** (each mock its own stack: VPC, RDS, ECS, load balancer; Terraform): see
[`../infra/README.md`](../infra/README.md).

Then drive the demo:

```bash
curl -XPOST localhost:8000/scenarios/demo/run -H 'content-type: application/json' -d '{"mode":"live","speed":10}'
cd mocks/harness && uv run python smoke.py   # optional: a stand-in "platform" plays the whole demo
```

## Simulated time and determinism

`mock-control` owns the clock (a row in its own database) and pushes it to every mock with
`PUT /_admin/clock` whenever it changes, and again every 5 s (`MOCK_CLOCK_PUSH_S`). Each mock stores the
last clock it received in its own database, so a restarted mock resumes at once and catches up on the
next push. Pushes carry a revision, so a late one never overwrites a newer clock. The mocks never call
`mock-control`.

* **live**: sim time runs at `speed`× wall time (`PUT /clock {"speed": 10}`, `POST /clock/start|pause|jump`).
* **stepped**: time moves only on `POST /clock/advance {"seconds": 30}` (or `{"to": ...}`), which runs each
  service's due jobs in a fixed order. A stepped scenario run with the same seed produces identical event
  logs in every service (`harness/tests/test_scenario.py` checks this).
* Requests may carry `X-Sim-Time` to pin "now", which is how scenario steps get deterministic timestamps.
* `POST /reset {"seed": 20261027, "mode": "stepped", "epoch": "2026-10-27T08:55:00-04:00"}` truncates every
  mock and reseeds. IDs, API keys and passwords are derived from the seed.

Default sim start: **Tue 27 Oct 2026, 08:55 New York**.

### Scenarios

YAML files in `control/scenarios/` drive the mocks with timed `<service>.<verb>` steps (`GET <svc>/_admin/scenario`
lists a service's verbs):

* `demo.yaml`: the five-minute demo's inputs. PB-ALPHA blasts 40 locates in chat (34 GC/ETB + 6 Warm),
  the hedge fund's API posts 25 more, there's a hot-name RFQ, IOIs, a PB-BRAVO SEDOL/ISIN list, an internal
  borrow, and a prompt-injection attempt. The first booking breaks at the processor, and background API flow
  bursts from 09:00 to 09:45.
* `chaos.yaml`: Venue A outage then lossy execution reports, a custodian settlement fail, slow and lossy
  webhooks, and an end-of-day file break.

Instruments in scenarios use placeholders such as `@hot:2:US` (tier, index, country), which every
service resolves like an ISIN.

### Faults

`PUT <svc>/_admin/faults` (or `PUT /faults/{service}` on control) accepts `latency_ms`, `jitter_ms`,
`error_pct`, `outage`, and `drop_pct` / `dup_pct` / `reorder_pct`. The last three apply to outbound
messages: WebSocket events, FIX execution reports and webhooks. Fault decisions come from the seeded RNG.

## Credentials

Every service exposes `GET /_admin/credentials` (derived from the seed). Set `MOCK_ADMIN_TOKEN` to require an
`X-Admin-Token` header on all `/_admin` routes.

| Service | Auth |
| --- | --- |
| Desk Chat human API | `X-User: <handle>` (demo identity switcher; no real auth) |
| mock-ib bot API | `Authorization: Bearer <botToken>`; WebSocket `/streams/subscribe?token=` |
| mock-gateway | counterparties: `Authorization: Bearer <apiKey>`; platform: `Bearer <platformToken>` |
| mock-ngt | REST `X-Participant-Key`; FIX `SenderCompID=LOANDESK`, `TargetCompID=VENUEA`, password in tag 554 |
| mock-wematch, mock-sfp, mock-custodian | `X-Api-Key` |

## Service notes

**mock-ib / Desk Chat.** Human API under `/api` and `/ws?as=<handle>`. Bot API: `GET /streams`,
`GET /streams/{id}/messages?after=`, WebSocket `/streams/subscribe`, `POST /chats/{room}/messages`, and
`POST /compose` (pushes a draft into a human's composer). Personas are fictional: PB-ALPHA (terse), PB-BRAVO
(lists), Harbor Lane (IOIs), Kestrel (RFQs, negotiates against a hidden limit), Delta One (internal) and
Juniper (RFQs plus prompt injection). Every persona message stores a gold label (`GET /_admin/labels`).
`cd ib && uv run mock-ib-corpus` writes the 300-conversation labelled eval set.

The Desk Chat React app (`ib/desk-chat`) defaults to an amber-on-black **terminal theme** modelled on
institutional terminal chat: a command line with `<GO>` (type a chat name, `MSG <handle>`, `SRCH <text>`,
`BLAST`, `THEME`, `HELP`), a function-key strip (F1 help, F2 chats, F3 command line, F8 Blast, F9 search,
F10 theme, Esc), dense one-line message rows, and amber title bars. It uses no vendor logo or name.
F10 (or `THEME`) cycles to the dark and light themes. Every theme has folders, tabs, unread and mention
badges, typing indicators, exchange-local timestamps, instrument chips with an instrument card, and an
"acting as" switcher so one browser can play any persona. `src/index.ts` exports `<DeskChat/>` for embedding in the trader UI; a
chip's "Open ticket" dispatches a `deskchat:ticket` window event.

**LLM personas (optional).** `MOCK_LLM_MODE=off|replay|record|live` (default `off`). `MOCK_LLM_MODEL`
defaults to `muse-spark-1.3` and `MOCK_LLM_BASE_URL` to `https://api.meta.ai/v1`. The key is read from
`META_AI_API` in the repo `.env` and never logged. `MOCK_LLM_BUDGET_USD` caps spend (default $0.50).
Responses are recorded to `cassettes/personas.jsonl` for exact replays. The model only restyles a scripted
message, and the output is rejected unless every identifier survives, so gold labels stay true. Switch a
persona with the `ib.persona_mode` verb.

**mock-gateway.** `POST /v1/locates` accepts our CDM-Borrower-Locate-shaped JSON (`{"borrowerLocate": …}`),
the flat form from the spec, or `text/csv`, up to 5,000 lines. Bad check digits come back per line with a
"did you mean". The platform pulls from `GET /v1/inbox` or WebSocket `/v1/inbox/stream`; alternatively, set
`MOCK_GATEWAY_DOWNSTREAM_URL` to have locates pushed to it. The platform answers with `POST /v1/decisions`, and
the decision reaches the counterparty's callback as an HMAC-signed webhook
(`X-Mock-Signature: t=…,v1=…`), retried with backoff. Built-in receivers at `/cp/{code}/callback` record
what each counterparty got. The `gateway.loadgen` verb produces Poisson arrivals with a 09:00–09:45 burst.

**mock-ngt.** See [`ngt/FIX_SPEC.md`](ngt/FIX_SPEC.md) for the FIX
dialect: persisted sequence numbers, ResendRequest/gap-fill, and reports raised offline recovered on the
next logon. Simulated lenders execute, counter (partial size or higher fee), or reject after 1–3 s (GC) or
5–20 s (specials) of sim time. Venue codes and agreement types are checked
(`AGREEMENT_MISMATCH`). REST and FIX share one engine.

**mock-sfp.** `POST /trades` takes our simplified CDM securities-lending trade or a flat form. `POST /compare`
returns `MATCHED`/`UNMATCHED` with field diffs and opens a break; `POST /contracts/{id}/amend` resolves it.
The answer key is at `GET /_admin/injected-breaks`. The nightly mark (or `POST /marks/run`) recalculates
collateral and emits margin calls with a `colr.003`-shaped payload. `GET /eod/contracts.csv` is the
end-of-day file (the `eod_break` flag makes it disagree).

**mock-custodian.** `POST /iso/sese.023` takes XML validated against the XSD subsets in
`custodian/mock_custodian/xsd/` (head.001 + sese.023; a schema failure returns a `sese.024` RJCT naming the
field). `POST /instructions` takes JSON and builds the XML for you. Outbound messages go to `GET /iso/outbox`
and the `/stream` WebSocket.

**Reference data.** `cd libs/refdata && uv run refdata export` writes 2,000 instruments and 25 counterparties. Identifiers are
check-digit-correct but sit in fictitious ranges: CUSIP issuer `99xxxx` (reserved for internal use), SEDOLs
starting with `9` (user-defined), FIGI-shaped with a `MKG` prefix, and `MOCK…` LEIs. Fees, utilization and
prices are pure functions of (seed, instrument, sim time): an OU process on log fee with occasional
squeezes, and GBM prices. Tier bands follow the spec: GC under 50 bps and under 30% utilization; Hot above
300 bps or above 85%.

## Tests

Each project tests itself, in its own environment:

```bash
cd mocks
for p in libs/* control ib gateway ngt wematch sfp custodian; do (cd $p && uv run pytest -q); done   # 27 unit tests; each system checks its app builds
(cd libs/mockcore && uv run mypy) && (cd libs/refdata && uv run mypy)
cd harness && uv run ruff check .. && uv run pytest               # 23 cross-system tests; need Postgres
cd ../ib/desk-chat && npm run build
```

The harness tests start every system from its own project environment on ports 18000–18060, each against
its own throwaway database (`loandesk_test_<service>`), and check that no system's database holds another's
tables. They act only as a client: HTTP, Venue A's FIX client, and their own ISO 20022 message files. They
cover each API end to end, FIX recovery, the full demo (`harness/smoke.py`) and stepped-run determinism.
With `MOCK_TEST_DATABASE_ADMIN_URL` set (a plain `postgresql://` URL), an unreachable Postgres fails the
run instead of skipping it.

## Limits (by design, for now)

* Scheduled work (venue responses, settlements, persona replies) lives in memory: a restarted service keeps
  its ledger but drops pending timers. Use `POST /reset` between runs.
* The FIX, CDM and ISO 20022 shapes are deliberate subsets. They're realistic enough to build adapters
  against, not certified implementations.
* No real auth on the Desk Chat human API; it's a demo identity switcher.
