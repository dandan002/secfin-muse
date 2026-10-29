# Market Analytics (S3-style): short-interest and lending-market data API

> Simulator of a short-interest and securities-lending data vendor (the role S3 Partners, FIS
> Astec Analytics or S&P/IHS Markit Securities Finance play). API shapes are our own; the
> `/v1/mapping` endpoint copies OpenFIGI v3's request and response shape. Not affiliated with or
> endorsed by S3 Partners, FIS or OpenFIGI/Bloomberg. All data is synthetic.

The desk subscribes to a data vendor for the market's view of each name: how much is shorted, how
crowded the short is, what borrowing costs, and how much supply is left. The platform uses it to
fill the `si_pct`, `util_pct`, `tier` and `threshold_flag` columns of its `instruments` table,
to rank names for the trader, and to warn before a squeeze. Every number is a function of (seed,
instrument, sim time) built on the same fee, utilization and price processes the venues quote, so
a squeeze this vendor flags is the same squeeze Venue A and Venue B price.

| | |
| --- | --- |
| Base | `https://analytics.<domain>` (local `http://localhost:8070`); OpenAPI at `/docs`, snapshot in [`openapi.json`](openapi.json) |
| Auth | `X-Api-Key: <key>` (or `Authorization: Bearer`); WebSocket `?key=<key>`; `/v1/mapping` also works without a key (lower cap). Scopes: `data:read`, `files:read`, `alerts:read`, `alerts:manage`; see [Access](../README.md#access-api-keys-scopes-and-plans) |
| Connector | [`connector/`](connector): `AnalyticsClient`, plus `FigiResolver` (cached OpenFIGI client) |

## Connector

```python
from analytics_connector import AnalyticsClient, FigiJob, FigiResolver

async with AnalyticsClient("https://analytics.mocks.example.com", api_key=key) as data:
    rows = await data.daily_file()                     # overnight bulk load: one dict per instrument
    snap = await data.security("QZNT US")              # intraday refresh of one name
    crowded = await data.screen(country="US", sort="squeezeRisk", limit=20)
    await data.set_watchlist([s.isin for s in crowded.items])
    async for event in data.stream():                  # SQUEEZE / FEE_SPIKE / UTIL_HIGH / SI_JUMP
        ...

# Real identifiers: the public OpenFIGI service. The mocks' fictitious ISINs/CUSIPs/SEDOLs: this mock.
async with FigiResolver(api_key=os.environ.get("OPENFIGI_API_KEY")) as figi:
    hits = await figi.map([FigiJob("ID_ISIN", isin) for isin in new_isins])
```

## Per-name analytics

| Method | Path | |
| --- | --- | --- |
| GET | `/v1/securities/{ref}` | Latest snapshot plus `identifiers`; `ref` is any identifier (ISIN, SEDOL, CUSIP, FIGI, `TICK US`, `TICK.MIC`) |
| GET | `/v1/securities/{ref}/history?from=&to=` | Daily closes, oldest first; default the last 30 days, at most 366 |

A snapshot:

```json
{
  "isin": "US9912345678", "ticker": "QZNT", "composite": "QZNT US", "asOf": "2026-10-27T13:00:00.000Z",
  "price": 41.2, "ccy": "USD",
  "shortInterest": {"shares": 41250000, "pctSharesOut": 22.1, "pctFloat": 27.4, "notional": 1699500000,
                    "daysToCover": 6.8, "change1dPct": 1.2, "change7dPct": 4.9, "change30dPct": 11.0,
                    "source": "ESTIMATED"},
  "borrow": {"indicativeFeeBps": 1450.0, "utilizationPct": 96.3, "lendableShares": 44100000,
             "onLoanShares": 42470000, "availableShares": 1630000, "costToBorrowScore": 8, "tier": "HOT"},
  "crowdingScore": 78.5, "squeezeRisk": 61.2, "thresholdList": true
}
```

| Field | Meaning |
| --- | --- |
| `shortInterest.pctSharesOut`, `pctFloat` | Estimated short interest as % of shares outstanding and of free float |
| `shortInterest.daysToCover` | Short shares ÷ 20-day average daily volume |
| `shortInterest.change*Pct` | Change in short interest against the 21:00 UTC close 1, 7 and 30 days earlier |
| `borrow.indicativeFeeBps`, `utilizationPct`, `tier` | The market's fee and utilization now, and the tier they imply (GC/WARM/HOT, the spec's bands) |
| `borrow.lendableShares`, `onLoanShares`, `availableShares` | Supply: on loan is about the short interest; lendable = on loan ÷ utilization, capped at the float |
| `borrow.costToBorrowScore` | 1 (under 25 bps) to 10 (4,000 bps and up), on bands 25/50/100/200/300/500/1,000/2,000/4,000 |
| `crowdingScore` | 0–100: 40% short % of float (full at 40%), 30% days to cover (full at 10), 30% utilization |
| `squeezeRisk` | 0–100: crowding scaled by how far the fee has run above its mean (×0.5 at the mean, ×1 at 4×) |
| `thresholdList` | On the Reg SHO threshold list |

History points: `date`, `siShares`, `siPctSharesOut`, `daysToCover`, `feeBps`, `utilizationPct`, `price`.
The vendor's day closes at **21:00 UTC** (after the US close); `asOf` is the time the numbers are for.

## Screen

`GET /v1/short-interest?country=&sector=&tier=&minPctFloat=&minCrowding=&sort=&limit=&offset=` →
`{asOf, total, offset, items: [snapshot]}`, highest `sort` first. `sort` is one of `crowdingScore`
(default: the crowded-shorts list), `squeezeRisk`, `pctFloat`, `daysToCover`, `indicativeFeeBps`,
`utilizationPct`. `limit` up to 2,000 (the whole universe).

## Exchange-reported short interest (US)

`GET /v1/exchange-short-interest?settlementDate=&instrument=&limit=&offset=`: short positions as
the exchanges report them (FINRA-style) on two settlement dates a month, the 15th and the month
end (moved back to a business day), published **7 business days later**. Default: the latest
published date. Items: `isin`, `ticker`, `settlementDate`, `publishDate`, `currentShortPosition`,
`previousShortPosition`, `changePct`, `avgDailyVolume`, `daysToCover`, `source: EXCHANGE_REPORTED`.
The reported figure differs from the vendor's daily estimate for that day by a few per cent, as it
does in life. **404** before the publish date, **422** for a date that isn't on the cycle or a non-US name.

## Daily file

| Method | Path | |
| --- | --- | --- |
| GET | `/v1/files` | The last ten files, newest first: `asOf`, `path`, `rows`, `publishedAt` |
| GET | `/v1/files/{asOf}/short-interest.csv` | One row per instrument at that day's 21:00 UTC close |

The file for day D publishes at 22:00 UTC on D; before then (or while the `late_file` flag is on)
it is **404**. Columns: `as_of, isin, sedol, cusip, figi, ticker, mic, name, country, ccy, price,
shares_outstanding, free_float_pct, adv_20d, adv_90d, si_shares, si_pct_shares_out, si_pct_float,
days_to_cover, si_change_1d_pct, utilization_pct, indicative_fee_bps, cost_to_borrow_score, tier,
lendable_shares, on_loan_shares, crowding_score, squeeze_risk, threshold_list` (`Y`/`N`).

This is the overnight bulk load: it carries the identifiers, so the platform can match rows to its
own instruments by ISIN and cross-check SEDOL, CUSIP and FIGI.

## Watchlist and alerts

| Method | Path | |
| --- | --- | --- |
| GET | `/v1/watchlist` | The names being watched |
| PUT | `/v1/watchlist` | `{instruments: [...]}` (any identifiers, up to 500): replace the watchlist |
| DELETE | `/v1/watchlist/{ref}` | Stop watching one name |
| GET | `/v1/alerts?since=&kind=&limit=` | Alerts, oldest first |
| WS | `/v1/stream?key=<key>&topics=alerts` | Alerts as they are raised (`topic`, `type: alert`, `topicSeq`, `data`) |

A scan runs every 15 sim-minutes. Each kind fires at most once per name per day.

| Kind | Severity | Scope | Fires when |
| --- | --- | --- | --- |
| `SQUEEZE` | HIGH | whole universe | Fee at least 3× the name's mean fee (`squeeze_mult`) |
| `FEE_SPIKE` | MEDIUM | watchlist | Fee up 25% or more in 15 minutes |
| `UTIL_HIGH` | MEDIUM | watchlist | Utilization at or above 90% |
| `SI_JUMP` | MEDIUM | watchlist | Short interest up 10% or more on the day |

An alert: `alertId` (`AN-A-…`), `kind`, `severity`, `isin`, `composite`, `ts`, plus the numbers behind it.

## Identifier mapping (OpenFIGI-shaped)

`POST /v1/mapping` takes and returns exactly what [OpenFIGI](https://www.openfigi.com/api) v3's
`POST /v3/mapping` does, answered from the mock universe, so one client works against both:

```json
[{"idType": "ID_ISIN", "idValue": "US9912345678"}, {"idType": "TICKER", "idValue": "QZNT", "exchCode": "US"}]
→ [{"data": [{"figi": "MKG…", "name": "…", "ticker": "QZNT", "exchCode": "US", "compositeFIGI": "MKG…",
              "securityType": "Common Stock", "marketSector": "Equity", …}]},
   {"warning": "No identifier found."}]
```

Supported `idType`s: `ID_ISIN`, `ID_CUSIP`, `ID_SEDOL`, `ID_BB_GLOBAL`, `COMPOSITE_ID_BB_GLOBAL`,
`TICKER` (needs `exchCode` or `micCode`). Like OpenFIGI: 10 jobs per request without a key, 100
with one (`X-OPENFIGI-APIKEY` or `X-Api-Key`), **413** above that. Results line up with the jobs.

### Reference data: use open sources, and cache them

The platform should keep its reference data current from open sources wherever it can, rather
than depend on a paid vendor for identifiers:

* **OpenFIGI** for identifier mapping (ISIN, CUSIP, SEDOL, ticker + exchange → FIGI, name,
  security type). It is free; a free API key raises the limits. Call it whenever a new identifier
  appears (a chat, a locate file, a vendor file) and when refreshing the security master. FIGIs
  are permanent and mappings rarely change, so **cache every answer** (hits for about 30 days,
  misses for a day), send only uncached look-ups, batch them (100 jobs per request with a key)
  and honour `429 Retry-After`. `FigiResolver` does all of this; its cache is any mapping, so
  back it with Redis or Postgres in production.
* **GLEIF** (LEI look-ups), the ISO 10383 **MIC** list, **ISO 4217** currencies and **ISO 3166**
  countries for counterparties and code lists.
* **FINRA** bi-monthly short interest, **SEC** fails-to-deliver files and exchange **Reg SHO
  threshold lists** as free cross-checks on the vendor's numbers.

The mock universe is real listed names with their real FIGIs and tickers (fetched from OpenFIGI
by `refdata fetch`), but its ISINs, CUSIPs and SEDOLs are fictitious (the real ones are licensed;
see `../README.md`). So the public OpenFIGI answers FIGI and ticker look-ups for mock names but
not their ISINs, CUSIPs or SEDOLs. Point the same resolver at this mock (`<url>/v1`) for those; it
returns the same real FIGIs. Use `https://api.openfigi.com/v3` for everything real.

## Errors

FastAPI style: `{"detail": "..."}` with **401** bad key, **404** unknown instrument, unpublished
file or unpublished exchange date, **413** too many mapping jobs, **422** bad history range,
off-cycle settlement date, non-US exchange look-up, or unknown watchlist names.

## Simulator controls (`/_admin`, needs `X-Admin-Token`)

`GET /_admin/credentials` (`apiKey`: every scope on the partner plan; `readOnlyKey`: research, no watchlist changes); `GET|PUT /_admin/flags`: `stale_feed` (every snapshot and screen is
stuck at the previous day's close, so the platform has to notice an old `asOf`), `late_file`
(the latest daily file misses its slot) and `squeeze_mult` (the SQUEEZE threshold, default 3).
Scenario verbs: `analytics.flags`, `analytics.scan` (run the alert scan now) and
`analytics.alert` (`{instrument, kind?, severity?}`: raise an alert on demand for a demo).
