import json

import httpx
import pytest
from control_connector import SimControl


async def test_requests_carry_token_and_bodies():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    async with SimControl("http://ctl", admin_token="adm", transport=httpx.MockTransport(handler)) as sim:
        await sim.advance(seconds=30)
        await sim.set_fault("ngt", outage=True)
        await sim.run_scenario("demo", wait=True)
        with pytest.raises(ValueError):
            await sim.advance()
    assert all(r.headers["x-admin-token"] == "adm" for r in seen)
    assert json.loads(seen[0].content) == {"seconds": 30}
    assert (seen[1].method, seen[1].url.path, json.loads(seen[1].content)) == ("PUT", "/faults/ngt", {"outage": True})
    assert seen[2].url.params["wait"] == "true"
