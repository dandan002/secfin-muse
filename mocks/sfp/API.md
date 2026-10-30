# Processor (FIS-style): books-and-records API

> Simulator of a securities-finance processor, the system of record for loans (the role FIS
> Securities Finance Processor, formerly Global One, plays). API shapes are our own; trades use a
> simplified **FINOS CDM** shape and margin calls an **ISO 20022 colr.003** subset. Not affiliated
> with or endorsed by FIS. All data is synthetic.

We book every execution here and reconcile our own books against it. The processor holds
contracts, positions, lifecycle events (returns, recalls, re-rates), daily marks with margin calls,
and an end-of-day contracts file. A seeded ~3% of bookings land with one wrong field (fee, quantity
or settlement date), so reconciliation has real breaks to find.

| | |
| --- | --- |
| Base | `https://sfp.<domain>` (local `http://localhost:8050`); OpenAPI at `/docs`, snapshot in [`openapi.json`](openapi.json) |
| Auth | `X-Api-Key: <key>` (or `Authorization: Bearer`); WebSocket `?key=<key>`. Keys carry scopes and a rate-limit plan: see [Access](../README.md#access-api-keys-scopes-and-plans) |
| Connector | [`connector/`](connector): `ProcessorClient`, `securities_lending_trade()` CDM builder |

## Connector

```python
from sfp_connector import ProcessorClient, securities_lending_trade

trade = securities_lending_trade(
    client_ref=f"BOOK-{fill.tradeId}", venue="VENUE_A", venue_trade_id=fill.tradeId, uti=fill.uti,
    our_lei=OUR_LEI, counterparty_lei=fill.lender.lei, side="BORROW", isin=fill.isin, quantity=fill.quantity,
    fee_bps=fill.feeBps, collateral_type="CASH", collateral_ccy="USD", margin_pct=102,
    settlement_date=fill.settlementDate, trade_date=date(2026, 10, 27), legal_agreement="MSLA",
    booking_entity="LOANDESK-US")
async with ProcessorClient("https://sfp.mocks.example.com", api_key=key) as sfp:
    booking = await sfp.book(trade)                  # BOOKED (or DUPLICATE on a retried clientRef)
    result = await sfp.compare(trade)                # MATCHED, or UNMATCHED with field diffs + breakId
    for d in result.diffs:
        await sfp.amend(booking.contractId, {d.field: d.ours}, reason="match venue fill")
    calls = await sfp.margin_calls()                 # after the nightly mark
    xml = await sfp.margin_call_colr003(calls[0].callId)
```

## Booking

`POST /trades` books one trade, given either a **CDM-shaped** document (it has `economicTerms`)
or the **flat** form. `clientRef` is the idempotency key: re-sending it returns the existing
contract with `"status": "DUPLICATE"` instead of booking twice. Response (201):
`{contractId, status: BOOKED|DUPLICATE, contract}`.

### CDM trade

```json
{
  "clientRef": "BOOK-NGT-T-00000001",
  "venue": "VENUE_A",
  "tradeDate": "2026-10-27",
  "tradeIdentifier": [{"issuer": "VENUE_A", "assignedIdentifier": "NGT-T-00000001"},
                      {"identifierType": "UTI", "assignedIdentifier": "MOCK00VENUEANGT..."}],
  "party": [{"role": "LENDER", "partyId": "<lender LEI>"}, {"role": "BORROWER", "partyId": "<our LEI>"}],
  "economicTerms": {"securityLending": {
    "security": {"isin": "US9939277482"}, "quantity": 250000, "feeRate": {"bps": 25}, "price": 41.20,
    "collateral": {"type": "CASH", "currency": "USD", "marginPercentage": 102},
    "termination": {"type": "OPEN"}, "settlementDate": "2026-10-28"}},
  "legalAgreement": "MSLA",
  "bookingEntity": "LOANDESK-US"
}
```

Our side comes from the party roles: we're the borrower if the BORROWER `partyId` is our LEI.
`termination` is `{"type": "TERM", "endDate": ...}` for term loans. `price` defaults to the
current market price.

### Flat form

`clientRef?`, `venue?`, `venueRef?`, `uti?`, `side` (BORROW/LEND), `counterparty` (code, LEI or
venue code), `instrument` (any identifier), `quantity`, `feeBps`, `price?`, `collateralType`,
`collateralCcy`, `marginPct` (100–150), `term`, `termEndDate?`, `tradeDate?`, `settlementDate`,
`legalAgreement` (MSLA/GMSLA_2010/GMSLA_2018), `bookingEntity`.

## Contracts and positions

| Method | Path | |
| --- | --- | --- |
| GET | `/contracts?status=&instrument=&limit=` | Contracts with their `history` |
| GET | `/contracts/{ref}` | By contract id (`SFP-C-…`), UTI or venue reference |
| GET | `/positions?bookingEntity=` | Open quantity, contracts, average fee, price and loan value per instrument × side × entity |

A contract carries `contractId`, `uti`, `venue`, `venueRef`, `clientRef`, `status` (OPEN/CLOSED),
`side`, `counterparty` {code, name, lei}, `isin`, `quantity`, `openQuantity`, `feeBps`,
`priceAtTrade`, `price`, `loanValue`, collateral fields, `collateralValue`, term fields, dates,
`legalAgreement`, `bookingEntity`, `bookedAt` and `history`.

## Reconciliation

| Method | Path | |
| --- | --- | --- |
| POST | `/compare` | `{contractId?, trade}`: compare our view of a trade (flat or CDM) with the booking. Found by `contractId`, else the trade's UTI, venue ref or clientRef |
| GET | `/breaks?status=` | Open and resolved breaks |
| POST | `/contracts/{ref}/amend` | `{fields, reason}`: correct booked fields |

Compared fields: `isin`, `side`, `counterparty`, `quantity`, `feeBps`, `collateralType`,
`collateralCcy`, `marginPct`, `term`, `termEndDate`, `settlementDate`, `legalAgreement`,
`bookingEntity` (only those present in the trade you send). The result is `MATCHED`, or
`UNMATCHED` with `diffs` [{field, ours, processor}] and a `breakId` (`SFP-B-…`, one open break per
contract). Amendable fields: `feeBps`, `quantity`, `settlementDate`, `marginPct`, `collateralType`,
`collateralCcy`, `termEndDate`, `legalAgreement`, `bookingEntity`. An amendment that removes every
diff resolves the break (`resolvedBreaks` in the response).

## Lifecycle

`POST /lifecycle/{ref}` with `{"event": ..., ...}`:

| Event | Body | Effect |
| --- | --- | --- |
| `RETURN` | `quantity?` (default all), `effectiveDate?` | Borrower returns shares; closes at zero |
| `RECALL` | `quantity?`, `effectiveDate?` | We recall shares we lent |
| `BUY_IN` | `quantity?`, `price?` | A lender bought in shares on a loan whose lifecycle lives on a venue (Venue B): the contract closes for them |
| `RERATE` | `feeBps`, `effectiveDate?` | New fee |
| `MARK` | | Mark to market now; may produce a margin call |

### Recalls from lenders

A Venue A fill or a bilateral trade has no venue lifecycle: its contract here is the loan, so when the
lender wants shares back, the recall notice lands here. (A Venue B loan is recalled on the venue.)

| Method | Path | |
| --- | --- | --- |
| GET | `/recalls?status=&contractId=&after=` | `recallId` (`SFP-R-…`), `seq`, `contractId`, `counterparty`, `instrumentId`, `quantity`, `outstanding` (not returned yet), `noticeDate`, `dueDate`, `status` (OPEN/RETURNED/BOUGHT_IN), `overdue`, `boughtIn` |
| GET | `/buy-ins?contractId=&after=` | Buy-ins on our overdue recalls: `buyInId` (`SFP-B-…`), `seq`, `recallId`, `contractId`, `counterparty`, `instrumentId`, `quantity`, `price`, `ccy`, `cost`, `collateralKept`, `amountDue` |

A `RETURN` on the contract answers its open recalls, oldest first (`recallsAnswered` in the response).
Shares not back by the due date leave the notice `overdue`: under the lending agreement the lender may
then buy them in at our cost. Here it does, a business day after the due date, in market hours (verb
`buy_in_after {days}`; `null` never): it buys what's still outstanding at the market price plus 0.5-3%,
the contract closes for those shares, and the lender keeps their share of our collateral against the
cost. `amountDue` is what's left for us to pay (negative: the collateral was more, and the lender owes
us the difference). A `buy_in` event goes out on the `contracts` topic, and the contract's history
gets a `BUY_IN` entry.

## Marks and margin calls

The processor marks every open contract nightly (00:00 UTC) or on `POST /marks/run`. For each:
loan value = open quantity × price, required collateral = loan value × margin %, and the call
amount = required − posted. A call of at least 1 is a margin call: the lender calls when the loan
value rises, the borrower when it falls.

| Method | Path | |
| --- | --- | --- |
| POST | `/marks/run` | Mark everything now → `{marked, marginCalls, calls}` |
| GET | `/marks?contractId=` | Mark history |
| GET | `/margin-calls?since=` | `callId` (`MC-…`), `contractId`, `uti`, `markDate`, `direction` (WE_CALL / WE_ARE_CALLED), `amount`, `ccy`, `deliverOrReturn` (DLVR/RTRN), values |
| GET | `/margin-calls/{callId}/colr.003` | The call as ISO 20022 **colr.003** MarginCallRequest XML |

colr.003 subset (namespace `urn:iso:std:iso:20022:tech:xsd:colr.003.001.05`):
`Document/MrgnCallReq/` → `TxId`; `Oblgtn/{PtyA,PtyB}/Id/LEI` (PtyA is the calling party),
`Oblgtn/CollAcctId/Id`, `Oblgtn/ValtnDt/Dt`; `MrgnCallRslt/MrgnCallAmt/{Amt[@Ccy], DlvrRtrInd}`;
`Expsr/{ExpsrTp=SLEB, LnVal, ReqrdCollVal, PstdCollVal, CtrctRef}`.

## End-of-day file

`GET /eod/contracts.csv?asOf=YYYY-MM-DD`, as the processor's batch file with one row per contract:
`contract_id, uti, venue_ref, booking_entity, side, counterparty_lei, isin, open_quantity, fee_bps,
collateral_type, collateral_ccy, margin_pct, collateral_value, settlement_date, status`. The
simulator's `eod_break` flag makes one row disagree with the online books.

## Streaming

`WS /stream?key=<key>&topics=contracts,breaks,marks`. Events have `topic`, `type`, `topicSeq`
(per topic, gap-free: a gap means loss) and `data`.

| Topic | Types |
| --- | --- |
| `contracts` | `contract_booked`, `contract_amended`, `contract_return`, `contract_recall`, `contract_rerate`, `contract_mark`, `recall_notice`, `recall_returned`, `buy_in` |
| `breaks` | `break_opened`, `break_resolved` |
| `marks` | `margin_call` |

## Errors

FastAPI style: `{"detail": "..."}` with **401** bad key, **404** unknown contract or call, **409**
lifecycle on a closed contract, **422** unknown instrument or counterparty, missing `termEndDate`,
non-amendable field, or a return larger than the open quantity.

## Simulator controls (`/_admin`, needs `X-Admin-Token`)

`GET /_admin/credentials`; `GET|PUT /_admin/flags` (`break_rate`, `force_break_next`, `eod_break`);
`GET /_admin/injected-breaks` (the answer key); scenario verbs `sfp.flags`, `sfp.marks` and
`sfp.recall {contractId? | instrument?, counterparty?, quantity?, dueDays? (1)}` (a lender recalls one of our borrows),
`sfp.buy_in {recallId?, quantity?, price?}` (a lender buys one in now) and `sfp.buy_in_after {days}`.
