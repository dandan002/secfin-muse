"""Request shapes, against an in-memory transport (no venue involved)."""

import json

import httpx
import pytest
from wematch_connector import ApiError, ListLine, VenueBClient


async def test_upload_json_and_csv_and_errors():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/accept"):
            return httpx.Response(409, json={"error": "BAD_STATE", "message": "match is AGREED"})
        return httpx.Response(201, json={"listId": "WM-L-1", "name": "n", "product": "SBL", "status": "OPEN",
                                         "lines": 1})

    async with VenueBClient("http://wm", api_key="k", transport=httpx.MockTransport(handler)) as wm:
        line = ListLine(instrument="XS0000000001", side="BORROW", quantity=1000, feeBps=40, minQuantity=500)
        await wm.upload_list([line], client_ref="R1")
        await wm.upload_csv([line])
        with pytest.raises(ApiError) as e:
            await wm.accept("M1")
        assert (e.value.status, e.value.code) == (409, "BAD_STATE")
    body = json.loads(seen[0].content)
    assert seen[0].headers["x-api-key"] == "k" and body["clientRef"] == "R1" and body["lines"][0]["feeBps"] == 40
    assert seen[1].headers["content-type"] == "text/csv"
    assert seen[1].content.decode().splitlines() == ["instrument,side,quantity,fee_bps,min_qty",
                                                     "XS0000000001,BORROW,1000,40.0,500"]
