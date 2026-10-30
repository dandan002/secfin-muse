from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, Float, Identity, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mockcore.db import make_base

Base = make_base("wematch")


class DealerLine(Base):
    """Simulated dealers' interest lines the optimiser matches against."""

    __tablename__ = "dealer_lines"
    __table_args__ = (Index("ix_dealer_inst_side", "instrument_id", "side"),)
    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    dealer: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))  # LEND | BORROW (dealer's side)
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_min: Mapped[float] = mapped_column(Float)  # dealer will lend at >= / borrow at <= fee_max
    fee_max: Mapped[float] = mapped_column(Float)
    product: Mapped[str] = mapped_column(String(8), default="SBL")


class InterestList(Base):
    __tablename__ = "lists"
    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    list_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    product: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))  # ACTIVE | WITHDRAWN | COMPLETED
    lines: Mapped[int] = mapped_column(Integer)
    rejected_lines: Mapped[int] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    client_ref: Mapped[str | None] = mapped_column(String(64), unique=True)
    auto_accept: Mapped[bool] = mapped_column(default=False)


class ListLine(Base):
    __tablename__ = "list_lines"
    __table_args__ = (Index("ix_list_lines_list", "list_id"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    list_id: Mapped[str] = mapped_column(String(32))
    line_no: Mapped[int] = mapped_column(Integer)
    instrument_id: Mapped[str] = mapped_column(String(16))
    identifier: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))  # our side
    qty: Mapped[int] = mapped_column(BigInteger)
    matched_qty: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_limit: Mapped[float] = mapped_column(Float)  # borrow: max fee we pay; lend: min fee we accept
    min_qty: Mapped[int] = mapped_column(BigInteger, default=0)
    term: Mapped[str] = mapped_column(String(8))
    collateral_type: Mapped[str] = mapped_column(String(8))
    collateral_ccy: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16))  # OPEN | PARTIAL | FILLED | WITHDRAWN


class Match(Base):
    __tablename__ = "matches"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    match_id: Mapped[str] = mapped_column(String(32), unique=True)
    list_id: Mapped[str] = mapped_column(String(32), index=True)
    line_id: Mapped[int] = mapped_column(BigInteger)
    dealer: Mapped[str] = mapped_column(String(32))
    dealer_line_id: Mapped[int] = mapped_column(Integer)
    instrument_id: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))  # our side
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)  # current proposal on the table
    proposed_by: Mapped[str] = mapped_column(String(8))  # US | DEALER | ENGINE
    status: Mapped[str] = mapped_column(String(16), index=True)  # PROPOSED | AGREED | REJECTED | EXPIRED
    our_accept: Mapped[bool] = mapped_column(default=False)
    dealer_accept: Mapped[bool] = mapped_column(default=False)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    term: Mapped[str] = mapped_column(String(8))
    collateral_type: Mapped[str] = mapped_column(String(8))
    collateral_ccy: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[Any]] = mapped_column(default=list)


class Loan(Base):
    __tablename__ = "loans"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    loan_id: Mapped[str] = mapped_column(String(32), unique=True)
    match_id: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(16))
    isin: Mapped[str] = mapped_column(String(12))
    side: Mapped[str] = mapped_column(String(8))
    counterparty: Mapped[str] = mapped_column(String(32))
    qty_original: Mapped[int] = mapped_column(BigInteger)
    qty_open: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    ccy: Mapped[str] = mapped_column(String(3))
    term: Mapped[str] = mapped_column(String(8))
    collateral_type: Mapped[str] = mapped_column(String(8))
    collateral_ccy: Mapped[str] = mapped_column(String(3))
    trade_date: Mapped[date] = mapped_column(Date)
    settle_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))  # OPEN | RETURNED
    pending_rerate_bps: Mapped[float | None] = mapped_column(Float)
    # The lender's recall on a loan we borrowed: shares owed back by recall_due (cleared as we return them).
    recall_qty: Mapped[int | None] = mapped_column(BigInteger)
    recall_due: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[Any]] = mapped_column(default=list)
    note: Mapped[str | None] = mapped_column(Text)
