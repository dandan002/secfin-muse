# Counterparty Gateway: locate and order API

> Simulator of the "smart API" a lending desk exposes to its prime-broker and fund clients. Locate
> requests follow a simplified **CDM Borrower Locate** shape; decision webhooks are HMAC-signed.
> API shapes are our own. All data is synthetic.

Counterparties send us **locate requests** (can you lend me these shares?) and **orders**. Our
platform pulls them from an **inbox** (or has them pushed), decides each line, and the gateway
returns the decision to the counterparty's callback as a **signed webhook**, retried with backoff.
A granted locate is a **quantity at an agreed fee**, with an **entry time** and an **expiry** (the
close, by default).

The gateway also plays the clients' side of the day: at each **close**, every locate expiring then
is shorted against by its client or not, and the **stock record** publishes the clients' short
positions settling the next business day: what the desk has to borrow to deliver.

| | |
| --- | --- |
| Base | `https://gateway.<domain>` (local `http://localhost:8020`); OpenAPI at `/docs`, snapshot in [`openapi.json`](openapi.json) |
| Counterparty auth | `Authorization: Bearer <counterparty API key>`; rate-limited per counterparty (default 20/s, burst 40) |
| Platform auth | `Authorization: Bearer <platform token>`; WebSocket `?token=` |
| Connector | [`connector/`](connector): `GatewayPlatformClient`, `CounterpartyClient`, `webhooks.verify` |

## Connector

```python
from gateway_connector import GatewayPlatformClient, CounterpartyClient, Decision, webhooks

# Our platform
async with GatewayPlatformClient("https://gateway.mocks.example.com", platform_token=tok) as gw:
    async for event in gw.stream():                       # or poll: await gw.inbox(after=last_seq)
        if event["type"] == "locate_received":
            req = event["data"]
            await gw.decide(req["locateId"], [Decision(lineNo=1, decision="APPROVED",
                                                       feeBps=30, regShoBasis="VENUE_A_AVAILABILITY")])
    record = await gw.stock_record()                      # after the close: client shorts settling tomorrow

# A counterparty (also how tests and load drive the gateway)
async with CounterpartyClient(url, api_key=pb_key) as cp:
    receipt = await cp.submit_locates([("US9939277482", 250_000), ("9ZZZZZ1", 75_000)],
                                      request_id="PB-0412", callback_url="https://pb.example/locates/cb")

# A counterparty's webhook receiver
ok = webhooks.verify(secret, raw_body, request.headers["X-Mock-Signature"])   # 5-minute replay window
```

## Counterparty API

| Method | Path | |
| --- | --- | --- |
| POST | `/v1/locates` | Submit one request of up to 5,000 lines: CDM JSON, flat JSON or `text/csv` |
| GET | `/v1/locates/{locateId}` | The request and every line's decision |
| GET | `/v1/locates?limit=` | Your recent requests |
| POST | `/v1/orders` | `{clientOrderId, side (BORROW/LOAN/RETURN/RECALL), identifier, quantity, feeBps?, term?, settlementDate?}` |

**Idempotency.** Send `Idempotency-Key` on submissions. The same key with the same body replays
the original response (`"idempotentReplay": true`); with a different body it gets a 409. The
connector generates a key per call and reuses it across its own retries.

**Rate limits.** Over the limit → `429` with `Retry-After` (seconds); the connector waits and retries.

### Locate formats

CDM-shaped:

```json
{"borrowerLocate": {
  "locateRequestId": "PB-0412", "settlementDate": "2026-10-28", "callbackUrl": "https://pb.example/locates/cb",
  "locateLines": [{"security": {"identifier": "US9939277482", "identifierType": "ISIN"},
                   "quantity": {"amount": 250000, "unit": "SHARES"}, "maxFee": {"bps": 450}}]}}
```

Flat: `{requestId, settlementDate, callbackUrl, lines: [{identifier, idType?, quantity, maxFeeBps?}]}`,
or a single line inline (`{identifier, quantity, ...}`).

CSV: header row with an identifier column (`identifier`, `isin`, `sedol`, `cusip`, `ticker` or
`security`), a quantity column (`quantity`, `qty`, `shares`) and optionally `maxFeeBps`; `requestId`,
`settlementDate` and `callbackUrl` go in the query string.

`maxFeeBps` (`maxFee.bps` in CDM) is the most the client will pay to short the name. It's optional;
a locate priced above it is one the client won't use.

Identifiers may be ISIN, CUSIP, SEDOL, FIGI or ticker. Each line is validated on receipt, with check
digits: bad lines come back in `invalidLines` [{lineNo, identifier, error}], with a "did you mean"
when one digit is off, and the rest proceed. Response (202):
`{locateId, requestId, status: RECEIVED, receivedAt, lines, invalidLines}`.

## Platform API

| Method | Path | |
| --- | --- | --- |
| GET | `/v1/inbox?status=RECEIVED&after=&limit=` | Requests awaiting decisions, each with `seq` (page with `after`) and `lines` |
| WS | `/v1/inbox/stream?token=` | Live `locate_received`, `order_received` (topic `inbox`) and `locate_decided` (topic `decisions`); events carry a per-topic gap-free `topicSeq` |
| POST | `/v1/decisions` | `{locateId, lines: [Decision]}` → `{status: DECIDED or PARTIALLY_DECIDED, lines}` |
| GET | `/v1/deliveries?locateId=` | Webhook attempts: `url`, `attempt`, `statusCode`, `ok`, `error` |
| GET | `/v1/stock-record?date=` | The stock record at a close (default the latest; 404 before the first) |

`Decision`: `lineNo`, `decision` (APPROVED / PARTIAL / REJECTED), `approvedQuantity` (0 with
APPROVED means the full quantity), `feeBps` (the agreed fee; `indicativeFeeBps` is still read),
`rationale`, `regShoBasis` (the Reg SHO 203(b) reasonable-grounds basis), `expiresAt` (default the
close, 16:00 New York, or the next business day's if decided after it). Invalid lines can't be
decided (409); approving more than requested is a 422. A decided line carries `feeBps`, `enteredAt`
(when it was granted) and `expiresAt`; after its close, `usedQuantity`.

## The clients' trading and the stock record

At each business day's close (16:00 New York; checked every minute of sim time, and caught up in
order if the gateway was down), every approved locate expiring by then is settled:

* **Used or not.** A client shorts against a locate with a set chance by kind (funds 70%, prime
  brokers 40%, broker-dealers 30%), for a quarter to all of it in round lots, at a time between its
  entry and expiry. A locate priced over the client's `maxFeeBps` is never used: it went elsewhere.
* **Buy-backs.** A quarter of existing short positions are partly or fully covered.
* **The record.** Each client's short position per name is written for that trade date; it settles
  the next business day. Draws are seeded per date, so a replay closes the same way.

`GET /v1/stock-record` (platform token; scope `inbox:read`):
`{tradeDate, settlementDate, closedAt, positions: [{counterparty, instrumentId, isin, shortQuantity,
soldQuantity, coveredQuantity}], shortSales: [{counterparty, instrumentId, quantity, locateId, lineNo,
executedAt}], locateUsage: [{locateId, lineNo, approvedQuantity, usedQuantity, feeBps, maxFeeBps}]}`.
A `stock_record` event goes out on the `inbox` topic when a close is written. A real prime broker
reads this from its own books and records; here the gateway, which already plays the clients, writes it.

**Push instead of pull.** With `MOCK_GATEWAY_DOWNSTREAM_URL` set, the gateway POSTs each request
to `<url>/locates` with header `X-Gateway-Token: <platform token>`, retrying up to 5 times.

## Decision webhooks

The decision goes to the request's `callbackUrl` as `POST`, `Content-Type: application/json`, body
`{locateId, requestId, counterparty, status, decidedAt, lines}`, with:

```
X-Mock-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(webhook secret, "<t>." + raw body)>
X-Mock-Delivery-Attempt: <n>
```

Verify against the **raw bytes** before parsing, and reject timestamps outside a tolerance window
to stop replays (`webhooks.verify` does both; default 300 s). Non-2xx responses are retried with
exponential backoff (2, 4, 8, 16 s of sim time; 5 attempts). Without a `callbackUrl`, the gateway's
built-in receiver for that counterparty records it (`GET /cp/{code}/received`).

## Errors

`{"detail": "..."}`: **401** bad key or token, **403** body names another counterparty,
**404** unknown locate, **409** idempotency-key reuse with a different body or deciding an invalid
line, **413** over 5,000 lines, **422** bad JSON or CSV, unknown security on an order, or bad
decision quantity, **429** rate limit.

## Simulator controls (`/_admin`, needs `X-Admin-Token`)

`GET /_admin/credentials` (platform token, each counterparty's API key and webhook secret);
`PUT /_admin/limits?rate_per_s=&burst=`; `GET|PUT /_admin/loadgen` (Poisson arrivals with a
09:00–09:45 burst; some lines on harder names carry a `maxFeeBps`); scenario verbs `gateway.loadgen`,
`gateway.burst` and `gateway.close_day {date?}` (run a close now, as if it had come).
