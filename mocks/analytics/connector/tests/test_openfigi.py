"""The resolver caches, batches to the key-dependent cap, and keeps results in job order (offline)."""

import json

import httpx
import pytest
from analytics_connector import FigiJob, FigiResolver


def _transport(calls: list[list[dict]]) -> httpx.MockTransport:
    def handle(req: httpx.Request) -> httpx.Response:
        jobs = json.loads(req.content)
        calls.append(jobs)
        out = [{"data": [{"figi": "BBG" + j["idValue"][-9:]}]} if j["idValue"] != "MISS" else
               {"warning": "No identifier found."} for j in jobs]
        return httpx.Response(200, json=out)

    return httpx.MockTransport(handle)


async def test_batches_and_caches():
    calls: list[list[dict]] = []
    jobs = [FigiJob("ID_ISIN", f"US{n:010d}") for n in range(25)] + [FigiJob("ID_ISIN", "MISS")]
    async with FigiResolver(transport=_transport(calls)) as r:  # no key: 10 jobs per request
        out = await r.map(jobs)
        assert [len(c) for c in calls] == [10, 10, 6]
        assert out[0][0]["figi"] == "BBG" + "US0000000000"[-9:] and out[-1] == []
        again = await r.map(reversed(jobs))
        assert len(calls) == 3 and again[0] == [] and again[-1] == out[0]  # all from cache, in order


async def test_key_raises_cap_and_misses_expire():
    calls: list[list[dict]] = []
    clock = [0.0]
    async with FigiResolver(api_key="k", transport=_transport(calls), now=lambda: clock[0]) as r:
        await r.map([FigiJob("ID_ISIN", f"US{n:010d}") for n in range(30)] + [FigiJob("ID_ISIN", "MISS")])
        assert [len(c) for c in calls] == [31]
        clock[0] = 2 * 86_400  # past the miss TTL, inside the hit TTL
        assert await r.figi("ID_ISIN", "MISS") is None and await r.figi("ID_ISIN", "US0000000001")
        assert calls[-1] == [{"idType": "ID_ISIN", "idValue": "MISS"}]


async def test_errors_are_not_cached():
    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"error": "Invalid idType."}])

    async with FigiResolver(transport=httpx.MockTransport(handle)) as r:
        with pytest.raises(ValueError):
            await r.figi("NOPE", "X")
        assert r.cache == {}
