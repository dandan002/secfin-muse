"""Async client for the market-analytics vendor's API (see ../../API.md)."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterable
from datetime import date, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from ._http import Http


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class ShortInterest(_Model):
    shares: int
    pctSharesOut: float
    pctFloat: float
    notional: float
    daysToCover: float
    change1dPct: float | None = None
    change7dPct: float | None = None
    change30dPct: float | None = None
    source: str


class Borrow(_Model):
    indicativeFeeBps: float
    utilizationPct: float
    lendableShares: int
    onLoanShares: int
    availableShares: int
    costToBorrowScore: int
    tier: Literal["GC", "WARM", "HOT"]


class Snapshot(_Model):
    isin: str
    ticker: str
    composite: str
    asOf: datetime
    price: float
    ccy: str
    shortInterest: ShortInterest
    borrow: Borrow
    crowdingScore: float
    squeezeRisk: float
    thresholdList: bool
    identifiers: dict[str, Any] | None = None  # on single-name lookups


class Screen(_Model):
    asOf: datetime
    total: int
    offset: int
    items: list[Snapshot]


class HistoryPoint(_Model):
    date: date
    siShares: int
    siPctSharesOut: float
    daysToCover: float
    feeBps: float
    utilizationPct: float
    price: float


class ExchangeShortInterest(_Model):
    isin: str
    settlementDate: date
    publishDate: date
    currentShortPosition: int
    previousShortPosition: int
    changePct: float | None
    daysToCover: float


class Alert(_Model):
    alertId: str
    kind: Literal["SQUEEZE", "FEE_SPIKE", "UTIL_HIGH", "SI_JUMP"]
    severity: Literal["HIGH", "MEDIUM"]
    isin: str
    composite: str
    ts: datetime


class AnalyticsClient:
    """Read short-interest and lending-market data, bulk-load the daily file, and watch for alerts.

    >>> async with AnalyticsClient(url, api_key=key) as data:
    ...     snap = await data.security("QZNT US")
    ...     crowded = await data.screen(country="US", sort="squeezeRisk", limit=20)
    ...     rows = await data.daily_file()                      # latest published file, parsed
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key
        self._h = Http(base_url, {"X-Api-Key": api_key}, timeout, transport)

    async def __aenter__(self) -> AnalyticsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._h.aclose()

    # -- per name -----------------------------------------------------------------------------
    async def security(self, ref: str) -> Snapshot:
        """Latest analytics for one name, with its identifiers; ``ref`` is any identifier."""
        out = await self._h.json("GET", f"/v1/securities/{ref}")
        return Snapshot.model_validate(out)

    async def history(self, ref: str, start: date | None = None, end: date | None = None) -> list[HistoryPoint]:
        params = {k: v.isoformat() for k, v in {"from": start, "to": end}.items() if v}
        out = await self._h.json("GET", f"/v1/securities/{ref}/history", params=params)
        return [HistoryPoint.model_validate(p) for p in out["series"]]

    async def screen(self, country: str | None = None, sector: str | None = None, tier: str | None = None,
                     min_pct_float: float = 0, min_crowding: float = 0, sort: str = "crowdingScore",
                     limit: int = 100, offset: int = 0) -> Screen:
        """The universe filtered and sorted, highest first (crowded shorts by default)."""
        params: dict[str, Any] = {"country": country, "sector": sector, "tier": tier, "minPctFloat": min_pct_float,
                                  "minCrowding": min_crowding, "sort": sort, "limit": limit, "offset": offset}
        return Screen.model_validate(await self._h.json("GET", "/v1/short-interest",
                                                        params={k: v for k, v in params.items() if v is not None}))

    # -- exchange-reported ----------------------------------------------------------------------
    async def exchange_short_interest(self, settlement_date: date | None = None, instrument: str | None = None,
                                      limit: int = 2000) -> list[ExchangeShortInterest]:
        """US short positions on a reporting settlement date (default: the latest published)."""
        params: dict[str, Any] = {"limit": limit}
        if settlement_date:
            params["settlementDate"] = settlement_date.isoformat()
        if instrument:
            params["instrument"] = instrument
        out = await self._h.json("GET", "/v1/exchange-short-interest", params=params)
        return [ExchangeShortInterest.model_validate(i) for i in out["items"]]

    # -- daily file ---------------------------------------------------------------------------
    async def files(self) -> list[dict[str, Any]]:
        return await self._h.json("GET", "/v1/files")

    async def daily_file(self, as_of: date | None = None) -> list[dict[str, str]]:
        """The daily file, parsed (CSV columns as keys). Default: the latest published one.
        Raises ``ApiError`` 404 when that day's file has not been published yet."""
        day = as_of.isoformat() if as_of else (await self.files())[0]["asOf"]
        text = (await self._h.raw("GET", f"/v1/files/{day}/short-interest.csv")).text
        return list(csv.DictReader(io.StringIO(text)))

    # -- watchlist and alerts -----------------------------------------------------------------
    async def watchlist(self) -> list[dict[str, Any]]:
        return await self._h.json("GET", "/v1/watchlist")

    async def set_watchlist(self, instruments: Iterable[str]) -> list[dict[str, Any]]:
        """Replace the watchlist; its names get fee-spike, utilization and SI-jump alerts."""
        out = await self._h.json("PUT", "/v1/watchlist", json={"instruments": list(instruments)})
        return out["items"]

    async def unwatch(self, ref: str) -> None:
        await self._h.raw("DELETE", f"/v1/watchlist/{ref}")

    async def alerts(self, since: datetime | None = None, kind: str | None = None) -> list[Alert]:
        params = {k: v for k, v in {"since": since.isoformat() if since else None, "kind": kind}.items() if v}
        return [Alert.model_validate(a) for a in await self._h.json("GET", "/v1/alerts", params=params)]

    async def stream(self, topics: Iterable[str] = ("alerts",)) -> AsyncIterator[dict[str, Any]]:
        """Alert events as they are raised. Each has ``topic``, ``type``, ``topicSeq``, ``data``."""
        async for event in self._h.ws(f"/v1/stream?key={self.api_key}&topics={','.join(topics)}"):
            yield event
