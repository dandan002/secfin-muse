# Desk Chat (IB-style): chat bot API and terminal

> Simulator of institutional desk chat (the role Instant Bloomberg plays): persistent rooms, Blast,
> @mentions, and a bot integration of the kind IB Connect offers. The bot API shape is our own,
> not Bloomberg's licensed API. No vendor names, logos or layouts. All data and personas are
> fictional.

Counterparty personas (prime brokers, agent lenders, funds) post locates, IOIs and RFQs in desk
shorthand. The LoanDesk agent reads them through the **bot API**, and traders use the **Desk Chat**
web terminal ([`desk-chat/`](desk-chat)). Every persona message stores a **gold label** of what it
contains, for parser evaluations.

| | |
| --- | --- |
| Base | `https://ib.<domain>` (local `http://localhost:8010`); UI at `https://chat.<domain>` (local `:5173`); OpenAPI at `/docs`, snapshot in [`openapi.json`](openapi.json) |
| Bot auth | `Authorization: Bearer <bot token>`; WebSocket `?token=` |
| Human auth | `X-User: <handle>` / `?as=<handle>`: a demo identity switcher, **not** authentication |
| Connector | [`connector/`](connector): `DeskChatBot` |
| Bot identity | `loandesk-agent` |

## Connector

```python
from ib_connector import DeskChatBot

async with DeskChatBot("https://ib.mocks.example.com", bot_token=tok) as bot:
    last_seen = {s.streamId: s.lastSeq for s in await bot.streams()}
    async for msg in bot.subscribe():                       # every stream the bot is in
        last_seen[msg.streamId] = msg.seq
        if msg.sender.kind == "persona" and msg.chips:       # instruments Desk Chat recognised
            await bot.send(msg.streamId, "looking", client_msg_id=f"ack-{msg.messageId}")
            await bot.compose(msg.streamId, "can show 450bps open", user="srivera")   # draft for a human
# after a reconnect: async for m in bot.history(stream_id, after=last_seen[stream_id]): ...
```

## Bot API

| Method | Path | |
| --- | --- | --- |
| GET | `/streams` | Rooms the bot is in: `streamId`, `name`, `type`, `members`, `lastSeq` |
| GET | `/streams/{streamId}/messages?after=&limit=` | Messages with `seq > after`, oldest first, up to 1,000, plus `hasMore` |
| WS | `/streams/subscribe?token=` | Live events in every room the bot is in: `message`, `typing`, `room_created` |
| POST | `/chats/{streamId}/messages` | `{text, chips?, clientMsgId?}` → the stored message; personas react to what we say |
| POST | `/compose` | `{roomId, text, user}`: push a draft into a human trader's composer (they review and send) |

### Message

`messageId`, `streamId`, `seq` (per room, increasing by one, so a gap means you missed messages:
page `history` after your last seq), `sender` {uuid, handle, displayName, firm, kind:
human|persona|bot, cpCode}, `text`, `chips`, `mentions` (user uuids), `kind`, `via`
(human|bot|persona), `blastId`, `clientMsgId`, `ts`.

**Chips** are structured instrument references attached to spans of the text:
`{type: "instrument", instrumentId, isin, composite, quantity, start, end}`. Desk Chat resolves
tickers, ISINs, CUSIPs and SEDOLs in what people type, as a terminal would.

## Human API (used by the Desk Chat UI)

`GET /api/me`, `GET /api/users`, `GET|POST /api/rooms`, `GET|POST /api/rooms/{id}/messages`
(`before`/`after` paging), `POST /api/blast` (`{roomIds, text}`: one message to many rooms),
`POST /api/rooms/{id}/read` (`{seq}`), `POST /api/rooms/{id}/typing`, `PUT /api/rooms/{id}/prefs`
(folder, tab position, mute), `GET /api/search?q=`, `GET /api/instruments/resolve?q=` (instrument
card with tier, fee, price), and `WS /ws?as=<handle>` (messages, typing, read receipts, compose
drafts). Identity comes from `X-User`, so put this API behind your own authentication if it's ever
exposed beyond a demo allowlist.

## Personas

| Persona | Firm type | Sends |
| --- | --- | --- |
| PB-ALPHA | Prime broker | Terse locate blasts (`pb-alpha`) |
| PB-BRAVO | Prime broker | Locate lists by SEDOL/ISIN (`pb-bravo`) |
| Harbor Lane | Agent lender | Morning availability IOIs (`harbor-lending`) |
| Kestrel | Hedge fund | RFQs; negotiates against a hidden limit (`kestrel-hf`) |
| Delta One | Internal desk | Internal borrow requests (`delta-one`) |
| Juniper | Hedge fund | RFQs, plus a prompt-injection attempt (`juniper-hf`) |

Personas are scripted by default. With `MOCK_LLM_MODE` set to `replay`, `record` or `live`, an
LLM restyles each scripted message, and the restyled text is rejected unless every identifier
survives, so the gold labels stay true.

## Simulator controls (`/_admin`, needs `X-Admin-Token`)

`GET /_admin/credentials` (bot handle and token), `GET /_admin/labels?after=` (gold labels),
`GET /_admin/personas`, `GET /_admin/llm` (calls, spend, cache misses); scenario verbs
`ib.persona {persona, action}`, `ib.say {as, room, text}`, `ib.persona_mode {persona, mode}`,
`ib.ambient {on, everyS}`. `cd mocks/ib && uv run mock-ib-corpus` writes the 300-conversation
labelled eval set.
