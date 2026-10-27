from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, Float, Identity, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mockcore.db import make_base

Base = make_base("ngt")


class Inventory(Base):
    """Simulated lender supply on the venue."""

    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("lender", "instrument_id"), Index("ix_inv_inst", "instrument_id"))
    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    lender: Mapped[str] = mapped_column(String(32))  # our counterparty code
    instrument_id: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_mult: Mapped[float] = mapped_column(Float)  # lender's markup over the fair fee
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    participant: Mapped[str] = mapped_column(String(16))  # venue participant code that sent it
    source: Mapped[str] = mapped_column(String(8))  # REST | FIX
    fix_session: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(12))  # BID | OFFER | FIRM_BID
    side: Mapped[str] = mapped_column(String(8))  # BORROW | LEND (participant's side)
    instrument_id: Mapped[str] = mapped_column(String(16))
    isin: Mapped[str] = mapped_column(String(12))
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)
    counterparty: Mapped[str | None] = mapped_column(String(32))  # our cp code of the contra (lender)
    counter_fee_bps: Mapped[float | None] = mapped_column(Float)
    counter_qty: Mapped[int | None] = mapped_column(BigInteger)
    counters: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), index=True)
    term: Mapped[str] = mapped_column(String(8))
    term_end: Mapped[date | None] = mapped_column(Date)
    collateral_type: Mapped[str] = mapped_column(String(8))
    collateral_ccy: Mapped[str] = mapped_column(String(3))
    margin_pct: Mapped[float] = mapped_column(Float)
    settle_date: Mapped[date] = mapped_column(Date)
    legal_agreement: Mapped[str] = mapped_column(String(16))
    booking_entity: Mapped[str] = mapped_column(String(64))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[Any]] = mapped_column(default=list)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(32), unique=True)
    uti: Mapped[str] = mapped_column(String(52), unique=True)
    order_id: Mapped[str] = mapped_column(String(32), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[str] = mapped_column(String(16))
    isin: Mapped[str] = mapped_column(String(12))
    lender: Mapped[str] = mapped_column(String(32))
    borrower: Mapped[str] = mapped_column(String(32))
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    ccy: Mapped[str] = mapped_column(String(3))
    loan_value: Mapped[float] = mapped_column(Float)
    term: Mapped[str] = mapped_column(String(8))
    term_end: Mapped[date | None] = mapped_column(Date)
    collateral_type: Mapped[str] = mapped_column(String(8))
    collateral_ccy: Mapped[str] = mapped_column(String(3))
    margin_pct: Mapped[float] = mapped_column(Float)
    settle_date: Mapped[date] = mapped_column(Date)
    legal_agreement: Mapped[str] = mapped_column(String(16))
    booking_entity: Mapped[str] = mapped_column(String(64))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Print(Base):
    """Anonymous street prints (ours and simulated market activity) for last-traded-fee."""

    __tablename__ = "prints"
    __table_args__ = (Index("ix_prints_inst_ts", "instrument_id", "ts"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ours: Mapped[bool] = mapped_column(default=False)


class FixSession(Base):
    __tablename__ = "fix_sessions"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # "<sender>-<target>"
    next_in: Mapped[int] = mapped_column(Integer, default=1)
    next_out: Mapped[int] = mapped_column(Integer, default=1)
    logged_on: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FixMessage(Base):
    __tablename__ = "fix_messages"
    __table_args__ = (Index("ix_fixmsg_session_dir_seq", "session_id", "direction", "seq"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(3))  # IN | OUT
    seq: Mapped[int] = mapped_column(Integer)
    msg_type: Mapped[str] = mapped_column(String(4))
    raw: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
