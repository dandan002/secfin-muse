# Venue A (NGT-style) — FIX 4.4-style session

> **This is our own stand-in, not the venue's protocol.** The real NGT messaging protocol is
> proprietary and licensed to clients. We use standard FIX 4.4 session and order messages and
> carry securities-lending economics in user-defined tags (20001+). Not affiliated with or
> endorsed by EquiLend. All data is synthetic.

## Session

| | |
| --- | --- |
| Transport | TCP, default port `9878` (`MOCK_NGT_FIX_PORT`) |
| BeginString | `FIX.4.4` |
| TargetCompID (venue) | `VENUEA` |
| SenderCompID (us) | `LOANDESK` |
| Password | tag 554 on Logon; get it from `GET /_admin/credentials` (derived from the sim seed) |
| Heartbeat | HeartBtInt (108) from the client's Logon; TestRequest after 1.2×, Logout after 2.5× of silence |
| Sequence numbers | Persisted per session across reconnects. `ResetSeqNumFlag(141)=Y` on Logon resets both sides to 1 |
| Gaps | Inbound gap → venue sends ResendRequest(2). Client ResendRequest → venue replays stored application messages with `PossDupFlag(43)=Y` and `OrigSendingTime(122)`, and gap-fills admin messages with SequenceReset(4) `GapFillFlag(123)=Y` |
| Offline | Execution reports raised while the client is logged off are stored with sequence numbers. The client sees the gap on its next Logon and recovers them with ResendRequest |
| Faults | `/_admin/faults` drop/dup/reorder apply to ExecutionReports; `outage` refuses Logon and logs out live sessions |

Session messages supported: Logon `A`, Heartbeat `0`, TestRequest `1`, ResendRequest `2`, Reject `3`,
SequenceReset `4` (reset and gap-fill), Logout `5`.

## Application messages

### Client → venue

| MsgType | Meaning | Key tags |
| --- | --- | --- |
| `D` NewOrderSingle | BID, OFFER or FIRM_BID | 11 ClOrdID, 48 SecurityID + 22 SecurityIDSource (4 ISIN, 2 SEDOL, 1 CUSIP) or 55 Symbol (`TICK XX`), 54 Side (1 borrow, 2 lend), 38 OrderQty, **20008 SLOrderType** (BID/OFFER/FIRM_BID; defaults from Side), **20001 FeeBps**, **20010 CounterpartyCode** (venue code; optional for BID = best lender), **20002 Term** (OPEN/TERM), **20005 TermEndDate** (YYYYMMDD), **20003 CollateralType** (CASH/NONCASH), 15 Currency (collateral), **20004 MarginPct**, 64 SettlDate, **20006 LegalAgreement** (MSLA/GMSLA_2010/GMSLA_2018), **20007 BookingEntity** |
| `G` OrderCancelReplaceRequest | Counter a lender's counter (new fee/qty) | 41 OrigClOrdID (or venue OrderID), 11 new ClOrdID, 20001 FeeBps, 38 OrderQty (optional) |
| `F` OrderCancelRequest | Cancel | 41 OrigClOrdID (or OrderID), 11 ClOrdID |
| `U1` SLAccept (user-defined) | Accept the lender's counter | 41 OrigClOrdID (or OrderID), 11 ClOrdID |

### Venue → client

| MsgType | When | Key tags |
| --- | --- | --- |
| `8` ExecutionReport | Every order state change | 37 OrderID, 11 ClOrdID, 17 ExecID, 150 ExecType, 39 OrdStatus, 48/22 security, 54, 38, 20001 FeeBps, 20008, 20010 contra code, 14/151/6 |
| | New | 150=`0`, 39=`0` |
| | Lender counter | 150=`D` (Restated), 39=`0`, 378=99, **20009 CounterFeeBps**, **20011 CounterQty**, 58 text |
| | Replaced (our counter accepted for review) | 150=`5` |
| | Executed | 150=`F`, 39=`2`, 32 LastQty, 31 LastPx (= fee bps), **20012 VenueTradeID** |
| | Trade confirm with UTI | 150=`I`, 39=`2`, 20012 VenueTradeID, **20013 UTI** (ISO 23897) |
| | Cancelled / Expired / Rejected | 150=`4` / `C` / `8`; rejects carry 103=99 and 58 `CODE: reason` |
| `9` OrderCancelReject | Bad cancel/replace | 41, 434 (1 cancel, 2 replace), 58 |
| `3` Reject | Unsupported MsgType | 45 RefSeqNum, 373, 58 |

## Order lifecycle

```
PENDING ──lender responds──▶ EXECUTED
   │  ▲                 └──▶ COUNTERED ──U1 accept──▶ EXECUTED
   │  └──── G counter ◀────────┘     └──G counter──▶ PENDING …
   ├──F──▶ CANCELLED      ├──ttl/16:30 NY──▶ EXPIRED      └──▶ REJECTED (no supply / fee too low / …)
```

Simulated lenders reply after 1–3 s (GC) or 5–20 s (specials) of sim time: execute if the bid
meets their ask, counter if within 80% of it (max 2 rounds), otherwise reject. FIRM_BID fills
immediately on GC names at or above 90% of the fair GC fee. Configure with `PUT /_admin/config`.

## Example

```
8=FIX.4.4|9=...|35=D|49=LOANDESK|56=VENUEA|34=2|52=20261027-13:31:02.000|11=LD-0001|48=US9939277482|22=4|
54=1|38=250000|20008=BID|20001=25|20010=4074|20002=OPEN|20003=CASH|15=USD|20004=102|64=20261028|
20006=MSLA|20007=LOANDESK-US|10=...|
```
