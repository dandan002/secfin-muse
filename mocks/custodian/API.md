# Custodian: ISO 20022 settlement API

> Simulator. Message names and element paths follow ISO 20022, but the custodian accepts a
> **subset** of each message (the XSDs in [`mock_custodian/xsd/`](mock_custodian/xsd) say exactly
> which elements); these are not the official schemas, which are free from iso20022.org. All data
> is synthetic.

The custodian holds our safekeeping account and settles the securities legs of loans and returns.
We send **settlement instructions** and **cancellation requests**; it answers with **status advices**
and **confirmations**, the same message flow a global custodian runs over SWIFTNet FileAct/InterAct.

| | |
| --- | --- |
| Base | `https://custodian.<domain>` (local `http://localhost:8060`); OpenAPI at `/docs`, snapshot in [`openapi.json`](openapi.json) |
| Auth | `X-Api-Key: <key>`; WebSocket `?key=<key>` |
| Connector | [`connector/`](connector): `CustodianClient`, `SettlementInstruction`, ISO builders and parser (standard library only) |
| Custodian BIC | `MKCUUS33XXX` |

## Connector

```python
from custodian_connector import CustodianClient, SettlementInstruction

leg = SettlementInstruction(tx_id="LD-SET-0001", movement="RECE", tx_type="SECB", isin="US9939277482",
                            quantity=250_000, trade_date=date(2026, 10, 27), settlement_date=date(2026, 10, 28),
                            counterparty_bic="MKHAUS33XXX", our_bic=our_bic, common_id=uti)
async with CustodianClient("https://custodian.mocks.example.com", api_key=key, our_bic=our_bic) as cust:
    ack = await cust.instruct(leg)              # sese.023 -> sese.024 PACK (ack.custodian_ref = CUS-…)
    async for advice in cust.stream():           # MACH, PENF (with reason), sese.025 SETT
        if advice.tx_id == leg.tx_id and advice.is_final: break
    await cust.cancel(leg.tx_id)                 # sese.020 -> sese.027 ACCP, then sese.024 CAND
```

`cust.outbox(after=last_id)` polls the same messages. Every advice parses to `StatusAdvice`
(`msg_type`, `status`, `tx_id`, `custodian_ref`, `reason`, `detail`, `settled_quantity`, `settled_at`, `xml`).

## Messages

| Direction | Message | Endpoint | Meaning |
| --- | --- | --- | --- |
| → custodian | `sese.023.001.09` SecuritiesSettlementTransactionInstruction | `POST /iso/sese.023` | Settle one leg |
| → custodian | `sese.020.001.07` SecuritiesTransactionCancellationRequest | `POST /iso/sese.020` | Cancel an instruction by its TxId |
| ← custodian | `sese.024.001.10` SecuritiesSettlementTransactionStatusAdvice | response + outbox | Acceptance, matching, pending/failing, cancelled |
| ← custodian | `sese.025.001.09` SecuritiesSettlementTransactionConfirmation | outbox | Settled |
| ← custodian | `sese.027.001.05` SecuritiesTransactionCancellationRequestStatusAdvice | response + outbox | Cancellation accepted or rejected |

Every message is a business message: `<BizMsg xmlns="urn:loandesk:mock:envelope">` holding a
`head.001.001.02` **AppHdr** (`Fr`/`To` BICFI, `BizMsgIdr`, `MsgDefIdr`, `CreDt`) and the `Document`.
Send `Content-Type: application/xml`.

### sese.023: settlement instruction

| Element (under `Document/SctiesSttlmTxInstr`) | |
| --- | --- |
| `TxId` | Your reference (AcctOwnrTxId), ≤ 35, unique (409 on reuse) |
| `SttlmTpAndAddtlParams/SctiesMvmntTp` | `RECE` receive / `DELI` deliver |
| `SttlmTpAndAddtlParams/Pmt` | `FREE` (FOP) / `APMT` (DVP, needs `SttlmAmt`) |
| `SttlmTpAndAddtlParams/CmonId` | Common reference shared with the counterparty: put the trade's UTI here |
| `TradDtls/TradDt/Dt/Dt`, `TradDtls/SttlmDt/Dt/Dt` | ISO dates |
| `FinInstrmId/ISIN` | ISIN |
| `QtyAndAcctDtls/SttlmQty/Qty/Unit`, `QtyAndAcctDtls/SfkpgAcct/Id` | Quantity; safekeeping account (ours: `LOANDESK-SAFE-01`) |
| `SttlmParams/SctiesTxTp/Cd` | `SECB` borrow, `SECL` loan, `SBRE` borrow return, `SLRE` loan return, `TRAD` |
| `DlvrgSttlmPties/Pty1/Id/AnyBIC`, `RcvgSttlmPties/Pty1/Id/AnyBIC` | Delivering and receiving parties |
| `SttlmAmt/Amt[@Ccy]`, `SttlmAmt/CdtDbtInd` | Against payment only |

Responses: **202** with a sese.024 `PACK` (header `X-Instruction-Id: CUS-…`); **400** with a sese.024
`RJCT` (reason `NARR`) when the XML fails the schema, listing the failures in `X-Validation-Errors`;
**422** with `RJCT` (reason `SAFE`) for an ISIN the custodian doesn't service; **409** for a duplicate TxId.

### sese.020: cancellation request

`Document/SctiesTxCxlReq/AcctOwnrTxId` (the sese.023's TxId), optionally `AcctSvcrTxId`
(`CUS-…`), `FinInstrmId/ISIN`, `SfkpgAcct/Id`.

Responses: **202** sese.027 `Accptd`, then a sese.024 `CAND` in the outbox; **409** sese.027 `Rjctd`
(reason `NARR`: already settled, cancelled or rejected); **404** sese.027 `Rjctd` for an unknown TxId;
**400** for a schema failure.

### Status codes

| Status | Message / element | Meaning |
| --- | --- | --- |
| `PACK` | sese.024 `AcptncSts/AccptdForFrtherPrcg` | Accepted |
| `RJCT` | sese.024 `AcptncSts/Rjctd` | Rejected (reason `NARR` schema, `SAFE` ISIN not serviced) |
| `MACH` | sese.024 `MtchgSts/Mtchd` | Matched with the counterparty's instruction |
| `NMAT` | sese.024 `MtchgSts/Umtchd` | Unmatched (reason `DDAT`: settlement dates differ) |
| `PENF` | sese.024 `SttlmSts/PdgFlng` | Pending, failing (reason `LACK` we lack securities, `CLAC` counterparty does); retried next business day |
| `SETT` | sese.025 `SctiesSttlmTxConf` | Settled: `SttldQty`, `FctvSttlmDt/Dt/DtTm` |
| `CAND` | sese.024 `CxlSts/Canc` | Cancelled |
| `ACCP` / `RJCT` | sese.027 `CxlReqSts/Accptd` / `Rjctd` | Cancellation request accepted / rejected |

Reasons sit in `…/Rsn/Cd/Cd` with free text in `AddtlRsnInf`; no reason is `NoSpcfdRsn=NORE`.

## Lifecycle

```
sese.023 ─▶ PACK ─(2–10 s)─▶ MACH ─(settlement date, 10:00 New York)─▶ SETT (sese.025)
   │          │                 └─▶ PENF (LACK/CLAC) ─next business day─▶ … up to 3 tries ─▶ SETT
   │          └─▶ NMAT (DDAT)
   └─▶ RJCT (schema / ISIN)          sese.020 at any point before settlement ─▶ sese.027 ACCP + CAND
```

## Receiving messages

| | |
| --- | --- |
| `GET /iso/outbox?after=<outId>&limit=` | Everything sent to you, oldest first: `outId`, `msgType`, `status`, `instructionId`, `ts`, `xml`. Store the last `outId` and poll after it |
| `WS /stream?key=` | The same messages live: events with `topic: "outbox"`, `data.xml`, `data.msgType`, `data.status`, `data.instruction` |

## JSON convenience routes

For systems without an ISO stack. The custodian builds the XML and runs the same flow.

| Method | Path | |
| --- | --- | --- |
| POST | `/instructions` | `{txId, movement, payment?, commonId?, txType, instrument, quantity, account?, tradeDate?, settlementDate, counterpartyBic, amount?, ccy?}` → `{instruction, sese023, ack}` |
| GET | `/instructions?status=` | All instructions with their `history` |
| GET | `/instructions/{ref}` | By TxId or `CUS-…` id |
| POST | `/instructions/{ref}/cancel` | Same as a sese.020 |

## Simulator controls (`/_admin`, needs `X-Admin-Token`)

`GET /_admin/credentials` (key, our BIC, custodian BIC); `GET|PUT /_admin/config` with `fail_rate`,
`unmatch_rate`, `max_attempts`, `fail_next`, `match_latency_s`, `settle_time_local`; scenario verb
`custodian.config`.
