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
    status: Mapped[str] = mapped_column(String(16))  # RECEIVED | DECIDED | PARTIALLY_DECIDED
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
    status: Mapped[str] = mapped_column(String(16))  # RECEIVED | INVALID | APPROVED | PARTIAL | REJECTED
    approved_qty: Mapped[int | None] = mapped_column(BigInteger)
    fee_bps: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    reg_sho_basis: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


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
