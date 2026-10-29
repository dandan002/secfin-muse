"""A cached OpenFIGI v3 mapping client.

Point it at the public service (``https://api.openfigi.com/v3``) for real identifiers, or at the
analytics mock (``<url>/v1``), which answers the same request shape from the synthetic universe.

Mappings barely change, so look-ups are cached (hits for 30 days, misses for one day by default)
and only the uncached ones are sent, batched to the service's per-request cap (10 jobs without a
key, 100 with one). 429s are retried after the server's ``Retry-After``.

    async with FigiResolver(api_key=os.environ.get("OPENFIGI_API_KEY")) as figi:
        hits = await figi.map([FigiJob("ID_ISIN", "US0378331005"), FigiJob("TICKER", "IBM", exch_code="US")])
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, MutableMapping
from dataclasses import dataclass
from typing import Any

import httpx

from ._http import Http

OPENFIGI_URL = "https://api.openfigi.com/v3"


@dataclass(frozen=True)
class FigiJob:
    id_type: str  # ID_ISIN, ID_CUSIP, ID_SEDOL, ID_BB_GLOBAL, TICKER, ...
    id_value: str
    exch_code: str | None = None
    mic_code: str | None = None

    def to_json(self) -> dict[str, str]:
        body = {"idType": self.id_type, "idValue": self.id_value}
        if self.exch_code:
            body["exchCode"] = self.exch_code
        if self.mic_code:
            body["micCode"] = self.mic_code
        return body


class FigiResolver:
    def __init__(self, base_url: str = OPENFIGI_URL, api_key: str | None = None,
                 cache: MutableMapping[FigiJob, tuple[float, list[dict[str, Any]]]] | None = None,
                 hit_ttl_s: float = 30 * 86_400, miss_ttl_s: float = 86_400, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None, now: Callable[[], float] = time.time) -> None:
        headers = {"Content-Type": "application/json"} | ({"X-OPENFIGI-APIKEY": api_key} if api_key else {})
        self._h = Http(base_url, headers, timeout, transport)
        self.batch = 100 if api_key else 10
        self.cache = {} if cache is None else cache  # any mapping works: a dict, or a Redis/DB-backed one
        self.hit_ttl_s, self.miss_ttl_s, self.now = hit_ttl_s, miss_ttl_s, now
        self.requests = 0

    async def __aenter__(self) -> FigiResolver:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._h.aclose()

    async def map(self, jobs: Iterable[FigiJob]) -> list[list[dict[str, Any]]]:
        """One result list per job, in order; empty when nothing matched."""
        jobs = list(jobs)
        now = self.now()
        todo = list(dict.fromkeys(j for j in jobs if not (j in self.cache and self.cache[j][0] > now)))
        for i in range(0, len(todo), self.batch):
            chunk = todo[i:i + self.batch]
            self.requests += 1
            results = await self._h.json("POST", "/mapping", json=[j.to_json() for j in chunk])
            for job, res in zip(chunk, results, strict=True):
                if "error" in res:  # a bad request, not a miss: don't cache it
                    raise ValueError(f"OpenFIGI rejected {job}: {res['error']}")
                data = res.get("data", [])
                self.cache[job] = (now + (self.hit_ttl_s if data else self.miss_ttl_s), data)
        return [self.cache[j][1] for j in jobs]

    async def figi(self, id_type: str, id_value: str, **kw: str) -> str | None:
        """The first match's FIGI, or None."""
        hits = (await self.map([FigiJob(id_type, id_value, **kw)]))[0]
        return hits[0]["figi"] if hits else None
