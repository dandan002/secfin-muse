# LoanDesk AI: external-system mocks

The "market side" of LoanDesk AI: stateful simulators of every outside system the desk talks to.
Each mock is a separate system with its own Postgres database and ledger, like the external systems it
stands in for: the mocks share no database and talk only over HTTP, so the platform makes real network
calls and reconciliation can genuinely disagree. `mock-control` pushes one simulated clock to all of them,
and every mock derives its data from one seed, so a demo replays event for event.

> **Disclaimer.** Simulators for demonstration only. Not affiliated with or endorsed by EquiLend,
> Wematch, FIS, S3 Partners or Bloomberg; all names are trademarks of their respective owners. The instrument
> universe is real listed equities (names, tickers, FIGIs, shares outstanding, last closes and volumes
> from OpenFIGI and Yahoo Finance; see *Reference data*), but everything about lending is synthetic:
> fees, utilization, availability, short interest, prints and trades. CUSIPs, ISINs and SEDOLs are
> fictitious (the real ones are licensed), and counterparties and their LEIs are fictional. API
> shapes, the FIX dialect, and the CDM/ISO 20022 payloads are our own simplified designs, not the
> vendors' specs.

| Project | Stands in for | Label in UIs | Port | What it does |
| --- | --- | --- | --- | --- |
| `control` | — | Simulation Control | 8000 | Sim clock (live/stepped), seeded resets, fault fan-out, scenario runner |
| `ib` + Desk Chat | Instant Bloomberg | Desk Chat | 8010 (+5173 UI) | Rooms, Blast, @mentions, search, unread, typing; bot API; 6 scripted/LLM personas with gold labels |
| `gateway` | Counterparty "smart API" | Counterparty Gateway | 8020 | Locates/orders (CDM-shaped, flat, CSV), idempotency, rate limits, signed webhooks, load generator; the clients' trading at each close and the stock record |
| `ngt` | EquiLend NGT | Venue A (NGT-style) | 8030, FIX 9878 | Availability feed, prints, BID/OFFER/COUNTER/ACCEPT/CANCEL/FIRM_BID, ledger with UTIs, FIX 4.4-style session |
| `wematch` | Wematch.live | Venue B (Wematch-style) | 8040 | Interest lists (≤10k lines), optimiser, negotiation, re-rate/return lifecycle |
| `sfp` | FIS Securities Finance Processor | Processor (FIS-style) | 8050 | Booking (CDM-shaped), positions, compare with field diffs, amend, marks/margin calls, EOD file, ~3% seeded breaks |
| `custodian` | Custodian | Custodian | 8060 | ISO 20022 `sese.023` in (XSD-validated); `sese.024` status and `sese.025` confirmations out; seeded fails |
| `analytics` | S3 Partners / FIS Astec-style data vendor | Market Analytics (S3-style) | 8070 | Short interest, days to cover, utilization, fee, lendable supply, crowding and squeeze scores; history, screens, exchange-reported SI, daily file, watchlist alerts, OpenFIGI-shaped mapping |

Every service serves OpenAPI docs at `/docs`.

## Layout: separate projects

Each system is its own project, with its own `pyproject.toml`, `uv.lock`, Dockerfile, image, tests and
dependencies. Only ib pulls in `openai`, only custodian `lxml`, only control `pyyaml`. No system imports
another. They build on two small versioned libraries, the way vendors build on internal SDKs.

```
mocks/
  control/  ib/  gateway/  ngt/  wematch/  sfp/  custodian/  analytics/
                     one external system each: loandesk-mock-<name>, with its own pyproject, uv.lock,
                     Dockerfile, tests, API.md, openapi.json, and connector/ (its client package,
                     loandesk-connector-<name>)
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
uv run python run_all.py      # syncs each system's own environment, starts all eight; logs in harness/out/logs/
cd ../ib/desk-chat && npm ci && npm run dev   # Desk Chat on http://localhost:5173
```

One system on its own: `cd ngt && uv run uvicorn mock_ngt.app:app --port 8030`.

**AWS** (each mock its own stack: VPC, RDS, ECS, load balancer; Terraform): see
[`../infra/README.md`](../infra/README.md).

Then drive the demo (every API needs a key; see *Access: API keys, scopes and plans*):

```bash
KEY=$(curl -s localhost:8000/_admin/credentials | jq -r .operatorKey)
curl -XPOST localhost:8000/scenarios/demo/run -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"mode":"live","speed":10}'
cd mocks/harness && uv run python smoke.py   # optional: a stand-in "platform" plays the whole demo
```

Desk Chat asks for its access key once: open `http://localhost:5173/?key=<deskKey>` with the
`deskKey` from `curl localhost:8010/_admin/credentials`.

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
  mock and reseeds. IDs are derived from the seed; API keys survive resets (see *Access*).

Default sim start: **Tue 27 Oct 2026, 08:55 New York**.

### Scenarios

YAML files in `control/scenarios/` drive the mocks with timed `<service>.<verb>` steps (`GET <svc>/_admin/scenario`
lists a service's verbs):

* `demo.yaml`: the five-minute demo's inputs. Basalt's API sends 40 locates (34 GC/ETB + 6 Warm),
  Kestrel's API posts 25 more, there's a hot-name RFQ, IOIs, a PB-BRAVO SEDOL/ISIN list, an internal
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

## APIs, protocols and connectors

Each system documents its API in its own `API.md` and ships a **connector**: a standalone client
package in `<system>/connector/` that the LoanDesk platform installs, the way a vendor ships an SDK.
Connectors depend on nothing from the system they call. Where a standard exists they implement it
independently (Venue A's FIX engine uses [simplefix](https://pypi.org/project/simplefix/); the
custodian connector builds and parses ISO 20022 with the standard library), so the harness tests
also check each system against the standard rather than against its own code. Every system also
publishes its OpenAPI contract as a committed `openapi.json`, which a test keeps in sync.

| System | Protocols | Docs | Connector |
| --- | --- | --- | --- |
| Venue A (`ngt`) | **FIX 4.4** (NewOrderSingle, Cancel/Replace, Cancel, OrderStatusRequest, ExecutionReport, TradeCaptureReport with UTI, MarketData V/W/X/Y; resend and gap-fill); REST + WebSocket | [ngt/API.md](ngt/API.md) | `ngt_connector`: `FixSession`, `VenueAClient` |
| Venue B (`wematch`) | REST interest lists (JSON, CSV, multipart), negotiation, loan lifecycle; WebSocket | [wematch/API.md](wematch/API.md) | `wematch_connector`: `VenueBClient` |
| Processor (`sfp`) | **CDM**-shaped trade booking, compare with field diffs, amend; **ISO 20022 colr.003** margin calls; EOD CSV file; WebSocket | [sfp/API.md](sfp/API.md) | `sfp_connector`: `ProcessorClient`, `securities_lending_trade()` |
| Market Analytics (`analytics`) | REST snapshots, screens and history; daily CSV file; WebSocket alerts; **OpenFIGI v3**-shaped `/v1/mapping` | [analytics/API.md](analytics/API.md) | `analytics_connector`: `AnalyticsClient`, `FigiResolver` (cached OpenFIGI client) |
| Custodian | **ISO 20022** sese.023 and sese.020 in; sese.024, sese.025, sese.027 out (head.001 headers, XSD-validated) | [custodian/API.md](custodian/API.md) | `custodian_connector`: `CustodianClient`, ISO builders and parser |
| Counterparty Gateway | **CDM** Borrower Locate, flat JSON or CSV; idempotency keys, rate limits; HMAC-signed webhooks | [gateway/API.md](gateway/API.md) | `gateway_connector`: `GatewayPlatformClient`, `CounterpartyClient`, `webhooks.verify` |
| Desk Chat (`ib`) | Chat bot API (REST + WebSocket) with instrument chips | [ib/API.md](ib/API.md) | `ib_connector`: `DeskChatBot` |
| Simulation Control | Clock, resets, faults, scenarios | [control/API.md](control/API.md) | `control_connector`: `SimControl` |

## Access: API keys, scopes and plans

Every business API sits behind a gate (`mockcore/keys.py`), the way a vendor's does. `/health`, `/`
and `/docs` stay open; `/_admin` takes the separate `X-Admin-Token` (set `MOCK_ADMIN_TOKEN`; empty
means local dev, no check).

* **Keys** look like `mk_<service>_<keyId>_<secret>` and open only the service that issued them. A key
  goes in `Authorization: Bearer`, the service's own header (`X-Api-Key`, `X-Participant-Key`), or
  `?key=` / `?token=` on a WebSocket. Only a SHA-256 of the secret is stored, in the service's
  `api_keys` table, which survives `/_admin/reset`: keys are identity, not sim state.
* **Scopes** entitle a key to routes. A valid key without the scope gets a 403 that names the missing
  scope (`{"detail": {"error": "forbidden", "missingScopes": [...]}}`). WebSockets check the scope of
  each topic and close with 4401/4403.
* **Plans** cap each key: `trial` (1/s, 1,000 a day), `standard` (20/s, 100,000 a day), `partner`
  (200/s, no cap). Over the limit is a 429 with `Retry-After` and `X-RateLimit-*` headers. Limits run
  on wall time and are counted in memory, so a restart forgives the day's usage.
* **Bootstrap keys** are the platform's own logins, derived with HMAC from `MOCK_KEY_SECRET`, so they
  survive restarts and snapshot restores. `GET /_admin/credentials` shows them. Without the secret
  they're derived from the public seed, which is fine on a laptop and guessable anywhere else (the
  service logs a warning). In AWS the secret is an SSM SecureString (`/<prefix>/key-secret`).
* **Issued keys** are random and shown once: `POST /_admin/keys {"principal", "scopes", "plan",
  "label", "ttlDays"}`. `PATCH /_admin/keys/{id}` changes scopes or plan, `POST .../rotate
  {"graceSeconds"}` issues a successor and lets the old key run out, `POST .../revoke` ends it.
  `GET /_admin/keys` lists keys (never secrets) with usage. `PUT /_admin/plans/{name}` retunes a plan.
  `GET /whoami` tells a key holder who they are, what they may do and what's left of their quota.

| Service | Bootstrap keys (`/_admin/credentials`) | Scopes |
| --- | --- | --- |
| control | `operatorKey`, `observerKey` (read-only) | `sim:read`, `sim:operate` |
| ib | `botToken` (Bearer), `deskKey` (Desk Chat's `X-Api-Key`; `X-User` still picks who you act as) | `streams:read`, `messages:write`, `compose`, `chat:read`, `chat:write` |
| gateway | `platformToken`, one `apiKey` per external counterparty (plus `webhookSecret`) | `locates:write`, `locates:read`, `orders:write`; platform: `inbox:read`, `decisions:write`, `deliveries:read` |
| ngt | `participantKey`, `marketDataKey` (market data only); FIX password in tag 554 | `reference:read`, `market:read`, `orders:read`, `orders:write`, `trades:read` |
| wematch | `apiKey`, `readOnlyKey` | `reference:read`, `lists:read`, `lists:write`, `matching:run`, `matches:read`, `matches:negotiate`, `loans:read`, `loans:manage` |
| sfp | `apiKey`, `readOnlyKey` (operations) | `trades:write`, `contracts:read`, `contracts:write`, `reconcile`, `marks:read`, `marks:run`, `eod:read` |
| custodian | `apiKey` | `instructions:write`, `instructions:read`, `outbox:read` |
| analytics | `apiKey`, `readOnlyKey` (research) | `data:read`, `files:read`, `alerts:read`, `alerts:manage`; `/v1/mapping` is also open without a key (10 jobs per request) |

Onboarding a new counterparty on the gateway, say on a trial plan:

```bash
curl -XPOST localhost:8020/_admin/keys -H 'content-type: application/json' \
  -d '{"principal":"CEDARHF","scopes":["locates:write","locates:read"],"plan":"trial","label":"Cedar Hollow"}'
```

Credentials derive from the seed: every system serves `GET /_admin/credentials`. Set `MOCK_ADMIN_TOKEN`
to require an `X-Admin-Token` header on every `/_admin` route (and on WebSockets under `/_admin`).

**Desk Chat UI.** The React app (`ib/desk-chat`) defaults to an amber-on-black **terminal theme** modelled
on institutional terminal chat: a command line with `<GO>` (type a chat name, `MSG <handle>`, `SRCH <text>`,
`BLAST`, `THEME`, `HELP`), a function-key strip (F1 help, F2 chats, F3 command line, F8 Blast, F9 search,
F10 theme, Esc), dense one-line message rows, and amber title bars. It uses no vendor logo or name.
F10 (or `THEME`) cycles to the dark and light themes. Every theme has folders, tabs, unread and mention
badges, typing indicators, exchange-local timestamps, instrument chips with an instrument card, and an
"acting as" switcher so one browser can play any persona. `src/index.ts` exports `<DeskChat/>` for
embedding in the trader UI; a chip's "Open ticket" dispatches a `deskchat:ticket` window event.

**LLM personas (optional).** `MOCK_LLM_MODE=off|replay|record|live` (default `off`). `MOCK_LLM_MODEL`
defaults to `muse-spark-1.3` and `MOCK_LLM_BASE_URL` to `https://api.meta.ai/v1`. The key is read from
`META_AI_API` in the repo `.env` and never logged. `MOCK_LLM_BUDGET_USD` caps spend (default $0.50).
Responses are recorded to `ib/cassettes/personas.jsonl` for exact replays. The model only restyles a
scripted message, and the output is rejected unless every identifier survives, so gold labels stay true.
Switch a persona with the `ib.persona_mode` verb.

**Reference data.** The universe (`libs/refdata`) is about 1,900 real listed equities across the US,
UK, Germany, France, the Netherlands and Japan, plus 30 widely held US ETFs, read from the committed
snapshot `libs/refdata/refdata/data/public_universe.json.gz`. `refdata fetch` builds it from free
sources: the Yahoo Finance screener (via yfinance) picks the largest names per market and sector plus
an even spread of smaller ones, six months of daily closes give each name's realised volatility, and
the OpenFIGI mapping API supplies FIGIs and Bloomberg-style tickers. The mocks never call these
sources at runtime, so every service still builds the identical universe offline. Refresh it with
`cd libs/refdata && uv run --extra fetch refdata fetch` (set `OPENFIGI_API_KEY`, free, to map ten
times faster). Yahoo's data is for personal use under its terms; the snapshot keeps only rounded
aggregates and records its sources.

Lending is synthetic but calibrated on those facts. Within each market, names are ranked by a seeded
"specialness" score (smaller and more volatile ranks higher, ETFs lower), and the ranking is cut
70/20/10 into GC, Warm and Hot so every market has all three. Fees, utilization and short interest are
drawn inside each tier's band (GC under 50 bps and 30% utilization; Hot above 300 bps or 85%). Prices
start at each name's last close and move at its own volatility. CUSIPs, ISINs and SEDOLs are licensed,
so they're generated in fictitious ranges: CUSIP issuer `99xxxx` (reserved for internal use), SEDOLs
starting with `9` (user-defined). FIGIs are real. Instruments resolve by FIGI, composite FIGI, share-class
FIGI, ISIN, SEDOL, CUSIP, `TICK XX` (also `BRK.B US` for `BRK/B US`), `TICK XX Equity` or `TICK.MIC`.

`MOCK_UNIVERSE=synthetic` switches back to 2,000 fully fictional companies generated from the seed
(fictional names, `MKG`-prefixed FIGIs). `cd libs/refdata && uv run refdata export` writes the
universe and the 25 counterparties as JSON and CSV. Fees, utilization and prices are pure functions of
(seed, instrument, sim time): an OU process on log fee with occasional squeezes, and GBM prices.

**Keep real reference data fresh from open sources, and cache it.** The platform's security master should
be filled and refreshed from free sources wherever possible: [OpenFIGI](https://www.openfigi.com/api) for
identifier mapping (ISIN, CUSIP, SEDOL, ticker + exchange to FIGI, name and security type), GLEIF for LEIs,
and the published ISO MIC, currency and country lists. Call OpenFIGI whenever a new identifier shows up and
on each security-master refresh, but cache every answer (FIGIs are permanent: keep hits for about 30 days
and misses for a day), send only uncached look-ups, batch them (100 per request with a free key) and honour
`429 Retry-After`. `analytics_connector.FigiResolver` does exactly this, and `refdata fetch` already uses
OpenFIGI for the snapshot's FIGIs. The mocks' ISINs, CUSIPs and SEDOLs are fictitious, so OpenFIGI won't
know them: point the same resolver at the analytics mock's OpenFIGI-shaped `/v1/mapping` for those (it
returns the same real FIGIs). See [analytics/API.md](analytics/API.md#reference-data-use-open-sources-and-cache-them).

## Tests

Each project tests itself, in its own environment:

```bash
cd mocks
for p in libs/* control ib gateway ngt wematch sfp custodian analytics */connector; do (cd $p && uv run pytest -q); done   # unit tests; each system checks its app builds
(cd libs/mockcore && uv run mypy) && (cd libs/refdata && uv run mypy)
cd harness && uv run ruff check .. && uv run pytest               # cross-system tests; need Postgres
cd ../ib/desk-chat && npm run build
```

* **Systems** (28 tests): each checks its app builds with only its own dependencies and that its committed
  `openapi.json` matches the code (`uv run python -m mockcore.openapi mock_<name>.app` regenerates it);
  ib also tests its personas and LLM restyling, ngt its FIX codec, analytics its metrics.
* **Connectors** (17 tests): offline, against in-memory transports or a tiny in-process FIX acceptor: FIX
  session handling (logon, TestRequest, gaps, gap-fill, sequence persistence) and parsing, ISO 20022
  structure, CDM building, webhook signatures, CSV and idempotency handling, OpenFIGI caching and batching.
* **Libraries** (22 tests): clock, scheduler, faults, admin-token guard, API keys, scopes and plans, reference data.
* **Harness** (40 tests, 7 of them on the key gate): starts every system from its own project environment on ports 18000–18070, each
  against its own throwaway database (`loandesk_test_<service>`), and drives them only as a client,
  mostly through their connectors (`tests/test_connectors.py`, `tests/test_fix_session.py`). It covers
  every API end to end, FIX order flow, market data and offline recovery, ISO 20022 settlement and
  cancellation, CDM booking with break and amend, margin calls as colr.003, signed webhooks, the short-interest feed and alerts, the full
  demo (`harness/smoke.py`), stepped-run determinism, and database isolation. With
  `MOCK_TEST_DATABASE_ADMIN_URL` set (a plain `postgresql://` URL), an unreachable Postgres fails the run
  instead of skipping it.

## Limits (by design, for now)

* Scheduled work (venue responses, settlements, persona replies) lives in memory: a restarted service keeps
  its ledger but drops pending timers. Use `POST /reset` between runs.
* The FIX, CDM and ISO 20022 shapes are deliberate subsets. They're realistic enough to build adapters
  against, not certified implementations.
* No real auth on the Desk Chat human API; it's a demo identity switcher.
