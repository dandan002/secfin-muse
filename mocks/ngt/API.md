# Venue A (NGT-style): participant API

> **Stand-in, not the venue's protocol.** EquiLend NGT's messaging protocol is proprietary and
> licensed to clients. This venue speaks standard FIX 4.4 session and order messages and carries
> securities-lending economics in user-defined tags (20001+); its REST API is our own design. Not
> affiliated with or endorsed by EquiLend. All data is synthetic.

Venue A is an electronic securities-lending venue. Lenders publish availability; borrowers bid
(name a lender or hit the best one), lenders execute, counter or reject, and every execution gets
a venue trade ID and a UTI. Participants connect over **FIX 4.4** (order flow, market data) or
**REST + WebSocket** (the same engine, for systems that don't run a FIX engine).

| | |
| --- | --- |
| REST base | `https://ngt.<domain>` (local `http://localhost:8030`); OpenAPI at `/docs`, snapshot in [`openapi.json`](openapi.json) |
| FIX | `fix.<domain>:9878` (local `localhost:9878`), plain TCP |
| Connector | [`connector/`](connector): `VenueAClient` (REST/WS) and `FixSession` (FIX initiator) |
| Sim time | All timestamps are simulated (see mock-control); FIX `SendingTime` uses wall time |

## Connector

```python
from ngt_connector import VenueAClient, FixSession, FileSeqStore, OrderRequest, ExecutionReport, TradeCaptureReport

order = OrderRequest(clientOrderId="LD-0001", type="BID", instrument="US9939277482", quantity=250_000,
                     feeBps=25, counterpartyCode="4074", collateralType="CASH", collateralCcy="USD",
                     marginPct=102, settlementDate="2026-10-28", legalAgreement="MSLA", bookingEntity="LOANDESK-US")

# FIX: production order flow
fix = FixSession("fix.mocks.example.com", 9878, password=pw, store=FileSeqStore("venue_a.seq"))
await fix.logon()                         # continues sequence numbers; recovers anything missed offline
await fix.new_order_single(order)
fill = await fix.wait_for(lambda m: isinstance(m, ExecutionReport) and m.cl_ord_id == "LD-0001"
                          and m.exec_type in ("TRADE", "RESTATED", "REJECTED"))
confirm = await fix.wait_for(lambda m: isinstance(m, TradeCaptureReport) and m.order_id == fill.order_id)
confirm.uti                               # ISO 23897 UTI
[book] = await fix.market_data(["US9939277482"], subscribe=True)   # W snapshot, then X incrementals

# REST: the same engine
async with VenueAClient("https://ngt.mocks.example.com", participant_key=key) as venue:
    avail = await venue.availability(instrument="US9939277482")
    o = await venue.place_order(order)
    async for event in venue.stream(["orders", "trades"]): ...
```

`FixSession` uses [simplefix](https://pypi.org/project/simplefix/) for framing, an implementation
independent of the venue's, and handles heartbeats, TestRequest, sequence gaps (ResendRequest) and
the venue's ResendRequests (gap-filled; stale orders are never re-sent).

## Credentials

| Channel | Credential | Header / tag |
| --- | --- | --- |
| REST, WebSocket | Participant key | `X-Participant-Key: <key>`; WebSocket `?key=<key>` (needed for `orders`/`trades` topics) |
| FIX | CompIDs + password | `SenderCompID(49)=LOANDESK`, `TargetCompID(56)=VENUEA`, `Password(554)` on Logon |

In the simulator, keys derive from the seed: `GET /_admin/credentials` (needs `X-Admin-Token`).
Our participant code on the venue is `0001`.

## Reference data and market data (REST)

| Method | Path | |
| --- | --- | --- |
| GET | `/v1/participants` | Participant code table: `code`, `name`, `lei`, `role` (LENDER/BORROWER), `agreement` |
| GET | `/v1/availability?instrument=&lender=&minQuantity=&limit=&offset=` | Aggregated lendable supply per instrument (`quantity`, `lenders`, `bestFeeBps`); with `instrument`, also `byLender` lines (`lenderCode`, `quantity`, `indicativeFeeBps`) |
| GET | `/v1/prints?instrument=&limit=` | Last prints (anonymous executions): `quantity`, `feeBps`, `ts` |

`instrument` accepts ISIN, CUSIP, SEDOL, FIGI or a composite ticker (`TICK US`).

## Orders (REST)

| Method | Path | Body / result |
| --- | --- | --- |
| POST | `/v1/orders` | `OrderRequest` → `Order` (201) |
| GET | `/v1/orders?status=&limit=` | Your orders, newest first |
| GET | `/v1/orders/{id}` | By venue `orderId` or your `clientOrderId` |
| POST | `/v1/orders/{id}/counter` | `{"feeBps", "quantity"?}`: answer a lender's counter → `Order` |
| POST | `/v1/orders/{id}/accept` | Take the lender's counter as it stands → `Trade` |
| POST | `/v1/orders/{id}/cancel` | → `Order` |
| GET | `/v1/trades?since=&limit=` | Executions |
| GET | `/v1/trades/{id}` | By `tradeId` or `uti` |

### OrderRequest

| Field | Type | |
| --- | --- | --- |
| `clientOrderId` | string ≤ 64 | Your ID; unique per participant (`DUPLICATE_CLORDID`) |
| `type` | `BID` \| `OFFER` \| `FIRM_BID` | BID: borrow from a lender. OFFER: lend. FIRM_BID: low-touch GC borrow, fills at once |
| `instrument` | string | ISIN, CUSIP, SEDOL, FIGI or `TICK XX` |
| `quantity` | int > 0 | Shares |
| `feeBps` | number ≥ 0 | Annualised borrow fee in basis points |
| `counterpartyCode` | string? | Venue code of the lender; omit to hit the best lender |
| `term`, `termEndDate` | `OPEN` \| `TERM`, date | `termEndDate` required for TERM |
| `collateralType`, `collateralCcy`, `marginPct` | `CASH`\|`NONCASH`, ISO 4217, 100–150 | e.g. 102 for US cash |
| `settlementDate` | date | Not in the past |
| `legalAgreement` | `MSLA` \| `GMSLA_2010` \| `GMSLA_2018` | Must match the lender's (`AGREEMENT_MISMATCH`) |
| `bookingEntity` | string | Your booking entity |
| `ttlSeconds` | int? | Default 900; unanswered orders expire, and all expire at 16:30 New York |

### Order lifecycle

```
PENDING ──lender executes──▶ EXECUTED ──▶ trade + UTI
   │  ▲                  └──▶ COUNTERED ──accept──▶ EXECUTED
   │  └──── counter ◀──────────┘     └──counter──▶ PENDING …
   ├──cancel──▶ CANCELLED     ├──ttl / 16:30 NY──▶ EXPIRED     └──▶ REJECTED
```

Simulated lenders answer after 1–3 s (GC) or 5–20 s (specials) of sim time: execute if the bid
meets their ask, counter (fee and/or partial size) if it's within 80% of it (max 2 rounds), else
reject. A FIRM_BID on a GC name at or above 90% of the fair GC fee fills immediately.

### Errors

REST errors are `{"error": CODE, "message": ...}`; FIX rejects carry `CODE: reason` in `58 Text`.

| Code | HTTP | When |
| --- | --- | --- |
| `UNKNOWN_INSTRUMENT` | 422 | Identifier doesn't resolve |
| `RESTRICTED` | 422 | Instrument not eligible on the venue |
| `UNKNOWN_COUNTERPARTY`, `NOT_A_LENDER` | 422 | Bad `counterpartyCode` |
| `AGREEMENT_MISMATCH` | 422 | Your agreement type isn't the lender's |
| `BAD_SETTLE_DATE` | 422 | Settlement date in the past |
| `DUPLICATE_CLORDID` | 409 | `clientOrderId` reused |
| `BAD_STATE` | 409 | Counter/accept/cancel not allowed in the order's status |
| `UNKNOWN_ORDER` | 404 | No such order |
| `NO_AVAILABILITY`, `FEE_TOO_LOW`, `FIRM_BID_GC_ONLY`, `BELOW_FLOOR` | (async) | Lender-side rejects: the order goes `REJECTED` with this `rejectReason` |

## Streaming (WebSocket)

`GET /v1/stream?topics=availability,prints,orders,trades&key=<participant key>`

The stream sends a snapshot (`availability_snapshot`), then `snapshot_end`, then live events.
Every event has `topic`, `type`, `seq` (one more than the last; a gap means loss, so re-read
`/v1/orders` and `/v1/trades`) and `data`.

| Topic | Types | `data` |
| --- | --- | --- |
| `availability` | `availability` | Instrument aggregate plus `byLender` lines |
| `prints` | `print` | One anonymous execution |
| `orders` | `order_new`, `order_resting`, `order_countered`, `order_replaced`, `order_executed`, `order_cancelled`, `order_expired`, `order_rejected` | `Order` |
| `trades` | `trade_executed` | `Trade` (with `uti`) |

## FIX 4.4 session

| | |
| --- | --- |
| Transport | TCP, port `9878` |
| BeginString | `FIX.4.4` |
| CompIDs | `SenderCompID(49)=LOANDESK`, `TargetCompID(56)=VENUEA` |
| Logon | `A` with `EncryptMethod(98)=0`, `HeartBtInt(108)`, `Password(554)`; `ResetSeqNumFlag(141)=Y` resets both sides to 1 |
| Heartbeat | Venue sends `TestRequest(1)` after 1.2× HeartBtInt of silence and `Logout` after 2.5× |
| Sequence numbers | Persisted per session across reconnects |
| Gaps | Inbound gap → venue sends `ResendRequest(2)`. Your `ResendRequest` → venue replays stored order and trade messages with `PossDupFlag(43)=Y` and `OrigSendingTime(122)`, and gap-fills the rest with `SequenceReset(4)` `GapFillFlag(123)=Y` |
| Offline | Execution reports and trade confirms raised while you're logged off are stored; your next Logon shows the gap and a ResendRequest recovers them |
| Market data | Not persisted or replayed (gap-filled on resend); re-request a snapshot after reconnecting |
| Faults | Simulator drop/dup/reorder faults apply to `8` and `AE`; `outage` refuses Logon and logs out live sessions |

Session messages: Logon `A`, Heartbeat `0`, TestRequest `1`, ResendRequest `2`, Reject `3`,
SequenceReset `4` (reset and gap-fill), Logout `5`.

### Client → venue

| MsgType | Meaning | Key tags |
| --- | --- | --- |
| `D` NewOrderSingle | BID, OFFER or FIRM_BID | 11 ClOrdID; 48 SecurityID + 22 SecurityIDSource (4 ISIN, 1 CUSIP, 2 SEDOL) or 55 Symbol; 54 Side (1 borrow, 2 lend); 38 OrderQty; 40 OrdType=2 + 44 Price (= fee bps); **20008 SLOrderType**; **20001 FeeBps**; **20010 CounterpartyCode**; **20002 Term**; **20005 TermEndDate**; **20003 CollateralType**; 15 Currency (collateral); **20004 MarginPct**; 64 SettlDate; **20006 LegalAgreement**; **20007 BookingEntity**; 60 TransactTime |
| `G` OrderCancelReplaceRequest | Counter a lender's counter | 41 OrigClOrdID (or OrderID), 11 new ClOrdID, 20001 FeeBps, 38 OrderQty (optional) |
| `F` OrderCancelRequest | Cancel | 41 OrigClOrdID (or OrderID), 11 ClOrdID |
| `U1` SLAccept (user-defined) | Accept the lender's counter | 41 OrigClOrdID (or OrderID), 11 ClOrdID |
| `H` OrderStatusRequest | Current state of an order | 37 OrderID or 11 ClOrdID, 790 OrdStatusReqID |
| `V` MarketDataRequest | Lendable supply | 262 MDReqID; 263 SubscriptionRequestType (0 snapshot, 1 subscribe, 2 unsubscribe); 264 MarketDepth=0; 267/269 MDEntryType=1 (Offer); 146 NoRelatedSym with 48/22 or 55 per instrument |

### Venue → client

| MsgType | When | Key tags |
| --- | --- | --- |
| `8` ExecutionReport | Every order state change | 37 OrderID, 11 ClOrdID, 17 ExecID, 150 ExecType, 39 OrdStatus, 55/48/22, 54, 38, 20001, 20008, 20010 contra, 14/151/6, 60 |
| | New | 150=`0`, 39=`0` |
| | Lender counter | 150=`D` (Restated), 39=`0`, 378=99, **20009 CounterFeeBps**, **20011 CounterQty**, 58 |
| | Our counter accepted for review | 150=`5` (Replaced) |
| | Executed | 150=`F` (Trade), 39=`2`, 32 LastQty, 31 LastPx (= fee bps), **20012 VenueTradeID** |
| | Order status reply | 150=`I` (Order Status), 39 = current, 790 OrdStatusReqID; unknown order → 39=`8`, 103=`5` |
| | Cancelled / Expired / Rejected | 150=`4` / `C` / `8`; rejects carry 103=99 and 58 `CODE: reason` |
| `AE` TradeCaptureReport | After every execution: the trade confirmation | 571 TradeReportID, 487=0, 856=0, 570=N, 55/48/22, 32 LastQty, 31 LastPx, 75 TradeDate, 60, 64 SettlDate, 15, 20001–20007 economics, **20012 VenueTradeID**, **20013 UTI**; side group 552=1 { 54, 37, 11, parties 453=1 { 448 contra code, 447=D, 452=17 ContraFirm } } |
| `W` MarketDataSnapshotFullRefresh | Reply to `V` (one per instrument) | 262, 55/48/22, 268 NoMDEntries { 269=1, 270 fee bps, 271 lendable qty, 20010 lender } |
| `X` MarketDataIncrementalRefresh | Supply changed (subscribed) | 262, 268 { 279 MDUpdateAction (1 change, 2 delete), 269=1, 55/48/22, 270, 271, 20010 } |
| `Y` MarketDataRequestReject | Unknown instrument / empty snapshot request | 262, 281=0, 58 |
| `9` OrderCancelReject | Bad cancel/replace | 41, 434 (1 cancel, 2 replace), 102=99, 58 |
| `3` Reject | Unsupported MsgType or failed accept | 45 RefSeqNum, 373, 58 |

FIX 4.4 has no UTI field (FIX 5.0 SP2 adds `RegulatoryTradeID(1903)`), so the UTI rides in 20013.

### Example

```
8=FIX.4.4|9=...|35=D|49=LOANDESK|56=VENUEA|34=2|52=20261027-13:31:02.000|11=LD-0001|48=US9939277482|22=4|
54=1|38=250000|40=2|44=25|20008=BID|20001=25|20010=4074|20002=OPEN|20003=CASH|15=USD|20004=102|
64=20261028|20006=MSLA|20007=LOANDESK-US|10=...|

8=FIX.4.4|9=...|35=AE|49=VENUEA|56=LOANDESK|34=7|...|571=NGT-T-00000001|487=0|856=0|570=N|55=ABC US|
48=US9939277482|22=4|32=250000|31=25|75=20261027|64=20261028|...|20012=NGT-T-00000001|
20013=MOCK00VENUEANGT...|552=1|54=1|37=NGT-O-00000001|11=LD-0001|453=1|448=4074|447=D|452=17|10=...|
```

## Simulator controls (`/_admin`, needs `X-Admin-Token`)

`GET /_admin/credentials`, `GET|PUT /_admin/config` (lender latency, spreads, counter band, TTL,
feed cadence), the common `faults`, `reset`, `tick`, `events`, `clock` routes, and scenario verbs
`ngt.supply {instrument, lender, quantity}` and `ngt.config {...}`.
