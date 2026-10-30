"""mock-ib: "Desk Chat", a chat server with IB-like behaviours and a bot API.

Two surfaces:
* Human API (``/api``, ``/ws``) used by the Desk Chat React client, behind the desk key
  (``X-Api-Key``, or ``?key=`` on the WebSocket). Who you act as comes from the ``X-User``
  header (or ``?as=``), so one browser can play any persona in a demo.
* Bot API (``/streams``, ``/chats``, ``/compose``), shaped like a chat-bot integration and
  authenticated with the bot's bearer key. This is what the LoanDesk agent will use.

Persona counterparties post scripted (or LLM-restyled) desk shorthand and react to what
we send them. Every persona message stores a gold label for parser evals.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

from fastapi import Depends, Header, HTTPException, Query, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mockcore import clock, rng
from mockcore.app import ServiceContext, ServiceSpec, create_app
from mockcore.keys import AuthSpec, Grant, Principal
from refdata import market
from refdata.universe import resolve

from . import personas as P
from .llm import LLM
from .models import Base, Label, Member, Message, PersonaState, Room, User

SCOPES = {
    "chat:read": "Desk Chat: rooms, messages, search, users, instrument cards",
    "chat:write": "Desk Chat: send, Blast, create rooms, read marks, typing, room prefs",
    "streams:read": "Bot: list streams, read messages, subscribe",
    "messages:write": "Bot: post into a stream",
    "compose": "Bot: push a draft into a human's composer",
}
spec = ServiceSpec(
    name="mock-ib",
    label="Desk Chat",
    schema="ib",
    base=Base,
    description=(
        "Persistent chat rooms, Blast, @mentions, search, typing and unread counts, plus a bot API "
        "(streams, subscribe, post as bot, push to composer). Behaviour modelled on institutional chat; "
        "the API shape and look are our own."
    ),
    auth=AuthSpec(SCOPES, lambda: [
        Grant(BOT_HANDLE, ("streams:read", "messages:write", "compose"), "partner", "LoanDesk agent (bot)"),
        Grant("DESK", ("chat:read", "chat:write"), "standard", "Desk Chat terminal", name="desk"),
    ]),
)
ctx = ServiceContext(spec)
app = create_app(ctx)
llm = LLM()

NS = uuid.UUID("6b1f2a1e-7c1d-4c55-9a59-0c0ffee00001")
BOT_HANDLE = "loandesk-agent"
OUR_FIRM = "LoanDesk Demo Bank"
OUR_HUMANS = (("srivera", "Sam Rivera"), ("kli", "Kim Li"))
_persona_modes: dict[str, str] = {}
_ambient: dict[str, Any] = {"on": False, "every_s": 240}


def uid(handle: str) -> str:
    return str(uuid.uuid5(NS, f"ib:{handle}"))


# -- seeding -------------------------------------------------------------------------------
async def on_reset(c: ServiceContext) -> None:
    _persona_modes.clear()
    _ambient.update(on=False, every_s=240)
    now = clock.now()
    async with c.session() as s:
        for handle, name in OUR_HUMANS:
            s.add(User(id=uid(handle), handle=handle, display_name=name, firm=OUR_FIRM, kind="human"))
        s.add(User(id=uid(BOT_HANDLE), handle=BOT_HANDLE, display_name="LoanDesk Agent", firm=OUR_FIRM, kind="bot"))
        for p in P.PERSONAS.values():
            s.add(User(id=uid(p.handle), handle=p.handle, display_name=p.display_name, firm=p.firm,
                       kind="persona", persona=p.key, cp_code=p.cp_code, tz=p.tz))
        await s.flush()
        ours = [uid(h) for h, _ in OUR_HUMANS]
        tab = 0
        for p in P.PERSONAS.values():
            s.add(Room(id=p.room_id, name=p.room_name, kind="persistent", tz=p.tz, created_at=now,
                       topic=f"{p.firm} ⇄ {OUR_FIRM}"))
            await s.flush()
            s.add(Member(room_id=p.room_id, user_id=uid(p.handle)))
            s.add(Member(room_id=p.room_id, user_id=uid(BOT_HANDLE)))
            for u in ours:
                s.add(Member(room_id=p.room_id, user_id=u, folder=p.folder, tab_pos=tab if tab < 4 else None))
            tab += 1
        s.add(Room(id="sl-desk", name="SL Desk (internal)", kind="group", created_at=now, topic="Our desk"))
        s.add(Room(id="dm-srivera-kli", name="Sam Rivera / Kim Li", kind="direct", created_at=now))
        await s.flush()
        for u in [*ours, uid(BOT_HANDLE)]:
            s.add(Member(room_id="sl-desk", user_id=u, folder="Internal"))
        for u in ours:
            s.add(Member(room_id="dm-srivera-kli", user_id=u, folder="Internal"))
        await s.commit()
    await _seed_history(c)
    c.scheduler.cancel("ambient")


async def _seed_history(c: ServiceContext) -> None:
    """Yesterday's conversation in each persona room, so paging and search have something to show."""
    r = rng.fresh("ib", "history")
    start = clock.now() - timedelta(days=1)
    async with c.session() as s:
        for p in P.PERSONAS.values():
            t = start
            room = await s.get(Room, p.room_id)
            sender = await s.get(User, uid(p.handle))
            trader = await s.get(User, uid("srivera"))
            for _ in range(12):
                t += timedelta(minutes=r.uniform(5, 40))
                drafts = _generate(r, p, "auto", {}, at=t)
                for d in drafts[:1]:
                    await _post(s, room, sender, d.text, d.chips, via="persona", label=d.label, ts=t, publish=False)
                if r.random() < 0.5:
                    t += timedelta(minutes=r.uniform(1, 4))
                    await _post(s, room, trader, r.choice(("ok", "on it", "conf'd", "thx", "looking")),
                                via="human", ts=t, publish=False)
            for u in (uid("srivera"), uid("kli")):
                await s.execute(update(Member).where(Member.room_id == p.room_id, Member.user_id == u)
                                .values(last_read_seq=room.last_seq))
        await s.commit()


spec.on_reset = on_reset


# -- core message path -------------------------------------------------------------------------
MENTION = re.compile(r"@([a-z0-9._-]+)", re.I)


def user_json(u: User) -> dict[str, Any]:
    return {"uuid": u.id, "handle": u.handle, "displayName": u.display_name, "firm": u.firm, "kind": u.kind,
            "persona": u.persona, "cpCode": u.cp_code, "tz": u.tz}


def message_json(m: Message, sender: User) -> dict[str, Any]:
    return {
        "messageId": m.id,
        "streamId": m.room_id,
        "seq": m.seq,
        "sender": user_json(sender),
        "text": m.text,
        "chips": m.chips,
        "mentions": m.mentions,
        "kind": m.kind,
        "via": m.via,
        "blastId": m.blast_id,
        "clientMsgId": m.client_msg_id,
        "ts": clock.iso(m.created_at),
    }


async def _post(
    s: AsyncSession,
    room: Room,
    sender: User,
    text: str,
    chips: list[dict[str, Any]] | None = None,
    via: str = "human",
    label: dict[str, Any] | None = None,
    blast_id: str | None = None,
    client_msg_id: str | None = None,
    ts: Any = None,
    publish: bool = True,
) -> dict[str, Any]:
    if client_msg_id:
        existing = (await s.execute(select(Message).where(Message.client_msg_id == client_msg_id))).scalar()
        if existing:
            return message_json(existing, sender)
    ts = ts or clock.now()
    seq = (await s.execute(
        update(Room).where(Room.id == room.id).values(last_seq=Room.last_seq + 1).returning(Room.last_seq)
    )).scalar_one()
    room.last_seq = seq
    handles = {h.lower() for h in MENTION.findall(text)}
    mentions: list[str] = []
    if handles:
        rows = await s.execute(select(User.id, User.handle).where(func.lower(User.handle).in_(handles)))
        mentions = [r.id for r in rows]
    msg = Message(room_id=room.id, seq=seq, sender_id=sender.id, text=text, chips=chips or [], mentions=mentions,
                  via=via, blast_id=blast_id, client_msg_id=client_msg_id, created_at=ts)
    s.add(msg)
    await s.flush()
    if label is not None:
        s.add(Label(message_id=msg.id, label={k: v for k, v in label.items() if not k.startswith("_")}))
    await s.execute(update(Member).where(Member.room_id == room.id, Member.user_id == sender.id)
                    .values(last_read_seq=seq))
    body = message_json(msg, sender)
    await ctx.events.emit(s, "message", "message", f"{room.id}:{seq}", body, topic=f"room:{room.id}", ts=ts,
                          publish=publish)
    return body


async def send_and_react(room_id: str, sender_handle: str, text: str, chips: list | None = None,
                         via: str = "human", client_msg_id: str | None = None, blast_id: str | None = None) -> dict:
    async with ctx.session() as s:
        room = await s.get(Room, room_id)
        sender = (await s.execute(select(User).where(User.handle == sender_handle))).scalar_one()
        body = await _post(s, room, sender, text, chips, via=via, client_msg_id=client_msg_id, blast_id=blast_id)
        await s.commit()
    if sender.kind != "persona":
        await _schedule_reactions(room_id, text)
    return body


async def _schedule_reactions(room_id: str, text: str) -> None:
    async with ctx.session() as s:
        rows = await s.execute(
            select(User)
            .join(Member, Member.user_id == User.id)
            .where(Member.room_id == room_id, User.kind == "persona")
        )
        for u in rows.scalars():
            p = P.PERSONAS[u.persona]
            st = await s.get(PersonaState, (p.key, "open_rfq"))
            out = P.react(rng.stream("ib", "react", p.key), p, text, st.value if st else None, now=clock.now())
            if out is None:
                continue
            delay, draft, new_state = out
            if st and new_state is None:
                await s.delete(st)
            elif new_state is not None and st:
                st.value = new_state
            elif new_state is not None:
                s.add(PersonaState(persona=p.key, key="open_rfq", value=new_state))
            ctx.scheduler.after(delay, f"react:{p.key}:{clock.iso(clock.now())}", _persona_send, p.key, room_id, draft)
        await s.commit()


async def _restyle(p: P.PersonaDef, draft: P.Draft) -> P.Draft:
    if _persona_modes.get(p.key, "scripted") != "llm" or draft.label["intent"] in ("INJECTION",):
        return draft
    out = await llm.complete(
        [
            {"role": "system", "content": (
                "You write chat messages for a fictional securities-lending counterparty. "
                f"Persona: {p.llm_brief} Rewrite the user's message in that voice. Keep every identifier, number "
                "and line exactly; never add new securities or numbers. Output only the message."
            )},
            {"role": "user", "content": draft.text},
        ],
        temperature=0.7,
        max_tokens=400,
    )
    if not out:
        return draft
    for line in draft.label.get("lines", []):
        if line["identifier"] not in out:
            return draft  # the model dropped a name: keep the label honest, use the scripted text
    chips = [{**c, "start": out.find(c["composite"]) if c.get("composite") in out else -1} for c in draft.chips]
    return P.Draft(out, {**draft.label, "llm": True}, chips)


async def _persona_send(persona_key: str, room_id: str, draft: P.Draft) -> None:
    p = P.PERSONAS[persona_key]
    draft = await _restyle(p, draft)
    ctx.hub.publish(f"room:{room_id}", {"type": "typing", "data": {"streamId": room_id, "user": uid(p.handle)}})
    async with ctx.session() as s:
        room = await s.get(Room, room_id)
        sender = await s.get(User, uid(p.handle))
        await _post(s, room, sender, draft.text, draft.chips, via="persona", label=draft.label)
        await s.commit()


def _generate(r, p: P.PersonaDef, action: str, args: dict[str, Any], at=None) -> list[P.Draft]:
    """Drafts for a persona action. ``auto`` picks something in character."""
    if action == "auto":
        action = {"terse": "any_of_these", "list": "borrow_list", "chatty": "offer", "rfq": "rfq",
                  "internal": "internal_borrow", "redteam": "rfq"}[p.style]
        roll = r.random()
        if p.style == "chatty" and roll < 0.2:
            action = "recall"
        elif p.style == "chatty" and roll < 0.45:
            action = "ioi"
        elif p.style == "terse" and roll < 0.35:
            action = "rfq"
        elif p.style == "terse" and roll < 0.2:
            action = "return"
        elif roll > 0.85:
            action = "chatter"
        args = {"n": r.choice((3, 4, 6))} if action == "borrow_list" else {}
    tier_mix = args.get("tierMix")
    inst = resolve(str(args["instrument"])) if args.get("instrument") else None
    if args.get("instrument") and inst is None:
        raise HTTPException(422, f"unknown instrument {args['instrument']!r}")
    if action == "locate_blast":
        counts = args.get("tierCounts")
        n = sum(counts.values()) if counts else int(args.get("n", 10))
        return P.locate_blast(r, p, n, tier_mix, args.get("perMessage"), counts)
    if action == "internal_borrow":
        q = args.get("qty")
        insts = [inst] if inst else P.pick_instruments(r, 1, tier_mix)
        # The text and the label both carry ``qty``: overriding only the label left them disagreeing.
        return [P._locate_message(r, p if p.style == "internal" else P.PERSONAS["delta-one"], insts, q)]
    if action == "ioi":
        return [P.ioi(r, p, int(args.get("n", 4)), at=at or clock.now())]
    if action == "borrow_list":
        return [P.borrow_list(r, p, int(args.get("n", 5)))]
    if action == "any_of_these":
        insts = [resolve(str(x)) for x in args.get("instruments") or []]
        if any(i is None for i in insts):
            raise HTTPException(422, f"unknown instrument in {args.get('instruments')}")
        d, hidden = P.any_of_these(r, p, insts or None, args.get("qtys"), at=at or clock.now())
        if args.get("maxFeeBps"):  # a scripted limit, so the demo's answer to our quote is known
            for ln in hidden["lines"]:
                ln["maxFeeBps"] = float(args["maxFeeBps"])
        d.label["_hidden"] = hidden
        return [d]
    if action == "offer":
        return [P.offer(r, p, inst, args.get("qty"), at=at or clock.now(), gc=args.get("gc"))]
    if action == "recall":
        return [P.recall(r, p, inst, args.get("qty"))]
    if action == "return":
        return [P.return_shares(r, p, inst, args.get("qty"))]
    if action == "rfq":
        d, _hidden = P.rfq(r, p, inst, args.get("qty"), at=at or clock.now(), max_fee_bps=args.get("maxFeeBps"))
        d.label["_hidden"] = _hidden
        return [d]
    if action == "injection":
        return [P.injection(r, p, inst, int(args.get("qty", 5_000_000)))]
    if action == "chatter":
        return [P.chatter(r, p)]
    raise HTTPException(422, f"unknown persona action {action!r}")


async def persona_act(persona_key: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
    p = P.PERSONAS.get(persona_key)
    if p is None:
        raise HTTPException(404, f"unknown persona {persona_key!r}; have {sorted(P.PERSONAS)}")
    room_id = args.get("room", p.room_id)
    r = rng.stream("ib", "persona", p.key)
    drafts = _generate(r, p, action, args)
    spacing = float(args.get("spacingS", 4.0))
    async with ctx.session() as s:
        for d in drafts:
            hidden = d.label.pop("_hidden", None)
            if hidden:
                st = await s.get(PersonaState, (p.key, "open_rfq"))
                if st:
                    st.value = hidden
                else:
                    s.add(PersonaState(persona=p.key, key="open_rfq", value=hidden))
        await s.commit()
    first = drafts[0] if drafts else None
    sent = None
    if first is not None:
        await _persona_send(p.key, room_id, first)
        sent = first.text
    for i, d in enumerate(drafts[1:], start=1):
        ctx.scheduler.after(spacing * i, f"persona:{p.key}:{clock.iso(clock.now())}:{i}", _persona_send, p.key,
                            room_id, d)
    lines = sum(len(d.label.get("lines", [])) for d in drafts)
    return {"persona": p.key, "room": room_id, "messages": len(drafts), "lines": lines, "first": sent}


# -- auth ----------------------------------------------------------------------------------------
def current_user(scope: str):  # noqa: ANN201 - a FastAPI dependency
    """The human the desk key is acting as (``X-User``), once the key is entitled to ``scope``."""
    gate = ctx.require(scope)

    async def dep(_: Principal = Depends(gate), x_user: str | None = Header(default=None),
                  as_: str | None = Query(None, alias="as")) -> User:
        handle = x_user or as_ or "srivera"
        async with ctx.session() as s:
            u = (await s.execute(select(User).where((User.handle == handle) | (User.id == handle)))).scalar()
        if u is None:
            raise HTTPException(401, f"unknown user {handle!r}")
        return u

    return dep


def bot_user(scope: str):  # noqa: ANN201 - a FastAPI dependency
    """The bot user a bot key belongs to (its principal), entitled to ``scope``."""
    gate = ctx.require(scope)

    async def dep(p: Principal = Depends(gate)) -> User:
        async with ctx.session() as s:
            u = (await s.execute(select(User).where(User.handle == p.principal))).scalar()
        if u is None or u.kind != "bot":
            raise HTTPException(403, {"error": "forbidden", "detail": f"{p.principal} is not a bot user"})
        return u

    return dep


async def _member_or_404(s: AsyncSession, room_id: str, user: User) -> tuple[Room, Member]:
    row = (await s.execute(select(Room, Member).join(Member, Member.room_id == Room.id)
                           .where(Room.id == room_id, Member.user_id == user.id))).first()
    if row is None:
        raise HTTPException(404, "no such room (or not a member)")
    return row[0], row[1]


# -- human API -----------------------------------------------------------------------------------
class SendBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    chips: list[dict[str, Any]] | None = None
    clientMsgId: str | None = None


class BlastBody(SendBody):
    roomIds: list[str] = Field(..., min_length=1)


class RoomCreate(BaseModel):
    name: str
    kind: str = "group"
    members: list[str] = Field(default_factory=list, description="handles")


class Prefs(BaseModel):
    folder: str | None = None
    tabPos: int | None = None
    muted: bool | None = None


class ReadBody(BaseModel):
    seq: int


@app.get("/api/me", tags=["human"])
async def me(user: User = Depends(current_user("chat:read"))) -> dict:
    return user_json(user)


@app.get("/api/users", tags=["human"], dependencies=[Depends(ctx.require("chat:read"))])
async def users() -> list[dict]:
    async with ctx.session() as s:
        return [user_json(u) for u in (await s.execute(select(User).order_by(User.kind, User.handle))).scalars()]


async def _rooms_for(s: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = (await s.execute(select(Room, Member).join(Member, Member.room_id == Room.id)
                            .where(Member.user_id == user.id).order_by(Room.name))).all()
    out = []
    for room, mem in rows:
        unread_msgs = (await s.execute(select(Message.mentions).where(
            Message.room_id == room.id, Message.seq > mem.last_read_seq))).scalars().all()
        last = (await s.execute(select(Message, User).join(User, User.id == Message.sender_id)
                                .where(Message.room_id == room.id).order_by(Message.seq.desc()).limit(1))).first()
        members = (await s.execute(select(User).join(Member, Member.user_id == User.id)
                                   .where(Member.room_id == room.id))).scalars().all()
        out.append({
            "id": room.id, "name": room.name, "kind": room.kind, "tz": room.tz, "topic": room.topic,
            "folder": mem.folder, "tabPos": mem.tab_pos, "muted": mem.muted,
            "lastSeq": room.last_seq, "lastReadSeq": mem.last_read_seq,
            "unread": len(unread_msgs), "unreadMentions": sum(1 for m in unread_msgs if user.id in (m or [])),
            "lastMessage": message_json(last[0], last[1]) if last else None,
            "members": [user_json(m) for m in members],
        })
    return out


@app.get("/api/rooms", tags=["human"])
async def rooms(user: User = Depends(current_user("chat:read"))) -> list[dict]:
    async with ctx.session() as s:
        return await _rooms_for(s, user)


@app.post("/api/rooms", tags=["human"])
async def create_room(body: RoomCreate, user: User = Depends(current_user("chat:write"))) -> dict:
    async with ctx.session() as s:
        n = (await s.execute(select(func.count()).select_from(Room))).scalar_one()
        room = Room(id=f"room-{n + 1:04d}", name=body.name, kind=body.kind, created_at=clock.now())
        s.add(room)
        await s.flush()
        handles = {user.handle, *body.members}
        found = (await s.execute(select(User).where(User.handle.in_(handles)))).scalars().all()
        for u in found:
            s.add(Member(room_id=room.id, user_id=u.id, folder=None))
        await ctx.events.emit(s, "room_created", "room", room.id, {"id": room.id, "name": room.name,
                              "members": [u.id for u in found]}, topic="rooms")
        await s.commit()
    return {"id": room.id, "name": room.name, "members": [u.handle for u in found]}


@app.get("/api/rooms/{room_id}/messages", tags=["human"])
async def room_messages(room_id: str, before: int | None = None, after: int | None = None,
                        limit: int = Query(50, le=500), user: User = Depends(current_user("chat:read"))) -> dict:
    async with ctx.session() as s:
        await _member_or_404(s, room_id, user)
        return await _page(s, room_id, before, after, limit)


async def _page(s: AsyncSession, room_id: str, before: int | None, after: int | None, limit: int) -> dict:
    q = select(Message, User).join(User, User.id == Message.sender_id).where(Message.room_id == room_id)
    if after is not None:
        q = q.where(Message.seq > after).order_by(Message.seq).limit(limit)
        rows = (await s.execute(q)).all()
    else:
        if before is not None:
            q = q.where(Message.seq < before)
        rows = list(reversed((await s.execute(q.order_by(Message.seq.desc()).limit(limit))).all()))
    msgs = [message_json(m, u) for m, u in rows]
    has_more = bool(msgs) and msgs[0]["seq"] > 1 if after is None else len(msgs) == limit
    return {"streamId": room_id, "messages": msgs, "hasMore": has_more}


@app.post("/api/rooms/{room_id}/messages", tags=["human"])
async def send(room_id: str, body: SendBody, user: User = Depends(current_user("chat:write"))) -> dict:
    async with ctx.session() as s:
        await _member_or_404(s, room_id, user)
    via = "persona" if user.kind == "persona" else "human"
    return await send_and_react(room_id, user.handle, body.text, body.chips, via=via, client_msg_id=body.clientMsgId)


@app.post("/api/blast", tags=["human"])
async def blast(body: BlastBody, user: User = Depends(current_user("chat:write"))) -> dict:
    blast_id = f"blast-{clock.now().strftime('%H%M%S%f')}-{user.handle}"
    sent = []
    for rid in body.roomIds:
        async with ctx.session() as s:
            await _member_or_404(s, rid, user)
        sent.append(await send_and_react(rid, user.handle, body.text, body.chips, blast_id=blast_id))
    return {"blastId": blast_id, "messages": sent}


@app.post("/api/rooms/{room_id}/read", tags=["human"])
async def mark_read(room_id: str, body: ReadBody, user: User = Depends(current_user("chat:write"))) -> dict:
    async with ctx.session() as s:
        _, mem = await _member_or_404(s, room_id, user)
        mem.last_read_seq = max(mem.last_read_seq, body.seq)
        await s.commit()
    ctx.hub.publish(f"user:{user.id}", {"type": "read", "data": {"streamId": room_id, "seq": body.seq}})
    return {"ok": True}


@app.post("/api/rooms/{room_id}/typing", tags=["human"])
async def typing(room_id: str, user: User = Depends(current_user("chat:write"))) -> dict:
    ctx.hub.publish(f"room:{room_id}", {"type": "typing", "data": {"streamId": room_id, "user": user.id}})
    return {"ok": True}


@app.put("/api/rooms/{room_id}/prefs", tags=["human"])
async def prefs(room_id: str, body: Prefs, user: User = Depends(current_user("chat:write"))) -> dict:
    async with ctx.session() as s:
        _, mem = await _member_or_404(s, room_id, user)
        for k, attr in (("folder", "folder"), ("tabPos", "tab_pos"), ("muted", "muted")):
            if k in body.model_fields_set:
                setattr(mem, attr, getattr(body, k))
        await s.commit()
    return {"ok": True}


@app.get("/api/search", tags=["human"])
async def search(q: str = Query(..., min_length=1), roomId: str | None = None, limit: int = Query(50, le=200),
                 user: User = Depends(current_user("chat:read"))) -> list[dict]:
    async with ctx.session() as s:
        my_rooms = select(Member.room_id).where(Member.user_id == user.id)
        stmt = (select(Message, User).join(User, User.id == Message.sender_id)
                .where(Message.room_id.in_(my_rooms), Message.text.ilike(f"%{q}%"))
                .order_by(Message.id.desc()).limit(limit))
        if roomId:
            stmt = stmt.where(Message.room_id == roomId)
        return [message_json(m, u) for m, u in (await s.execute(stmt)).all()]


@app.get("/api/instruments/resolve", tags=["human"], dependencies=[Depends(ctx.require("chat:read"))])
async def resolve_instrument(q: str) -> dict:
    inst = resolve(q)
    if inst is None:
        raise HTTPException(404, "not found")
    return {**inst.to_json(), **market.snapshot(inst, clock.now())}


@app.websocket("/ws")
async def human_ws(ws: WebSocket, as_: str = Query("srivera", alias="as")) -> None:
    """The desk key goes in ``?key=``; ``?as=`` picks who you act as."""
    if await ctx.gate.websocket(ws, "chat:read") is None:
        return
    async with ctx.session() as s:
        user = (await s.execute(select(User).where((User.handle == as_) | (User.id == as_)))).scalar()
        if user is None:
            await ws.close(code=4401)
            return
        room_ids = (await s.execute(select(Member.room_id).where(Member.user_id == user.id))).scalars().all()
    topics = [f"room:{r}" for r in room_ids] + [f"user:{user.id}", "rooms"]

    async def on_msg(m: dict[str, Any]) -> None:
        if m.get("type") == "typing" and m.get("streamId") in room_ids:
            ctx.hub.publish(f"room:{m['streamId']}", {"type": "typing", "data": {"streamId": m["streamId"],
                                                                                 "user": user.id}})

    await ctx.ws_stream(ws, topics, on_message=on_msg)


# -- bot API ---------------------------------------------------------------------------------------
class ComposeBody(BaseModel):
    roomId: str
    text: str
    user: str = Field("srivera", description="handle of the human whose composer receives the text")


@app.get("/streams", tags=["bot"])
async def streams(bot: User = Depends(bot_user("streams:read"))) -> list[dict]:
    async with ctx.session() as s:
        return [{"streamId": r["id"], "name": r["name"], "type": r["kind"], "members": r["members"],
                 "lastSeq": r["lastSeq"]} for r in await _rooms_for(s, bot)]


@app.get("/streams/{stream_id}/messages", tags=["bot"])
async def stream_messages(stream_id: str, after: int = 0, limit: int = Query(100, le=1000),
                          bot: User = Depends(bot_user("streams:read"))) -> dict:
    async with ctx.session() as s:
        await _member_or_404(s, stream_id, bot)
        return await _page(s, stream_id, None, after, limit)


@app.websocket("/streams/subscribe")
async def bot_ws(ws: WebSocket) -> None:
    """The bot key goes in ``?token=`` (or ``?key=``)."""
    p = await ctx.gate.websocket(ws, "streams:read")
    if p is None:
        return
    async with ctx.session() as s:
        room_ids = (await s.execute(select(Member.room_id).where(Member.user_id == uid(p.principal)))).scalars().all()
    await ctx.ws_stream(ws, [f"room:{r}" for r in room_ids] + ["rooms"])


@app.post("/chats/{room_id}/messages", tags=["bot"])
async def bot_send(room_id: str, body: SendBody, bot: User = Depends(bot_user("messages:write"))) -> dict:
    async with ctx.session() as s:
        await _member_or_404(s, room_id, bot)
    return await send_and_react(room_id, bot.handle, body.text, body.chips, via="bot", client_msg_id=body.clientMsgId)


@app.post("/compose", tags=["bot"])
async def compose(body: ComposeBody, bot: User = Depends(bot_user("compose"))) -> dict:
    async with ctx.session() as s:
        target = (await s.execute(select(User).where(User.handle == body.user))).scalar()
        if target is None or target.kind != "human":
            raise HTTPException(404, "no such human user")
        await _member_or_404(s, body.roomId, target)
    ctx.hub.publish(f"user:{target.id}", {"type": "compose", "data": {"streamId": body.roomId, "text": body.text,
                                                                       "from": bot.id}})
    return {"ok": True}


# -- admin / scenario verbs -------------------------------------------------------------------------
@app.get("/_admin/credentials", tags=["admin"])
async def credentials() -> dict:
    return {"botHandle": BOT_HANDLE, "botToken": ctx.gate.bootstrap_key(BOT_HANDLE),
            "deskKey": ctx.gate.bootstrap_key("desk")}


@app.get("/_admin/labels", tags=["admin"])
async def labels(after: int = 0, limit: int = Query(1000, le=10_000)) -> list[dict]:
    async with ctx.session() as s:
        rows = await s.execute(select(Label, Message).join(Message, Message.id == Label.message_id)
                               .where(Label.message_id > after).order_by(Label.message_id).limit(limit))
        return [{"messageId": m.id, "streamId": m.room_id, "seq": m.seq, "text": m.text, "label": lab.label,
                 "ts": clock.iso(m.created_at)} for lab, m in rows.all()]


@app.get("/_admin/personas", tags=["admin"])
async def persona_list() -> list[dict]:
    return [{"key": p.key, "handle": p.handle, "displayName": p.display_name, "firm": p.firm, "cpCode": p.cp_code,
             "room": p.room_id, "style": p.style, "mode": _persona_modes.get(p.key, "scripted")}
            for p in P.PERSONAS.values()]


@app.get("/_admin/llm", tags=["admin"])
async def llm_stats() -> dict:
    return llm.stats()


@ctx.verb("persona")
async def verb_persona(args: dict[str, Any]) -> dict:
    """Make a persona act: {persona, action: any_of_these|offer|rfq|borrow_list|ioi|recall|return|internal_borrow|
    injection|chatter|
    locate_blast (a stray locate: those belong to sales and the API)|auto, ...}"""
    return await persona_act(args["persona"], args.get("action", "auto"), args)


@ctx.verb("say")
async def verb_say(args: dict[str, Any]) -> dict:
    """Post raw text: {as: handle, room, text}"""
    return await send_and_react(args["room"], args["as"], args["text"], args.get("chips"),
                                via="persona" if args["as"] not in (h for h, _ in OUR_HUMANS) else "human")


@ctx.verb("persona_mode")
async def verb_mode(args: dict[str, Any]) -> dict:
    """Switch a persona between scripted and llm: {persona, mode}"""
    if args["mode"] not in ("scripted", "llm"):
        raise HTTPException(422, "mode must be scripted or llm")
    keys = list(P.PERSONAS) if args.get("persona") in (None, "*") else [args["persona"]]
    for k in keys:
        _persona_modes[k] = args["mode"]
    return {"modes": _persona_modes, "llm": llm.stats()}


async def _ambient_tick() -> None:
    if not _ambient["on"]:
        return
    r = rng.stream("ib", "ambient")
    # Re-arm first so neither a skipped persona nor a failure ends the background traffic.
    ctx.scheduler.after(_ambient["every_s"] * r.uniform(0.5, 1.5), "ambient", _ambient_tick)
    p = r.choice([p for p in P.PERSONAS.values() if p.style != "redteam"])
    await persona_act(p.key, "auto", {})


@ctx.verb("ambient")
async def verb_ambient(args: dict[str, Any]) -> dict:
    """Background persona traffic: {on: bool, everyS: seconds}"""
    _ambient.update(on=bool(args.get("on", True)), every_s=float(args.get("everyS", 240)))
    ctx.scheduler.cancel("ambient")
    if _ambient["on"]:
        ctx.scheduler.after(_ambient["every_s"], "ambient", _ambient_tick)
    return dict(_ambient)
