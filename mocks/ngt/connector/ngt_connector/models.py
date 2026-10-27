"""Typed shapes of Venue A's REST API (see ../../API.md). Unknown fields are kept, not dropped."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OrderType = Literal["BID", "OFFER", "FIRM_BID"]
OrderStatus = Literal["PENDING", "COUNTERED", "EXECUTED", "CANCELLED", "EXPIRED", "REJECTED"]
LegalAgreement = Literal["MSLA", "GMSLA_2010", "GMSLA_2018"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class OrderRequest(_Model):
    """A borrow (BID / FIRM_BID) or lend (OFFER) order. The same fields go over REST and FIX."""

    clientOrderId: str = Field(..., max_length=64)
    type: OrderType
    instrument: str = Field(..., description="ISIN, SEDOL, CUSIP, FIGI or 'TICK XX'")
    quantity: int = Field(..., gt=0)
    feeBps: float = Field(..., ge=0)
    counterpartyCode: str | None = Field(None, description="Venue participant code; omit to hit the best lender")
    term: Literal["OPEN", "TERM"] = "OPEN"
    termEndDate: date | None = None
    collateralType: Literal["CASH", "NONCASH"]
    collateralCcy: str = Field(..., min_length=3, max_length=3)
    marginPct: float = Field(..., ge=100, le=150)
    settlementDate: date
    legalAgreement: LegalAgreement
    bookingEntity: str
    ttlSeconds: int | None = Field(None, gt=0, le=86_400)

    @model_validator(mode="after")
    def _term(self) -> OrderRequest:
        if self.term == "TERM" and not self.termEndDate:
            raise ValueError("termEndDate is required for TERM loans")
        return self


class Order(_Model):
    orderId: str
    clientOrderId: str
    type: OrderType
    side: Literal["BORROW", "LEND"]
    status: OrderStatus
    instrumentId: str
    isin: str
    composite: str
    quantity: int
    feeBps: float
    counterpartyCode: str | None = None
    counterpartyName: str | None = None
    counterFeeBps: float | None = None
    counterQuantity: int | None = None
    counters: int = 0
    rejectReason: str | None = None


class Party(_Model):
    code: str
    name: str
    lei: str


class Trade(_Model):
    tradeId: str
    uti: str
    orderId: str
    clientOrderId: str
    instrumentId: str
    isin: str
    lender: Party
    borrower: Party
    quantity: int
    feeBps: float
    price: float
    ccy: str
    loanValue: float
    collateralType: str
    collateralCcy: str
    marginPct: float
    settlementDate: date
    legalAgreement: str
    bookingEntity: str
    executedAt: str


class LenderLine(_Model):
    instrumentId: str
    isin: str
    composite: str
    lenderCode: str
    lenderName: str
    quantity: int
    indicativeFeeBps: float
    asOf: str


class InstrumentAvailability(_Model):
    instrumentId: str
    isin: str
    composite: str
    quantity: int
    lenders: int
    bestFeeBps: float | None
    asOf: str


class Availability(_Model):
    asOf: str
    total: int
    instruments: list[InstrumentAvailability]
    byLender: list[LenderLine] | None = None


class Print(_Model):
    instrumentId: str
    quantity: int
    feeBps: float
    ts: str


class Participant(_Model):
    code: str
    name: str
    lei: str
    role: str
