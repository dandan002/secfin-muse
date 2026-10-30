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

## Sign-in (Clerk)

The web app opens on a landing page; the desk is behind a Clerk sign-in, and the API checks every request itself.

1. Create an application in the [Clerk dashboard](https://dashboard.clerk.com). Turn off sign-up (or restrict it)
   so only invited traders get in.
2. `platform/web/.env.local` (gitignored): `VITE_CLERK_PUBLISHABLE_KEY=pk_test_...`
3. The API (`.env` or the environment): `LOANDESK_CLERK_ISSUER=https://<your-instance>.clerk.accounts.dev`
   (the Frontend API URL on the dashboard's API keys page). Every route but `/health` then needs the session token
   (`Authorization: Bearer`, `?token=` on the `/stream` WebSocket), checked against Clerk's JWKS for signature,
   expiry, issuer and the site it was minted for (`azp`, from `LOANDESK_CLERK_AUTHORIZED_PARTIES`, default the CORS
   origins). The trader in the audit trail is the token's `trader` or `username` claim (add one under
   *Sessions → Customize session token*, e.g. `{"trader": "{{user.username}}"}`), else the Clerk user id; the
   `X-Trader` header is ignored.

Without the key the landing page says sign-in isn't configured; `npm run dev` still offers "open the desk (dev,
no sign-in)", and without the issuer the API stays open, as before. Set both for anything beyond your laptop.

## What's built (spec module → code)

| Spec | Backend | Screen |
| --- | --- | --- |
| 1. Security master | `secmaster.py`: resolve ticker/ISIN/SEDOL/CUSIP/FIGI with check-digit validation and "did you mean"; tiers from the desk's own bands | ticket autocomplete, instrument card |
| 2. Live market | `services/market.py`, `sync.py`: Venue A availability and prints, live availability ledger (supply less approved locates); `services/inventory.py`: internal inventory from the processor's positions, netted across our books, less what lenders recalled and less the clients' shorts on the latest stock record (those shares went out on their sales; refreshed when a stock record or a CNS fail lands), into an `INTERNAL` source that locates cross against first (`GET /inventory`, Reg SHO basis `INTERNAL_INVENTORY:… (crossed n)`) | **Market** (`4`): tier filter, fee flash, 5d sparkline, watchlist |
| 3. Locates | `services/locates.py`, `rules.locate_filters`: threshold list (fails), restricted list, zero availability (fresh feed only) and price under $1 are declined by `policy:FILTER` before routing (each `LOANDESK_FILTER_*` can be `reject`, `hold` or `off`); `rules.locate_route`: AUTO / REVIEW / HOLD, ETB auto-approve policy, Reg SHO 203(b) basis, gateway replies. A granted locate is a quantity at an agreed fee (`feeBps`), entered at a time and good until the close (16:00 New York, or the next business day's if granted after it; a trader can set a later `expiresAt`). A client's `maxFeeBps` under our floor holds the line for the trader. After the close, the stock record marks each locate `USED` (with `qtyUsed`) or `EXPIRED` | **Locates** (`2`): inbox grouped by requester, `a` `p` `x` `⇧A`, fee against the client's limit, good-until; actioned with entry, fee, expiry and used, release, export |
| 3b. Short list | `services/shortlist.py`, `sync.sync_stock_record`: after each close the gateway's stock record lands: every client's short position settling the next business day. Per name, what to deliver is the clients' shorts; the box (net of every open borrow and loan on our books at the processor, plus fills booked since it was last read), less borrows failing to arrive, covers it; the rest is the shortfall. Open borrow orders, cover cards at the gate, borrow cards agreed in chat and chat bids not answered yet count as working. What's left gets a cover card (`LOANDESK_DRAFT_COVERS`): a borrow order the router places at the market fee plus `LOANDESK_COVER_MARKUP_PCT` (5%), or for a hot name a bid in chat to an agent lender we deal with ("Bid 120k NVDA US @ 35bps open?"), once; if the lender counters or passes, the next cover goes to a venue. A cover order still at the gate whose bid the market fee has moved through (a card left overnight) is superseded at the next draft and drafted again at today's fee, rather than rejected under the venue's floor once approved. The list also carries every name we're net borrowed in: a box over the shorts is **excess**, costing its fee every day, and the same draft makes a `lifecycle` RETURN card per borrow for the latest list, dearest fee first (booked borrows only, never a failing one); returns made since the box was read are counted off it, so the next draft doesn't return them again | **Shorts** (`9`): per name client short, box, fails, short by, working, to borrow, excess (and its cost a day); clients, covers and returns per name; draft covers and returns `d`, borrow by hand `b` |
| 4. Trading | `services/orders.py`, `rules.pretrade_checks`, `rules.route_order`: limits, restricted, concentration, fat finger, lending fee floor; GC → firm bid, specials → negotiated bid on Venue A. Venue B: a one-line interest list at the order's limit (`sync_venue_b` follows it; a dealer counter inside the limit is accepted by `policy:LIMIT`, one outside it comes back COUNTERED; accept / counter / cancel work as on Venue A). Venue A down: an AUTO order with no named counterparty fails over to a Venue B list and says so (`LOANDESK_FAILOVER_TO_VENUE_B`). A trade agreed in Desk Chat becomes one order card per line to that counterparty (`propose_from_chat`), with its pre-trade checks as risk flags; it's already agreed, so approving it books it directly as a `BILATERAL` trade (our own UTI, the counterparty's ref) rather than sending it to a venue. The rules read "done … ref" after our own bid as a borrow we agreed | **Trade** (`3`): ticket with venue choice and live checks, `ctrl+↵`; **Blotter** (`1`): accept counter `a`, re-rate `r`, cancel `x` |
| 5. Booking | `services/booking.py`: every fill from either venue books itself (`policy:AUTO-BOOK`, `LOANDESK_AUTO_BOOK`): CDM trade to the processor, compare, Rule 10c-1a fields captured on the trade; a break drafts an `amend` card aligning the processor to the fill; a matched contract instructs the custodian (`policy:AUTO-SETTLE`: SECB/SECL loan leg, cash collateral against it at the margin) and `sync_settlement` follows it. `services/lifecycle.py`: return (we borrowed) / recall (we lent), re-rate (Venue B asks the counterparty and waits; Venue A re-rates the contract), mark, and the daily mark run; returns instruct the reverse leg (SBRE/SLRE); `lifecycle` gate cards | **Blotter** fills & loans: book, fix break, settle, return / recall `⇧C`, re-rate `⇧R`, mark `⇧M`, run marks |
| 3c. Fails and Rule 204 | `services/fails.py`, `sync.sync_fails`: a client's short that failed to deliver at CNS comes from the gateway's books, per client and name. Reg SHO Rule 204(a) wants it closed out, by borrowing or buying, before regular trading opens the business day after it failed (09:30 New York). It's owed on the short list like the short itself (the name's need is never less than its open fails); the name sorts first and its cover card carries the deadline (a card drafted before the fail is superseded and redrafted). As far as the cover (borrows done, booked or not) reaches past the clients' other shorts, the fail is **closed out**; as far as shares already settled into our account reach, they're **delivered** against it (`policy:CNS-DELIVER`, `LOANDESK_AUTO_DELIVER_FAILS`; off, the trader delivers). Still open at the deadline, it's **missed** and Rule 204(b) makes the name **pre-borrow** until the shares deliver: its locates wait for the trader and can only be granted against shares we hold (the basis says `PRE_BORROW_RULE_204B`), never street supply | **Shorts**: failed at CNS panel (close-out deadline, time left, deliver), fail out per name, pre-borrow tag; **Locates**: pre-borrow tag |
| 5b. Recalls | `services/recalls.py`, `sync.sync_recalls`: a recall arrives three ways: at the processor for a Venue A fill or a bilateral trade (its contract there is the loan), on the Venue B loan, or in Desk Chat. A lender's recall in Desk Chat ("Recall notice: AAPL US 80k, ref HAR-4242, please return by T+2") is matched to our open borrows from that lender in that name, the one carrying their ref first (a chat borrow keeps it as the order's external ref), then the oldest. The shares go on the trade (`recalledQty`, due T+n, T+1 if it doesn't say) and stop counting as cover; a `lifecycle` RETURN card for them goes to the gate; excess returns already drafted in the name are superseded; and the short list redrafts, so a name the clients still need gets a cover. Processor and Venue B recalls name the contract or loan, so they go straight on it. Recalled shares come off our lendable supply (the `INTERNAL` availability locates cross against) the moment the recall lands, and stay off when the box is read again. Approving the return takes them off the loan, but the recall stands (**returning**) until the return delivery settles at the custodian; a failing return shows **return failing**. One not back by its due date shows overdue (the lender may buy the shares in at our cost). A recall on a borrow whose own delivery to us hasn't settled carries a warning: the return can't settle before the shares arrive. A chat recall's message is **actioned**; a recall we can't match (no borrow from them, or more than we have) stays open for the trader with a note | **Chat**: actioned recalls under agreed; **Blotter**: recalled and due on the loan, overdue in red; **Shorts**: recalled per name |
| 5c. Buy-ins | `services/buyins.py`, `sync.sync_buy_ins`: shares bought in against us. A lender's, on a recall still not returned a business day after it was due: from the processor for a Venue A fill or a bilateral trade, or on the Venue B loan (which we then book at the processor as a `BUY_IN`). The bought-in shares come off the loan and off what's recalled; the return card for the recall is superseded, and one for whatever is still recalled is drafted again ("after a buy-in"); we owe the lender's cost over the collateral it kept. The buyer's at CNS, on a client fail still open three business days on: the fail is **bought in**, the name leaves the penalty box, the shares cover the client's short (on the short list and in lendable supply before the next stock record shows it), and the cost over the sale price is charged to the client (Rule 204(d)). The cash goes through the gate: a lender's buy-in gets a `payment` card (approved, the custodian pays the lender what's due, or expects the lender to pay back what the collateral it kept came to over its cost, and the buy-in follows the payment to settled); a CNS buy-in's cost is taken by NSCC's money settlement with no instruction from us, and a `charge` card, approved, posts it to the client's account at the gateway. `GET /buy-ins` | **Shorts**: bought-in panel (from whom, price, amount due, cash and client charge, what's still to settle and to charge); **Blotter**: bought in on the loan |
| 6. Approval gate | `gate.py`: propose → approve / edit / reject / take over, executors per kind, kill switch | **Agent** (`5`): cards, diff, transcript, `space` `e` `x` `t` |
| Paste anything | `ingest/paste.py`: sheet ranges, Bloomberg output, chat shorthand, per-cell confidence | **Paste** (`6`): preview, apply as locates / Venue B list / watchlist |
| Chat ingest | `ingest/chat.py`, `sync.sync_chat`: every Desk Chat room the bot is in, both sides (a room seen for the first time starts at its end). Chat is where traders work borrows and loans, and it leans on context ("Any of these? META 24000…" / our "Maybe these META 0.15%" / "Send"), so Spark (`LOANDESK_PARSER_EFFORT` low) reads every message with the room's conversation over `LOANDESK_CHAT_CONTEXT_HOURS` (10): the 40 most recent messages plus any older request still open, each with its time. It returns the intent (RFQ, OFFER, IOI, QUOTE, COUNTER, ACCEPT, DECLINE, RECALL, RETURN, INTERNAL_BORROW), the lines worked out from context (fees as % a year → bps, GC), the message it answers and which side we'd be on. Theirs wait **open**; a trade either side accepted is **agreed**, to book; ours are kept as context. Locates belong to sales and the API (ignored in chat); injections are flagged by rules before any model; with the model off the rules read what they can and hand replies like "Send" to the trader. Nothing trades or replies on its own | **Chat** (`8`): needs you / open / agreed / all, both sides, the reading line by line, dismiss |
| Audit | `events.py`: append-only `events` table, pushed to the UI after commit | **Audit** (`7`) |

`⌘K` / `ctrl+K` opens the command palette: `locate 500k VOD LN for KESTRELHF`, `borrow 100k AAPL @ 30 from HARBORAL`,
`show hot names`, or any name.

## Pricing and counterparty limits

* **Pricing** (`rules.fee_floor`): we lend a bit above market. The floor is the market fee plus `LOANDESK_MARKUP_PCT`
  (default 5%, at least `LOANDESK_MARKUP_MIN_BPS`, default 2), never under our borrow cost when a locate crosses
  internal inventory; internal desks pay market. Locates are quoted at the floor at intake; decisions under it are
  refused unless a trader sets `overrideFeeFloor` (recorded in the rationale); LEND tickets fail a blocking
  `fee floor` check; the agent must price at or above it.
* **Counterparty limits** (`services/limits.py`): exposure is booked trades plus outstanding approved locates. Checks:
  credit limit (reference data), outstanding-locate limit (`LOANDESK_CP_LOCATE_LIMIT_PCT` of credit, default 50%),
  per-counterparty per-name cap (`LOANDESK_LOCATE_NAME_CAP`), and `LOANDESK_BLOCKED_COUNTERPARTIES` (declined by
  `policy:FILTER`). A breach holds the line for the trader; approvals can't exceed the room (`counterpartyRoom.maxQty`,
  also the cap on `suggestedQty`); internal desks have only the per-name cap.

## Agent (off by default)

```
gather ─► decide ⇄ tools ─► check ─(incomplete answer, once)─► revise ─► decide
                              │      └─(hard limit / low confidence / turned Hot / asked, once)─► escalate ─► decide
                              │
                           propose ─► await_trader (paused until the card is decided) ─► done
```

* `agent/graph.py`: the agent's tasks as LangGraph graphs. The first is the warm-locate review. Every review starts
  from the same market picture: availability (supply, what the desk already holds, lenders), utilization, a 20-day
  fee and utilization history, and the rules engine's supply signals (`rules.supply_signals`: thin cover, recall-level
  or rising utilization, rising or volatile fees, a thin lender base). The model must assess availability,
  utilization and fee history in its answer; an incomplete answer goes back to it once. The prompt treats listed
  supply as indicative and steers to partial or no fills when something is off, and gives the model latitude:
  `reasoning_effort` per role, more tool calls, a fee-history tool, and an `escalate` flag to ask for Wonder itself.
  Spark decides first; low confidence, a broken hard limit, a name that turned Hot or the reviewer's own request hands
  the case to Wonder once (Hot names start with Wonder). `rules.locate_decision_checks` blocks on hard limits
  (availability, concentration, fee band) and flags desk practice (full fill on a Hot name or into supply issues) on
  the card. The graph, not the model, turns a checked answer into a gate action. Counterparty text in any tool result
  is fenced in `<untrusted>` tags (`fence()`).
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
* System-one decision models (mammoth-1, elephant-1, Jev) were tried as a cheap first reviewer and not adopted:
  confident only on the cases the rules already make easy, and they cut by default. The write-up and results are
  in [`docs/system-one`](docs/system-one/README.md).

Not built yet: quoting RFQs from chat (they are read and wait; agreed trades become order cards), basket routing (a
whole basket to Venue B in one list; the Paste screen does it by hand; the short list drafts one order per name),
a recall driven by a corporate action,
recalling what we lent when a client's short needs it back, plain-language policies, FIX transport, and roles for the
desk (every signed-in trader can do everything).

## Tests

```bash
cd platform/api && uv run pytest -q && uv run ruff check .
cd platform/web && npm run build
```

Chat parser eval against mock-ib's labelled corpus: whole conversations, both sides labelled.

```bash
(cd mocks/ib && uv run mock-ib-corpus --out out/chat_corpus.jsonl --n 300)
(cd platform/api && uv run python -m loandesk.ingest.chat_eval ../../mocks/ib/out/chat_corpus.jsonl)   # rules only
(cd platform/api && LOANDESK_AGENT_MODE=record uv run python -m loandesk.ingest.chat_eval \
    ../../mocks/ib/out/chat_corpus.jsonl --model --conversations 40)                                    # Spark, in context
```

Spark in context, 40 conversations (81 labelled messages): 98.8% of intents right with no misses; every
acceptance ("Send", "Send HII, pass the rest", "Sure, send 500") with the right names, sizes and prices; quotes
with sizes carried from the RFQ; one quote handed to the trader. About $0.0034 a message on low reasoning. The
rules alone can't read replies (62% here) and hand them to the trader instead.
