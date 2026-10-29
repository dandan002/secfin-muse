"""mock-analytics: "Market Analytics (S3-style)", a short-interest and lending-market data vendor.

Per-name snapshots (short interest, days to cover, utilization, indicative fee, lendable and
on-loan supply, cost-to-borrow score, crowding and squeeze risk), daily history, a universe
screen, FINRA-style exchange-reported short interest on the bi-monthly cycle, a daily CSV file
for bulk loads, a watchlist with alerts (REST and WebSocket), and an OpenFIGI-shaped mapping
endpoint for the synthetic identifiers. Reference: ../API.md.

Numbers come from the same deterministic fee, utilization and price processes the venues use
(``refdata.market``), so the vendor's view agrees with the market's. ``stale_feed`` and
``late_file`` simulate a feed that stops updating and a daily file that misses its slot. API
shapes are our own.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, Header, HTTPException, Query, WebSocket
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from mockcore import clock, rng
from mockcore.app import ServiceContext, ServiceSpec, create_app
from mockcore.auth import derive_secret
from refdata import market
from refdata.counterparties import OUR_FIRM
from refdata.universe import Instrument, instrument_by_id, resolve, universe

from . import metrics
from .models import Alert, Base, Watch

spec = ServiceSpec(
    name="mock-analytics",
    label="Market Analytics (S3-style)",
    schema="analytics",
    base=Base,
    description="Simulated short-interest and securities-lending data vendor: snapshots, history, screens, "
                "exchange-reported short interest, daily file, watchlist alerts, OpenFIGI-shaped mapping.",
)
ctx = ServiceContext(spec)
app = create_app(ctx)

_flags: dict[str, Any] = {"stale_feed": False, "late_file": False, "squeeze_mult": 3.0}
FILE_DELAY = timedelta(hours=1)  # the daily file lands an hour after the data cut
HISTORY_MAX_DAYS = 366


def api_key() -> str:
    return derive_secret(rng.get_seed(), "analytics-api", OUR_FIRM["code"])[:32]


async def client(x_api_key: str | None = Header(default=None)) -> str:
    if x_api_key != api_key():
        raise HTTPException(401, "missing or bad X-Api-Key")
    return OUR_FIRM["code"]


def as_of() -> datetime:
    """Now, or the previous day's close when the feed is (simulated) stale."""
    now = clock.now()
    if _flags["stale_feed"]:
        return metrics.close_ts(metrics.data_date(now) - timedelta(days=1))
    return now


def _inst(ref: str) -> Instrument:
    inst = resolve(ref)
    if inst is None:
        raise HTTPException(404, f"unknown instrument {ref!r}")
    return inst


def identifiers(inst: Instrument) -> dict[str, Any]:
    return {"isin": inst.isin, "sedol": inst.sedol, "cusip": inst.cusip, "figi": inst.figi, "ticker": inst.ticker,
            "mic": inst.mic, "exchCode": inst.exch_code, "composite": inst.composite, "name": inst.name,
            "fisn": inst.fisn, "cfi": inst.cfi, "country": inst.country, "ccy": inst.ccy, "sector": inst.sector,
            "sharesOutstanding": inst.shs_out, "freeFloatPct": inst.free_float_pct, "adv20d": inst.adv_20d,
            "adv90d": inst.adv_90d}


def snap(inst: Instrument, ts: datetime) -> dict[str, Any]:
    s = metrics.snapshot(inst, ts)
    s["asOf"] = clock.iso(ts)
    return s


# -- per name -------------------------------------------------------------------------------------
@app.get("/v1/securities/{ref}", tags=["short interest"])
async def security(ref: str, who: str = Depends(client)) -> dict:
    """Latest analytics for one name; ``ref`` is any identifier (ISIN, SEDOL, CUSIP, FIGI, "TICK US")."""
    inst = _inst(ref)
    return {"identifiers": identifiers(inst), **snap(inst, as_of())}


@app.get("/v1/securities/{ref}/history", tags=["short interest"])
async def history(ref: str, start: date | None = Query(None, alias="from"), end: date | None = Query(None, alias="to"),
                  who: str = Depends(client)) -> dict:
    """Daily closes (21:00 UTC data cut), oldest first. Defaults to the last 30 days; at most 366."""
    inst = _inst(ref)
    last = metrics.data_date(as_of())
    end = min(end or last, last)
    start = start or end - timedelta(days=30)
    if start > end:
        raise HTTPException(422, "from is after to")
    if (end - start).days > HISTORY_MAX_DAYS:
        raise HTTPException(422, f"at most {HISTORY_MAX_DAYS} days per request")
    series = []
    d = start
    while d <= end:
        ts = metrics.close_ts(d)
        si = metrics.si_pct(inst, ts)
        shares = int(inst.shs_out * si / 100)
        series.append({"date": d.isoformat(), "siShares": shares, "siPctSharesOut": round(si, 2),
                       "daysToCover": round(shares / max(inst.adv_20d, 1), 2), "feeBps": market.fee_bps(inst, ts),
                       "utilizationPct": market.util_pct(inst, ts), "price": market.price(inst, ts)})
        d += timedelta(days=1)
    return {"isin": inst.isin, "composite": inst.composite, "series": series}


# -- screen ---------------------------------------------------------------------------------------
SortKey = Literal["crowdingScore", "squeezeRisk", "pctFloat", "daysToCover", "indicativeFeeBps", "utilizationPct"]


def _sort_value(s: dict[str, Any], key: str) -> float:
    if key in ("pctFloat", "daysToCover"):
        return s["shortInterest"][key]
    if key in ("indicativeFeeBps", "utilizationPct"):
        return s["borrow"][key]
    return s[key]


@app.get("/v1/short-interest", tags=["short interest"])
async def screen(country: str | None = None, sector: str | None = None,
                 tier: Literal["GC", "WARM", "HOT"] | None = None, minPctFloat: float = 0, minCrowding: float = 0,
                 sort: SortKey = "crowdingScore", limit: int = Query(100, ge=1, le=2000), offset: int = Query(0, ge=0),
                 who: str = Depends(client)) -> dict:
    """Screen the whole universe, highest ``sort`` first (the crowded-shorts list by default)."""
    ts = as_of()
    rows = []
    for inst in universe():
        if (country and inst.country != country.upper()) or (sector and inst.sector != sector):
            continue
        s = snap(inst, ts)
        if tier and s["borrow"]["tier"] != tier:
            continue
        if s["shortInterest"]["pctFloat"] < minPctFloat or s["crowdingScore"] < minCrowding:
            continue
        rows.append(s)
    rows.sort(key=lambda s: (-_sort_value(s, sort), s["isin"]))
    return {"asOf": clock.iso(ts), "total": len(rows), "offset": offset, "items": rows[offset:offset + limit]}


# -- exchange-reported ----------------------------------------------------------------------------
@app.get("/v1/exchange-short-interest", tags=["exchange-reported"])
async def exchange_short_interest(settlementDate: date | None = None, instrument: str | None = None,
                                  limit: int = Query(500, ge=1, le=2000), offset: int = Query(0, ge=0),
                                  who: str = Depends(client)) -> dict:
    """US short positions on the mid-month and month-end settlement dates, published 7 business days later."""
    today = metrics.data_date(as_of())
    settle = settlementDate or metrics.latest_published(today)
    if settle not in metrics.settlement_dates(settle):
        raise HTTPException(422, f"{settle} is not a reporting settlement date (mid-month or month-end)")
    if metrics.publish_date(settle) > today:
        raise HTTPException(404, f"short interest for {settle} publishes on {metrics.publish_date(settle)}")
    if instrument:
        inst = _inst(instrument)
        if inst.country != "US":
            raise HTTPException(422, "exchange-reported short interest covers US listings only")
        names = [inst]
    else:
        names = [i for i in universe() if i.country == "US"]
    items = [metrics.exchange_report(i, settle) for i in names]
    return {"settlementDate": settle.isoformat(), "publishDate": metrics.publish_date(settle).isoformat(),
            "total": len(items), "offset": offset, "items": items[offset:offset + limit]}


# -- daily file -----------------------------------------------------------------------------------
FILE_COLUMNS = ("as_of", "isin", "sedol", "cusip", "figi", "ticker", "mic", "name", "country", "ccy", "price",
                "shares_outstanding", "free_float_pct", "adv_20d", "adv_90d", "si_shares", "si_pct_shares_out",
                "si_pct_float", "days_to_cover", "si_change_1d_pct", "utilization_pct", "indicative_fee_bps",
                "cost_to_borrow_score", "tier", "lendable_shares", "on_loan_shares", "crowding_score",
                "squeeze_risk", "threshold_list")


def published_dates(now: datetime, n: int = 10) -> list[date]:
    last = metrics.data_date(now - FILE_DELAY)
    if _flags["late_file"]:
        last -= timedelta(days=1)
    return [last - timedelta(days=k) for k in range(n)]


@app.get("/v1/files", tags=["files"])
async def files(who: str = Depends(client)) -> list[dict]:
    """The last ten daily files, newest first."""
    return [{"asOf": d.isoformat(), "path": f"/v1/files/{d.isoformat()}/short-interest.csv", "rows": len(universe()),
             "publishedAt": clock.iso(metrics.close_ts(d) + FILE_DELAY)} for d in published_dates(clock.now())]


@app.get("/v1/files/{as_of_date}/short-interest.csv", tags=["files"], response_class=PlainTextResponse)
async def daily_file(as_of_date: date, who: str = Depends(client)) -> str:
    """One row per instrument at the day's 21:00 UTC data cut: what the platform bulk-loads overnight."""
    if as_of_date > published_dates(clock.now(), 1)[0]:
        raise HTTPException(404, f"file for {as_of_date} not yet published")
    ts = metrics.close_ts(as_of_date)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(FILE_COLUMNS)
    for inst in universe():
        s = metrics.snapshot(inst, ts)
        si, b = s["shortInterest"], s["borrow"]
        w.writerow((as_of_date.isoformat(), inst.isin, inst.sedol, inst.cusip or "", inst.figi, inst.ticker,
                    inst.mic, inst.name, inst.country, inst.ccy, s["price"], inst.shs_out, inst.free_float_pct,
                    inst.adv_20d, inst.adv_90d, si["shares"], si["pctSharesOut"], si["pctFloat"],
                    si["daysToCover"], si["change1dPct"], b["utilizationPct"], b["indicativeFeeBps"],
                    b["costToBorrowScore"], b["tier"], b["lendableShares"], b["onLoanShares"], s["crowdingScore"],
                    s["squeezeRisk"], "Y" if s["thresholdList"] else "N"))
    return buf.getvalue()


# -- watchlist and alerts --------------------------------------------------------------------------
class WatchlistIn(BaseModel):
    instruments: list[str] = Field(..., max_length=500, description="Any identifiers; replaces the watchlist")


async def _watchlist(s) -> list[dict[str, Any]]:
    rows = (await s.execute(select(Watch).order_by(Watch.instrument_id))).scalars()
    return [{"isin": w.isin, "composite": instrument_by_id(w.instrument_id).composite,
             "addedAt": clock.iso(w.added_at)} for w in rows]


@app.get("/v1/watchlist", tags=["alerts"])
async def get_watchlist(who: str = Depends(client)) -> list[dict]:
    async with ctx.session() as s:
        return await _watchlist(s)


@app.put("/v1/watchlist", tags=["alerts"])
async def put_watchlist(body: WatchlistIn, who: str = Depends(client)) -> dict:
    insts, unknown = {}, []
    for ref in body.instruments:
        inst = resolve(ref)
        if inst is None:
            unknown.append(ref)
        else:
            insts[inst.id] = inst
    if unknown:
        raise HTTPException(422, f"unknown instruments {unknown}")
    async with ctx.session() as s:
        keep = {w.instrument_id: w for w in (await s.execute(select(Watch))).scalars()}
        await s.execute(delete(Watch).where(Watch.instrument_id.not_in(list(insts))))
        for inst in insts.values():
            if inst.id not in keep:
                s.add(Watch(instrument_id=inst.id, isin=inst.isin, added_at=clock.now()))
        await s.commit()
        return {"count": len(insts), "items": await _watchlist(s)}


@app.delete("/v1/watchlist/{ref}", tags=["alerts"], status_code=204)
async def unwatch(ref: str, who: str = Depends(client)) -> None:
    inst = _inst(ref)
    async with ctx.session() as s:
        await s.execute(delete(Watch).where(Watch.instrument_id == inst.id))
        await s.commit()


def _alert_json(a: Alert) -> dict[str, Any]:
    inst = instrument_by_id(a.instrument_id)
    return {"alertId": a.alert_id, "kind": a.kind, "severity": a.severity, "isin": a.isin,
            "composite": inst.composite, "ts": clock.iso(a.ts), **a.data}


@app.get("/v1/alerts", tags=["alerts"])
async def alerts(since: datetime | None = None, kind: str | None = None, limit: int = Query(500, le=5000),
                 who: str = Depends(client)) -> list[dict]:
    async with ctx.session() as s:
        q = select(Alert).order_by(Alert.id).limit(limit)
        if since:
            q = q.where(Alert.ts >= since)
        if kind:
            q = q.where(Alert.kind == kind.upper())
        return [_alert_json(a) for a in (await s.execute(q)).scalars()]


async def _raise(s, inst: Instrument, kind: str, severity: str, data: dict[str, Any], seen: set[str]) -> None:
    key = f"{kind}:{inst.id}:{clock.now().date().isoformat()}"
    if key in seen:
        return
    seen.add(key)
    a = Alert(alert_id="pending", dedupe_key=key, kind=kind, severity=severity, instrument_id=inst.id,
              isin=inst.isin, data=data, ts=clock.now())
    s.add(a)
    await s.flush()
    a.alert_id = f"AN-A-{a.id:08d}"
    await ctx.events.emit(s, "alert", "alert", a.alert_id, _alert_json(a), topic="alerts")


async def scan() -> dict[str, Any]:
    """Squeeze alerts across the universe; fee spike, high utilization and SI jumps on the watchlist.
    Each kind fires at most once per name per day."""
    now = clock.now()
    async with ctx.session() as s:
        seen = set((await s.execute(select(Alert.dedupe_key).where(
            Alert.dedupe_key.like(f"%:{now.date().isoformat()}")))).scalars())
        before = len(seen)
        for inst in universe():
            fee = market.fee_bps(inst, now)
            if fee >= _flags["squeeze_mult"] * inst.base_fee_bps:
                await _raise(s, inst, "SQUEEZE", "HIGH", {"feeBps": fee, "baseFeeBps": inst.base_fee_bps,
                                                          "multiple": round(fee / inst.base_fee_bps, 2)}, seen)
        for w in (await s.execute(select(Watch).order_by(Watch.instrument_id))).scalars().all():
            inst = instrument_by_id(w.instrument_id)
            fee, prev = market.fee_bps(inst, now), market.fee_bps(inst, now - timedelta(minutes=15))
            if fee >= prev * 1.25:
                await _raise(s, inst, "FEE_SPIKE", "MEDIUM", {"feeBps": fee, "previousFeeBps": prev}, seen)
            util = market.util_pct(inst, now)
            if util >= 90:
                await _raise(s, inst, "UTIL_HIGH", "MEDIUM", {"utilizationPct": util}, seen)
            si = metrics.snapshot(inst, now)["shortInterest"]
            if (si["change1dPct"] or 0) >= 10:
                await _raise(s, inst, "SI_JUMP", "MEDIUM", {"pctSharesOut": si["pctSharesOut"],
                                                            "change1dPct": si["change1dPct"]}, seen)
        await s.commit()
    return {"raised": len(seen) - before, "at": clock.iso(now)}


@app.websocket("/v1/stream")
async def stream(ws: WebSocket, key: str = Query(""), topics: str = "alerts") -> None:
    """Live alerts as they are raised."""
    if key != api_key():
        await ws.close(code=4401)
        return
    await ctx.ws_stream(ws, [t.strip() for t in topics.split(",") if t.strip()])


# -- OpenFIGI-shaped mapping -----------------------------------------------------------------------
class MappingJob(BaseModel):
    idType: str
    idValue: str
    exchCode: str | None = None
    micCode: str | None = None


_ID_TYPES = ("ID_ISIN", "ID_CUSIP", "ID_SEDOL", "ID_BB_GLOBAL", "COMPOSITE_ID_BB_GLOBAL", "TICKER")


def _map_one(job: MappingJob) -> dict[str, Any]:
    if job.idType not in _ID_TYPES:
        return {"error": f"Invalid idType {job.idType!r}; this mock supports {list(_ID_TYPES)}."}
    if job.idType == "TICKER":
        if not (job.exchCode or job.micCode):
            return {"error": "TICKER needs exchCode or micCode."}
        inst = resolve(f"{job.idValue} {job.exchCode}" if job.exchCode else f"{job.idValue}.{job.micCode}")
    else:
        inst = resolve(job.idValue)
        if inst and job.idType == "ID_CUSIP" and inst.cusip != job.idValue.upper():
            inst = None
    if inst and job.exchCode and inst.exch_code != job.exchCode.upper():
        inst = None
    if inst and job.micCode and inst.mic != job.micCode.upper():
        inst = None
    if inst is None:
        return {"warning": "No identifier found."}
    kind = "ETP" if inst.is_etf else "Common Stock"
    return {"data": [{"figi": inst.figi, "name": inst.name.upper(), "ticker": inst.ticker, "exchCode": inst.exch_code,
                      "compositeFIGI": inst.figi, "securityType": kind, "marketSector": "Equity",
                      "shareClassFIGI": None, "securityType2": "Mutual Fund" if inst.is_etf else "Common Stock",
                      "securityDescription": inst.ticker}]}


@app.post("/v1/mapping", tags=["reference"])
async def mapping(jobs: list[MappingJob], x_openfigi_apikey: str | None = Header(default=None),
                  x_api_key: str | None = Header(default=None)) -> list[dict]:
    """OpenFIGI v3 ``/mapping`` request and response shapes, answered from the synthetic universe.

    Like OpenFIGI: no key allows 10 jobs per request, a key 100. Results line up with the jobs."""
    keyed = api_key() in (x_openfigi_apikey, x_api_key)
    cap = 100 if keyed else 10
    if len(jobs) > cap:
        raise HTTPException(413, f"at most {cap} jobs per request {'with' if keyed else 'without'} an API key")
    return [_map_one(j) for j in jobs]


# -- admin ----------------------------------------------------------------------------------------
async def on_reset(c: ServiceContext) -> None:
    _flags.update(stale_feed=False, late_file=False, squeeze_mult=3.0)


spec.on_reset = on_reset
ctx.scheduler.every(900, "alert-scan", scan)


@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {"header": "X-Api-Key", "apiKey": api_key()}


@app.get("/_admin/flags", tags=["admin"])
async def get_flags() -> dict:
    return dict(_flags)


@app.put("/_admin/flags", tags=["admin"])
async def put_flags(flags: dict[str, Any]) -> dict:
    unknown = set(flags) - set(_flags)
    if unknown:
        raise HTTPException(422, f"unknown flags {sorted(unknown)}")
    _flags.update(flags)
    return dict(_flags)


@ctx.verb("flags")
async def verb_flags(args: dict[str, Any]) -> dict:
    """Set vendor flags: {stale_feed, late_file, squeeze_mult}"""
    return await put_flags(args)


@ctx.verb("scan")
async def verb_scan(args: dict[str, Any]) -> dict:
    """Run the alert scan now."""
    return await scan()


@ctx.verb("alert")
async def verb_alert(args: dict[str, Any]) -> dict:
    """Raise an alert on demand: {instrument, kind?: SQUEEZE|FEE_SPIKE|UTIL_HIGH|SI_JUMP, severity?}"""
    inst = _inst(str(args.get("instrument", "")))
    kind = str(args.get("kind", "SQUEEZE")).upper()
    now = clock.now()
    data = {"feeBps": market.fee_bps(inst, now), "utilizationPct": market.util_pct(inst, now), "injected": True}
    async with ctx.session() as s:
        seen = set((await s.execute(select(Alert.dedupe_key))).scalars())
        await _raise(s, inst, kind, str(args.get("severity", "HIGH")).upper(), data, seen)
        await s.commit()
    return {"ok": True, "isin": inst.isin, "kind": kind}
