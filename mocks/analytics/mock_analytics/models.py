from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, String
from sqlalchemy.orm import Mapped, mapped_column

from mockcore.db import make_base

Base = make_base("analytics")


class Watch(Base):
    """A name on the client's watchlist: it gets the per-name alerts (fee spike, utilization, SI jump)."""

    __tablename__ = "watchlist"
    instrument_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    isin: Mapped[str] = mapped_column(String(12))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    alert_id: Mapped[str] = mapped_column(String(32), unique=True)
    dedupe_key: Mapped[str] = mapped_column(String(96), unique=True)  # kind:instrument:day, one per day
    kind: Mapped[str] = mapped_column(String(16))  # SQUEEZE | FEE_SPIKE | UTIL_HIGH | SI_JUMP
    severity: Mapped[str] = mapped_column(String(8))  # HIGH | MEDIUM
    instrument_id: Mapped[str] = mapped_column(String(16), index=True)
    isin: Mapped[str] = mapped_column(String(12))
    data: Mapped[dict[str, Any]] = mapped_column()
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
