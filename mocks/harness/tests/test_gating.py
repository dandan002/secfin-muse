"""The gate in front of every mock: keys, scopes, plans, rotation and revocation, over real HTTP."""

from __future__ import annotations

import asyncio

import pytest


def _bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


def test_every_business_api_refuses_anonymous_callers(mocks):
    for svc, path in [("control", "/clock"), ("ib", "/api/rooms"), ("ib", "/streams"), ("gateway", "/v1/inbox"),
                      ("ngt", "/v1/availability"), ("wematch", "/v1/loans"), ("sfp", "/contracts"),
                      ("custodian", "/instructions")]:
        r = mocks.http.get(mocks.url(svc, path))
        assert r.status_code == 401, (svc, path, r.status_code)
        assert r.json()["detail"]["error"] == "unauthenticated" and "WWW-Authenticate" in r.headers
        assert mocks.http.get(mocks.url(svc, "/health")).status_code == 200  # liveness stays open


def test_scopes_split_read_only_keys_from_writers(mocks):
    sfp = mocks.creds("sfp")
    ro = {"x-api-key": sfp["readOnlyKey"]}
    assert mocks.get("sfp", "/contracts", headers=ro).status_code == 200
    r = mocks.post("sfp", "/trades", headers=ro, json={})
    assert r.status_code == 403 and r.json()["detail"]["missingScopes"] == ["trades:write"]

    observer = _bearer(mocks.creds("control")["observerKey"])
    assert mocks.get("control", "/clock", headers=observer).status_code == 200
    assert mocks.post("control", "/reset", headers=observer, json={}).status_code == 403

    md = {"x-participant-key": mocks.creds("ngt")["marketDataKey"]}
    assert mocks.get("ngt", "/v1/availability?limit=1", headers=md).status_code == 200
    assert mocks.get("ngt", "/v1/orders", headers=md).status_code == 403


def test_a_key_from_one_system_opens_nothing_else(mocks):
    sfp_key = {"x-api-key": mocks.creds("sfp")["apiKey"]}
    assert mocks.get("custodian", "/instructions", headers=sfp_key).status_code == 401
    assert mocks.get("wematch", "/v1/loans", headers=sfp_key).status_code == 401


def test_issue_rotate_and_revoke_a_counterparty_key(mocks):
    r = mocks.post("gateway", "/_admin/keys", json={"principal": "CEDARHF", "scopes": ["locates:read"],
                                                    "plan": "trial", "label": "Cedar Hollow onboarding"})
    assert r.status_code == 201, r.text
    issued = r.json()
    key, key_id = issued["apiKey"], issued["keyId"]
    assert key.startswith(f"mk_gateway_{key_id}_")
    listed = mocks.get("gateway", "/_admin/keys").json()
    row = next(k for k in listed["keys"] if k["keyId"] == key_id)
    assert row["plan"] == "trial" and "apiKey" not in row and key not in str(listed)  # never shown again

    me = mocks.get("gateway", "/whoami", headers=_bearer(key))
    assert me.json()["principal"] == "CEDARHF" and me.headers["X-RateLimit-Limit"] == "1000"
    assert mocks.post("gateway", "/v1/locates", headers=_bearer(key),
                      json={"identifier": "@gc:0:US", "quantity": 1000}).status_code == 403  # read-only so far
    mocks.patch("gateway", f"/_admin/keys/{key_id}", json={"scopes": ["locates:read", "locates:write"]}
                ).raise_for_status()
    assert mocks.post("gateway", "/v1/locates", headers=_bearer(key),
                      json={"identifier": "@gc:0:US", "quantity": 1000}).status_code == 202

    rotated = mocks.post("gateway", f"/_admin/keys/{key_id}/rotate", json={"graceSeconds": 0}).json()
    assert mocks.get("gateway", "/whoami", headers=_bearer(key)).status_code == 401
    new = rotated["apiKey"]
    assert mocks.get("gateway", "/whoami", headers=_bearer(new)).json()["scopes"] == ["locates:read", "locates:write"]

    mocks.post("gateway", f"/_admin/keys/{rotated['keyId']}/revoke").raise_for_status()
    r = mocks.get("gateway", "/whoami", headers=_bearer(new))
    assert r.status_code == 401 and "revoked" in r.json()["detail"]["detail"]


def test_keys_and_revocations_survive_a_reset(mocks):
    # (Revoking a bootstrap key would outlive this test too, so this uses issued keys.)
    kept, gone = (mocks.post("custodian", "/_admin/keys", json={"principal": "MKLDUS33XXX", "label": label,
                                                                "scopes": ["instructions:read"]}).json()
                  for label in ("kept", "gone"))
    mocks.post("custodian", f"/_admin/keys/{gone['keyId']}/revoke").raise_for_status()
    mocks.reset(seed=7)
    assert mocks.get("custodian", "/instructions", headers={"x-api-key": kept["apiKey"]}).status_code == 200
    assert mocks.get("custodian", "/instructions", headers={"x-api-key": gone["apiKey"]}).status_code == 401


def test_quota_and_rate_limit_answer_429_with_retry_after(mocks):
    key = mocks.post("wematch", "/_admin/keys", json={"principal": "WM-LOANDESK-NYC", "scopes": ["loans:read"],
                                                      "plan": "trial"}).json()["apiKey"]
    mocks.put("wematch", "/_admin/plans/trial", json={"ratePerSecond": 1000, "burst": 1000, "dailyQuota": 2})
    codes = [mocks.get("wematch", "/v1/loans", headers={"x-api-key": key}) for _ in range(3)]
    assert [r.status_code for r in codes] == [200, 200, 429]
    assert codes[1].headers["X-RateLimit-Remaining"] == "0"
    assert codes[2].json()["detail"]["error"] == "quota_exceeded" and int(codes[2].headers["Retry-After"]) > 0
    mocks.delete("wematch", "/_admin/plans").raise_for_status()


def test_websockets_are_gated_by_scope(mocks):
    websockets = pytest.importorskip("websockets")
    ws_url = mocks.url("ngt", "/v1/stream").replace("http", "ws", 1)
    md = mocks.creds("ngt")["marketDataKey"]

    async def first(url: str) -> str | int:
        try:
            async with websockets.connect(url) as ws:
                return (await asyncio.wait_for(ws.recv(), 5))[:30]
        except websockets.ConnectionClosed as e:
            return e.rcvd.code if e.rcvd else -1
        except websockets.InvalidStatus as e:
            return e.response.status_code

    assert asyncio.run(first(f"{ws_url}?topics=availability&key={md}")).startswith("{")
    assert asyncio.run(first(f"{ws_url}?topics=orders&key={md}")) in (4403, 403)
    assert asyncio.run(first(f"{ws_url}?topics=availability")) in (4401, 403)
