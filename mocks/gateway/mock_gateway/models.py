from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Identity, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mockcore.db import make_base

Base = make_base("gateway")


class LocateRequest(Base):
    __tablename__ = "requests"
    __table_args__ = (Index("ix_req_cp_idem", "cp_code", "idempotency_key", unique=True),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    locate_id: Mapped[str] = mapped_column(String(32), unique=True)
    request_id: Mapped[str | None] = mapped_column(String(64))
    cp_code: Mapped[str] = mapped_column(String(32), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    body_hash: Mapped[str] = mapped_column(String(64))
    format: Mapped[str] = mapped_column(String(8))  # CDM | FLAT | CSV
    source: Mapped[str] = mapped_column(String(8))  # API | LOADGEN
    settle_date: Mapped[date | None] = mapped_column(Date)
    callback_url: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(24))  # RECEIVED | DECIDED | PARTIALLY_DECIDED
    forward_status: Mapped[str] = mapped_column(String(16))  # SINK | PENDING | FORWARDED | FAILED
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response: Mapped[dict[str, Any]] = mapped_column()


class LocateLine(Base):
    __tablename__ = "lines"
    __table_args__ = (Index("ix_lines_locate", "locate_id"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    locate_id: Mapped[str] = mapped_column(String(32))
    line_no: Mapped[int] = mapped_column(Integer)
    identifier: Mapped[str] = mapped_column(String(64))
    id_type: Mapped[str | None] = mapped_column(String(8))
    instrument_id: Mapped[str | None] = mapped_column(String(16))
    isin: Mapped[str | None] = mapped_column(String(12))
    tier: Mapped[str | None] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(BigInteger)
    max_fee_bps: Mapped[float | None] = mapped_column(Float)  # the client's limit, if it sent one
    status: Mapped[str] = mapped_column(String(16))  # RECEIVED | INVALID | APPROVED | PARTIAL | REJECTED
    approved_qty: Mapped[int | None] = mapped_column(BigInteger)
    fee_bps: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    reg_sho_basis: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    # Set at the close the locate expires into: how much of it the client shorted (0: unused or went elsewhere).
    used_qty: Mapped[int | None] = mapped_column(BigInteger)
    used_on: Mapped[date | None] = mapped_column(Date)  # the trade date of that close


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    locate_id: Mapped[str] = mapped_column(String(32), index=True)
    url: Mapped[str] = mapped_column(String(512))
    attempt: Mapped[int] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CpReceived(Base):
    """What the simulated counterparties' callback endpoints received from us."""

    __tablename__ = "cp_received"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    cp_code: Mapped[str] = mapped_column(String(32), index=True)
    signature_ok: Mapped[bool] = mapped_column(Boolean)
    body: Mapped[dict[str, Any]] = mapped_column()
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CpOrder(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True)
    cp_code: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[dict[str, Any]] = mapped_column()
    status: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# -- the clients' trading (the prime broker's stock record) ---------------------------------------------
class ShortSale(Base):
    """A client's short sale, done against one of its locates."""

    __tablename__ = "short_sales"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    cp_code: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(BigInteger)
    locate_id: Mapped[str] = mapped_column(String(32))
    line_no: Mapped[int] = mapped_column(Integer)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShortPosition(Base):
    """A client's short position in one name at a close (the stock record), settling the next business day."""

    __tablename__ = "short_positions"
    __table_args__ = (Index("ix_pos_date_cp_inst", "trade_date", "cp_code", "instrument_id", unique=True),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date)
    cp_code: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[str] = mapped_column(String(16))
    short_qty: Mapped[int] = mapped_column(BigInteger)
    sold_qty: Mapped[int] = mapped_column(BigInteger)  # new shorts that day
    covered_qty: Mapped[int] = mapped_column(BigInteger)  # bought back that day


class DayClose(Base):
    """A business day whose close has been run: locates used or not, covers, the stock record written."""

    __tablename__ = "day_closes"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CnsFail(Base):
    """A fail to deliver at CNS: a client's short sale that settled short, owed to the street until we deliver."""

    __tablename__ = "cns_fails"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fail_id: Mapped[str] = mapped_column(String(32), unique=True)
    settle_date: Mapped[date] = mapped_column(Date, index=True)
    cp_code: Mapped[str] = mapped_column(String(32))  # the client whose short it is (Rule 204(d) allocation)
    instrument_id: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(BigInteger)
    outstanding: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(8))  # OPEN | CLOSED
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deliveries: Mapped[list[Any]] = mapped_column(default=list)
