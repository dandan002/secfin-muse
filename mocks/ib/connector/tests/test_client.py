import httpx
from ib_connector import DeskChatBot

MSG = {"messageId": 1, "streamId": "r1", "seq": 1, "text": "hi", "kind": "text", "via": "persona", "ts": "t",
       "sender": {"uuid": "u", "handle": "pb", "displayName": "PB", "firm": "F", "kind": "persona"}}


async def test_history_pages_until_done():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        after = int(req.url.params["after"])
        calls.append(after)
        if after == 0:
            return httpx.Response(200, json={"messages": [MSG, {**MSG, "messageId": 2, "seq": 2}], "hasMore": True})
        return httpx.Response(200, json={"messages": [{**MSG, "messageId": 3, "seq": 3}], "hasMore": False})

    async with DeskChatBot("http://ib", bot_token="t", transport=httpx.MockTransport(handler)) as bot:
        seqs = [m.seq async for m in bot.history("r1")]
    assert seqs == [1, 2, 3] and calls == [0, 2]
