from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, Float, Identity, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from mockcore.db import make_base

Base = make_base("sfp")


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (Index("ix_contracts_inst", "instrument_id"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    contract_id: Mapped[str] = mapped_column(String(32), unique=True)
    uti: Mapped[str | None] = mapped_column(String(52), index=True)
    venue: Mapped[str | None] = mapped_column(String(32))
    venue_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    client_ref: Mapped[str | None] = mapped_column(String(64), unique=True)  # idempotency
    side: Mapped[str] = mapped_column(String(8))  # our side: BORROW | LEND
    counterparty: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(16))
    isin: Mapped[str] = mapped_column(String(12))
    qty: Mapped[int] = mapped_column(BigInteger)
    open_qty: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    ccy: Mapped[str] = mapped_column(String(3))
    collateral_type: Mapped[str] = mapped_column(String(8))
    collateral_ccy: Mapped[str] = mapped_column(String(3))
    margin_pct: Mapped[float] = mapped_column(Float)
    collateral_value: Mapped[float] = mapped_column(Float)
    term: Mapped[str] = mapped_column(String(8))
    term_end: Mapped[date | None] = mapped_column(Date)
    trade_date: Mapped[date] = mapped_column(Date)
    settle_date: Mapped[date] = mapped_column(Date)
    legal_agreement: Mapped[str] = mapped_column(String(16))
    booking_entity: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # OPEN | CLOSED
    submitted: Mapped[dict[str, Any]] = mapped_column()  # flat fields as received
    cdm: Mapped[dict[str, Any] | None] = mapped_column()  # original CDM-shaped payload, if any
    injected_break: Mapped[dict[str, Any] | None] = mapped_column()  # {"field", "submitted", "booked"}
    booked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[Any]] = mapped_column(default=list)


class RecallNotice(Base):
    """A lender recalling shares from a contract where we borrow: we owe ``outstanding`` back by ``due_date``."""

    __tablename__ = "recall_notices"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    recall_id: Mapped[str] = mapped_column(String(32), unique=True)
    contract_id: Mapped[str] = mapped_column(String(32), index=True)
    counterparty: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(BigInteger)
    outstanding: Mapped[int] = mapped_column(BigInteger)  # not returned yet
    notice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))  # OPEN | RETURNED | BOUGHT_IN
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bought_in_qty: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(16))  # None (the lender's own reasons) | DIVIDEND | AGM | ...
    event_id: Mapped[str | None] = mapped_column(String(32))  # the corporate action, if that's why


class BuyIn(Base):
    """The lender bought in shares we didn't return on a recall: the loan closes for them, and the lender keeps
    their share of our collateral against the cost. ``amount_due`` is what's left for us to pay (negative: the
    collateral was more, and the lender owes us the rest)."""

    __tablename__ = "buy_ins"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    buy_in_id: Mapped[str] = mapped_column(String(32), unique=True)
    recall_id: Mapped[str] = mapped_column(String(32), index=True)
    contract_id: Mapped[str] = mapped_column(String(32), index=True)
    counterparty: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[float] = mapped_column(Float)  # what the lender paid a share
    ccy: Mapped[str] = mapped_column(String(3))
    cost: Mapped[float] = mapped_column(Float)  # qty x price
    collateral_kept: Mapped[float] = mapped_column(Float)
    amount_due: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Mark(Base):
    __tablename__ = "marks"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    contract_id: Mapped[str] = mapped_column(String(32), index=True)
    mark_date: Mapped[date] = mapped_column(Date)
    price: Mapped[float] = mapped_column(Float)
    loan_value: Mapped[float] = mapped_column(Float)
    required_collateral: Mapped[float] = mapped_column(Float)
    posted_collateral: Mapped[float] = mapped_column(Float)
    call_amount: Mapped[float] = mapped_column(Float)  # >0: we must receive (lend) / deliver (borrow) more
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Break(Base):
    __tablename__ = "breaks"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    break_id: Mapped[str] = mapped_column(String(32), unique=True)
    contract_id: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(16))  # COMPARE | EOD
    diffs: Mapped[list[Any]] = mapped_column()
    status: Mapped[str] = mapped_column(String(16))  # OPEN | RESOLVED
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
