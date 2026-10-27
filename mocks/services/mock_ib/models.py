from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mockcore.db import make_base

Base = make_base("ib")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # deterministic UUID
    handle: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    firm: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # human | persona | bot
    persona: Mapped[str | None] = mapped_column(String(32))
    cp_code: Mapped[str | None] = mapped_column(String(32))
    tz: Mapped[str] = mapped_column(String(64), default="America/New_York")


class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # persistent | direct | group
    tz: Mapped[str] = mapped_column(String(64), default="America/New_York")
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    topic: Mapped[str] = mapped_column(String(256), default="")


class Member(Base):
    __tablename__ = "members"
    room_id: Mapped[str] = mapped_column(ForeignKey("ib.rooms.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("ib.users.id", ondelete="CASCADE"), primary_key=True)
    last_read_seq: Mapped[int] = mapped_column(Integer, default=0)
    folder: Mapped[str | None] = mapped_column(String(64))
    tab_pos: Mapped[int | None] = mapped_column(Integer)  # open as a tab at this position
    muted: Mapped[bool] = mapped_column(default=False)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("room_id", "seq"),
        Index("ix_messages_room_seq", "room_id", "seq"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("ib.rooms.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    sender_id: Mapped[str] = mapped_column(ForeignKey("ib.users.id"))
    text: Mapped[str] = mapped_column(Text)
    chips: Mapped[list[Any]] = mapped_column(default=list)
    mentions: Mapped[list[Any]] = mapped_column(default=list)
    kind: Mapped[str] = mapped_column(String(16), default="text")  # text | system
    via: Mapped[str] = mapped_column(String(16), default="human")  # human | bot | persona
    blast_id: Mapped[str | None] = mapped_column(String(32))
    client_msg_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Label(Base):
    """Gold labels for persona-generated messages (what the parser should extract)."""

    __tablename__ = "labels"
    message_id: Mapped[int] = mapped_column(ForeignKey("ib.messages.id", ondelete="CASCADE"), primary_key=True)
    label: Mapped[dict[str, Any]] = mapped_column()


class PersonaState(Base):
    __tablename__ = "persona_state"
    persona: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column()
