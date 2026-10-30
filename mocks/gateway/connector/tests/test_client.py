import json

import httpx
from gateway_connector import CounterpartyClient, webhooks


def test_webhook_signatures():
    body = b'{"locateId":"LOC-1"}'
    header = webhooks.sign("s3cret", body, timestamp=1_000)
    assert webhooks.verify("s3cret", body, header, now=1_100)
    assert not webhooks.verify("s3cret", body + b" ", header, now=1_100)  # tampered
    assert not webhooks.verify("other", body, header, now=1_100)
    assert not webhooks.verify("s3cret", body, header, now=10_000)  # replayed long after
    assert webhooks.verify("s3cret", body, header, tolerance_s=None)
    assert not webhooks.verify("s3cret", body, "garbage")


async def test_submit_formats_idempotency_and_rate_limit_retry():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"detail": "rate limit exceeded"})
        return httpx.Response(202, json={"locateId": "LOC-1", "status": "RECEIVED", "lines": 2})

    async with CounterpartyClient("http://gw", api_key="k", transport=httpx.MockTransport(handler)) as cp:
        r = await cp.submit_locates([("XS0000000001", 1000), ("ABC US", 500)], request_id="R1")
        await cp.submit_locates([("XS0000000001", 1000)], fmt="CSV", idempotency_key="K2")
    assert r.locateId == "LOC-1"
    first, retry, csv_req = seen
    assert first.headers["idempotency-key"] == retry.headers["idempotency-key"]  # a retry is the same request
    doc = json.loads(retry.content)["borrowerLocate"]
    assert doc["locateLines"][1] == {"security": {"identifier": "ABC US"},
                                     "quantity": {"amount": 500, "unit": "SHARES"}}
    assert csv_req.headers["content-type"] == "text/csv" and csv_req.headers["idempotency-key"] == "K2"
    assert csv_req.content.decode().splitlines() == ["identifier,quantity", "XS0000000001,1000"]


async def test_fee_limits_and_the_agreed_fee():
    from gateway_connector import Decision

    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(202, json={"locateId": "LOC-1", "status": "RECEIVED", "lines": 2})

    async with CounterpartyClient("http://gw", api_key="k", transport=httpx.MockTransport(handler)) as cp:
        await cp.submit_locates([("XS0000000001", 1000, 450.0), ("ABC US", 500)])
        await cp.submit_locates([("XS0000000001", 1000, 450.0), ("ABC US", 500)], fmt="CSV")
    cdm = json.loads(seen[0].content)["borrowerLocate"]["locateLines"]
    assert cdm[0]["maxFee"] == {"bps": 450.0} and "maxFee" not in cdm[1]
    assert seen[1].content.decode().splitlines() == ["identifier,quantity,maxFeeBps", "XS0000000001,1000,450.0",
                                                     "ABC US,500,"]
    # The old name still reads; it goes out as the agreed fee.
    assert Decision(lineNo=1, decision="APPROVED", indicativeFeeBps=30).model_dump(exclude_none=True)["feeBps"] == 30
