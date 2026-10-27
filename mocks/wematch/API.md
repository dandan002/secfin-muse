# Venue B (Wematch-style): list-matching API

> Simulator of a regulated list-matching venue for securities borrowing and lending, TRS and equity
> repo ("one list, one API"). API shapes are our own. Not affiliated with or endorsed by Wematch.
> All data is synthetic.

Members upload **interest lists** (what they want to borrow or lend, with fee limits). An
**optimiser** matches lists against every other member's, proposes **matches**, and each side
accepts, counters or rejects. Agreed matches become **loans** with a lifecycle (re-rate, return,
recall).

| | |
| --- | --- |
| Base | `https://wematch.<domain>` (local `http://localhost:8040`); OpenAPI at `/docs`, snapshot in [`openapi.json`](openapi.json) |
| Auth | `X-Api-Key: <key>`; WebSocket `?key=<key>` |
| Connector | [`connector/`](connector): `VenueBClient`, `ListLine` |
| Our entity | `LOANDESK` (see `GET /v1/entities`) |

## Connector

```python
from wematch_connector import VenueBClient, ListLine

async with VenueBClient("https://wematch.mocks.example.com", api_key=key) as wm:
    lst = await wm.upload_list([ListLine(instrument="US9939277482", side="BORROW", quantity=500_000, feeBps=40)],
                               client_ref="borrow-list-2026-10-27")
    await wm.run_optimiser(lst.listId)
    for m in await wm.matches(list_id=lst.listId, status="PROPOSED"):
        await wm.accept(m.matchId)             # or wm.counter(m.matchId, fee_bps=36)
    loans = await wm.loans(status="OPEN")
```

## Interest lists

| Method | Path | |
| --- | --- | --- |
| POST | `/v1/interest-lists` | Upload as JSON, `text/csv`, or multipart `file` |
| GET | `/v1/interest-lists` | Your lists |
| GET | `/v1/interest-lists/{listId}` | The list with `lineItems` (each with `matchedQuantity` and `status`) |
| DELETE | `/v1/interest-lists/{listId}` | Withdraw |

**JSON**: `{name, product (SBL|TRS|REPO), clientRef?, autoAccept?, lines: [ListLine]}`, up to
10,000 lines. `ListLine`: `instrument` (any identifier), `side` (BORROW/LEND), `quantity`,
`feeBps` (BORROW: the most we pay; LEND: the least we take), `minQuantity`, `term`,
`collateralType`, `collateralCcy`. `autoAccept` accepts the optimiser's proposals on our side
automatically.

**CSV**: a header row and one line per row. Headers are matched loosely: `instrument` (or isin,
sedol, cusip, ticker, security, identifier, figi), `side` (direction, b/l), `quantity` (qty,
shares, size), `fee_bps` (fee, rate, bps, limit), `min_qty`, `term`, `collateral`, `ccy`. List
metadata goes in the query string: `?name=&product=&clientRef=`.

The response is the list plus `errors` [{lineNo, error, input}] for rejected lines, which are
counted in `rejectedLines` while the rest are accepted. Re-sending a `clientRef` returns the
original list with `"duplicate": true`.

## Matching and negotiation

| Method | Path | |
| --- | --- | --- |
| POST | `/v1/optimiser/run` | `{listId?}`: match now (it also runs every 5 minutes of sim time) |
| GET | `/v1/matches?status=&listId=` | Proposed and past matches |
| GET | `/v1/matches/{matchId}` | One match, with `history` |
| POST | `/v1/matches/{matchId}/propose` · `/counter` | `{feeBps, quantity?}`: new terms (quantity may only go down) |
| POST | `/v1/matches/{matchId}/accept` | Our acceptance |
| POST | `/v1/matches/{matchId}/reject` | |

A match is `PROPOSED` until both sides accept (`ourAccept`, `counterpartyAccept`), then
`AGREED`, and a loan books. Simulated dealers answer in 3–15 s of sim time: accept, counter
(the match stays PROPOSED with new terms, and `rounds` counts up to 3), or reject. Proposals
expire after 30 minutes (`EXPIRED`).

## Loans

| Method | Path | |
| --- | --- | --- |
| GET | `/v1/loans?status=` | `loanId`, `matchId`, `isin`, `side`, `counterparty`, `quantityOriginal`, `quantityOpen`, `feeBps`, `pendingRerateBps`, `notional`, `notionalUsd`, dates, `status` (OPEN/RETURNED), `history` |
| POST | `/v1/loans/{loanId}/rerate` | `{feeBps}`: request a new fee; the counterparty answers later (history `RERATED`) |
| POST | `/v1/loans/{loanId}/return` · `/recall` | `{quantity?}` (default all) |
| GET | `/v1/stats` | Open loans, ongoing notional (USD), match counts by status |

## Streaming

`WS /v1/stream?key=<key>&topics=lists,matches,loans,optimiser`. Events have `topic`, `type`,
`topicSeq` (per topic, gap-free) and `data`:

| Topic | Types |
| --- | --- |
| `lists` | `list_uploaded`, `list_withdrawn` |
| `matches` | `match_proposed`, `match_countered`, `match_updated`, `match_agreed`, `match_rejected`, `match_expired` |
| `loans` | `loan_booked`, `loan_rerate_requested`, and return/recall/re-rate updates |
| `optimiser` | `optimiser_run` (when it proposed something) |

## Errors

`{"error": CODE, "message": ...}`:

| Code | HTTP | |
| --- | --- | --- |
| `TOO_MANY_LINES` | 422 | Over 10,000 lines |
| `EMPTY_FILE`, `BAD_HEADER` | 422 | CSV without a usable header |
| `UNKNOWN_MATCH`, `UNKNOWN_LOAN` | 404 | |
| `BAD_STATE` | 409 | Acting on a match that isn't PROPOSED, or a loan that isn't OPEN |
| `QTY_TOO_LARGE` | 422 | Raising quantity in negotiation, or returning more than is open |

## Simulator controls (`/_admin`, needs `X-Admin-Token`)

`GET /_admin/credentials`; `GET /_admin/dealer-lines?side=&limit=` (the other members' lists);
`GET|PUT /_admin/config` (`dealer_latency_s`, `dealer_accept_p`, `max_rounds`, `proposal_ttl_s`,
`optimiser_every_s`); scenario verbs under `wematch.*`.
