"""The desk's system of record (spec: *Data model and API contracts*).

Records keep CDM-shaped JSON where a standard shape exists, plus indexed columns for the grids.
``events`` is append-only: every state change lands there, so a day can be audited and replayed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Instrument(Base):
    """Static reference data, loaded from the security-master feed. Live fields are computed on read."""

    __tablename__ = "instruments"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    mic: Mapped[str] = mapped_column(String(4))
    composite: Mapped[str] = mapped_column(String(24), index=True)  # "AAPL US"
    name: Mapped[str] = mapped_column(String(128), index=True)
    country: Mapped[str] = mapped_column(String(2))
    ccy: Mapped[str] = mapped_column(String(3))
    sector: Mapped[str] = mapped_column(String(48))
    cfi: Mapped[str] = mapped_column(String(6))
    isin: Mapped[str] = mapped_column(String(12), unique=True)
    sedol: Mapped[str] = mapped_column(String(7), index=True)
    cusip: Mapped[str | None] = mapped_column(String(9), index=True)
    figi: Mapped[str] = mapped_column(String(12), index=True)
    shs_out: Mapped[int] = mapped_column(BigInteger)
    free_float_pct: Mapped[float] = mapped_column(Float)
    adv_20d: Mapped[int] = mapped_column(BigInteger)
    adv_90d: Mapped[int] = mapped_column(BigInteger)
    si_pct: Mapped[float] = mapped_column(Float)
    index_name: Mapped[str | None] = mapped_column(String(32))
    threshold_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    next_ex_div: Mapped[str | None] = mapped_column(String(10))


class Counterparty(Base):
    __tablename__ = "counterparties"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(4))
    ngt_code: Mapped[str] = mapped_column(String(8), index=True)
    wematch_entity: Mapped[str] = mapped_column(String(32))
    lei: Mapped[str] = mapped_column(String(20), unique=True)
    bic: Mapped[str] = mapped_column(String(11))
    credit_limit_usd: Mapped[int] = mapped_column(BigInteger)
    agreement: Mapped[str] = mapped_column(String(16))
    collateral_schedule: Mapped[str] = mapped_column(String(128))
    persona: Mapped[str | None] = mapped_column(String(32))


class Availability(Base):
    """Supply by source: INTERNAL inventory, VENUE_A and VENUE_B feeds."""

    __tablename__ = "availability"
    __table_args__ = (UniqueConstraint("instrument_id", "source"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(12))
    qty: Mapped[int] = mapped_column(BigInteger)
    indicative_fee_bps: Mapped[float | None] = mapped_column(Float)
    lenders: Mapped[int] = mapped_column(Integer, default=0)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InternalPosition(Base):
    """Our open loan positions per book, from the processor. Netted across books into INTERNAL availability."""

    __tablename__ = "internal_positions"
    instrument_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    book: Mapped[str] = mapped_column(String(64), primary_key=True)  # the processor's booking entity
    borrowed: Mapped[int] = mapped_column(BigInteger, default=0)
    lent: Mapped[int] = mapped_column(BigInteger, default=0)
    avg_borrow_fee_bps: Mapped[float | None] = mapped_column(Float)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Print(Base):
    """Last traded fees seen on a venue (for last-fee, trend and fat-finger checks)."""

    __tablename__ = "prints"
    __table_args__ = (Index("ix_prints_inst_ts", "instrument_id", "ts"), UniqueConstraint("venue", "venue_ref"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(12))
    venue_ref: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Locate(Base):
    """One locate line. ``request_ref`` groups lines that arrived together (a gateway request, a chat message)."""

    __tablename__ = "locates"
    __table_args__ = (Index("ix_locates_status", "status"),
                      UniqueConstraint("source_channel", "request_ref", "line_no"))
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    request_ref: Mapped[str] = mapped_column(String(64))
    line_no: Mapped[int] = mapped_column(Integer)
    requester_cp: Mapped[str] = mapped_column(String(16), index=True)
    identifier: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[str | None] = mapped_column(String(16), index=True)
    qty_req: Mapped[int] = mapped_column(BigInteger)
    qty_approved: Mapped[int] = mapped_column(BigInteger, default=0)
    qty_used: Mapped[int] = mapped_column(BigInteger, default=0)
    # RECEIVED | APPROVED | PARTIAL | REJECTED | EXPIRED | USED | RELEASED | INVALID
    status: Mapped[str] = mapped_column(String(12))
    route: Mapped[str] = mapped_column(String(8))  # AUTO | REVIEW | HOLD: who the rules engine says decides
    route_reasons: Mapped[list[Any]] = mapped_column(default=list)
    tier_at_decision: Mapped[str | None] = mapped_column(String(8))
    # The fee: our quote until the line is decided, then the fee agreed with the client (what it pays on its shorts).
    fee_bps: Mapped[float | None] = mapped_column("fee_ind_bps", Float)
    fee_limit_bps: Mapped[float | None] = mapped_column(Float)  # the most the client said it would pay, if it did
    decided_by: Mapped[str | None] = mapped_column(String(48))  # policy:<name> | agent:<model> | trader:<user>
    rationale: Mapped[str | None] = mapped_column(Text)
    source_channel: Mapped[str] = mapped_column(String(8))  # API | CHAT | PASTE | FILE | MANUAL | INTERNAL
    source_ref: Mapped[str | None] = mapped_column(String(128))
    reg_sho_basis: Mapped[str | None] = mapped_column(String(256))
    reply_status: Mapped[str | None] = mapped_column(String(12))  # PENDING | SENT | FAILED | N/A
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # the locate's entry time
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # default: the close


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_status", "status"),)
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    side: Mapped[str] = mapped_column(String(8))  # BORROW | LEND
    type: Mapped[str] = mapped_column(String(12))  # BID | OFFER | FIRM_BID | INTEREST_LIST
    instrument_id: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)
    cp_code: Mapped[str | None] = mapped_column(String(16))
    venue: Mapped[str] = mapped_column(String(12))  # VENUE_A | VENUE_B
    venue_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    route_reason: Mapped[str] = mapped_column(Text)
    collateral_type: Mapped[str] = mapped_column(String(8))
    collateral_ccy: Mapped[str] = mapped_column(String(3))
    margin_pct: Mapped[float] = mapped_column(Float)
    term: Mapped[str] = mapped_column(String(8))
    term_end: Mapped[date | None] = mapped_column(Date)
    settle_date: Mapped[date] = mapped_column(Date)
    legal_agreement: Mapped[str] = mapped_column(String(16))
    booking_entity: Mapped[str] = mapped_column(String(64))
    # NEW | SENT | PENDING | COUNTERED | EXECUTED | CANCELLED | EXPIRED | REJECTED | FAILED
    status: Mapped[str] = mapped_column(String(12))
    filled_qty: Mapped[int] = mapped_column(BigInteger, default=0)
    counter_fee_bps: Mapped[float | None] = mapped_column(Float)
    counter_qty: Mapped[int | None] = mapped_column(BigInteger)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    checks: Mapped[list[Any]] = mapped_column(default=list)
    created_by: Mapped[str] = mapped_column(String(48))
    action_id: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(24), index=True)
    venue: Mapped[str] = mapped_column(String(12))
    venue_trade_id: Mapped[str] = mapped_column(String(64), unique=True)
    uti: Mapped[str | None] = mapped_column(String(52))
    side: Mapped[str] = mapped_column(String(8))
    instrument_id: Mapped[str] = mapped_column(String(16))
    cp_code: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(BigInteger)
    fee_bps: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    ccy: Mapped[str] = mapped_column(String(3))
    settle_date: Mapped[date] = mapped_column(Date)
    venue_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cdm_json: Mapped[dict[str, Any] | None] = mapped_column()
    sfp_contract_id: Mapped[str | None] = mapped_column(String(32))
    # UNBOOKED | BOOKED | MATCHED | UNMATCHED | AMENDED | FAILED
    compare_status: Mapped[str] = mapped_column(String(12), default="UNBOOKED")
    compare_diffs: Mapped[list[Any]] = mapped_column(default=list)
    break_id: Mapped[str | None] = mapped_column(String(32))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The loan after booking: returns and recalls reduce open_qty; OPEN | CLOSED.
    open_qty: Mapped[int | None] = mapped_column(BigInteger)
    loan_status: Mapped[str | None] = mapped_column(String(8), default="OPEN")
    pending_rerate_bps: Mapped[float | None] = mapped_column(Float)  # asked of the venue, not yet agreed
    # A lender's recall on a borrow: shares we owe back by recall_due (cleared as we return them).
    recalled_qty: Mapped[int | None] = mapped_column(BigInteger)
    recall_due: Mapped[date | None] = mapped_column(Date)
    recall_ref: Mapped[str | None] = mapped_column(String(32))
    returns_settling: Mapped[int | None] = mapped_column(Integer)  # return/recall legs not settled yet
    lifecycle: Mapped[list[Any] | None] = mapped_column(default=list)  # [{ts, event, quantity, feeBps, by}]
    reg_10c1a: Mapped[dict[str, Any] | None] = mapped_column()  # SEC Rule 10c-1a fields, captured at booking
    # Settlement of the loan leg at the custodian: PENDING | MATCHED | UNMATCHED | FAILING | SETTLED | REJECTED
    settle_status: Mapped[str | None] = mapped_column(String(12))
    settle_ref: Mapped[str | None] = mapped_column(String(32))


class Action(Base):
    """A proposed external side effect, waiting at the approval gate."""

    __tablename__ = "actions"
    __table_args__ = (Index("ix_actions_status", "status"),)
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # locate_decision | chat_send | order | booking | recall | amend
    title: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    original: Mapped[dict[str, Any] | None] = mapped_column()  # the request it answers, for the diff
    proposed_by: Mapped[str] = mapped_column(String(48))
    rationale: Mapped[str | None] = mapped_column(Text)
    risk_flags: Mapped[list[Any]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(12))  # PENDING | APPROVED | REJECTED | EXECUTED | FAILED | TAKEN_OVER
    approver: Mapped[str | None] = mapped_column(String(48))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column()
    transcript: Mapped[list[Any]] = mapped_column(default=list)  # source, parse, tools called, model output
    source_ref: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    """One agent task. Its working state lives in the LangGraph checkpointer under ``id`` (the thread id)."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_status", "status"),)
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    task: Mapped[str] = mapped_column(String(32))  # review_locate
    subject: Mapped[str] = mapped_column(String(64), index=True)  # what it is about, e.g. the locate id
    status: Mapped[str] = mapped_column(String(12))  # RUNNING | WAITING | DONE | FAILED
    model: Mapped[str | None] = mapped_column(String(48))  # the model holding the case now
    action_id: Mapped[str | None] = mapped_column(String(24), index=True)  # the gate action it waits on
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PasteSession(Base):
    __tablename__ = "paste_sessions"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # TABLE | BLOOMBERG | CHAT | EMPTY
    raw: Mapped[str] = mapped_column(Text)
    rows: Mapped[list[Any]] = mapped_column(default=list)
    columns: Mapped[list[Any]] = mapped_column(default=list)
    applied_as: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_entity", "entity", "entity_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entity: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(32))
    before: Mapped[dict[str, Any] | None] = mapped_column()
    after: Mapped[dict[str, Any] | None] = mapped_column()
    actor: Mapped[str] = mapped_column(String(48))


class ChatMessage(Base):
    """One message read from Desk Chat, what the parser made of it, and what the desk did with it."""

    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_status", "status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # the chat server's message id
    stream_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str] = mapped_column(String(64))  # handle
    sender_name: Mapped[str | None] = mapped_column(String(128))
    counterparty: Mapped[str | None] = mapped_column(String(16), index=True)
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    side: Mapped[str] = mapped_column(String(4), default="THEM")  # THEM (the counterparty) | US (our desk)
    # RFQ | OFFER | IOI | QUOTE | COUNTER | ACCEPT | DECLINE | RECALL | RETURN | INTERNAL_BORROW | INJECTION | LOCATE
    # | CHATTER | UNKNOWN
    intent: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    parser: Mapped[str] = mapped_column(String(32))  # rules | model:<name> | trader:<user>
    lines: Mapped[list[Any]] = mapped_column(default=list)  # [{identifier, instrumentId, quantity, feeBps, rate}]
    refers_to: Mapped[int | None] = mapped_column(Integer)  # seq of the message this one answers, in the same room
    our_side: Mapped[str | None] = mapped_column(String(6))  # LEND | BORROW, if it goes ahead
    # OPEN | AGREED (a trade agreed in chat) | OURS | FLAGGED | UNSURE | IGNORED (locates, chatter) | DISMISSED
    status: Mapped[str] = mapped_column(String(12))
    note: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClientShort(Base):
    """One client's short position in one name at a close, from the stock record; it settles the next business day
    and the desk has to deliver it. The day's sales and buy-backs ride along."""

    __tablename__ = "client_shorts"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    counterparty: Mapped[str] = mapped_column(String(16), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(16), primary_key=True, index=True)
    short_qty: Mapped[int] = mapped_column(BigInteger)
    sold_qty: Mapped[int] = mapped_column(BigInteger, default=0)
    covered_qty: Mapped[int] = mapped_column(BigInteger, default=0)
    settle_date: Mapped[date] = mapped_column(Date)


class BuyIn(Base):
    """Shares bought in against us. A lender's, on a recall we didn't return in time: the borrow closes for them and
    we pay what the lender's purchase cost over the collateral it kept. Or the buyer's at CNS, on a client fail we
    didn't deliver: the fail closes, we pay the purchase price over the sale price, charged on to the client (Rule
    204(d)), and the client's short is covered by it."""

    __tablename__ = "buy_ins"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # the processor's, the venue's or the gateway's id
    kind: Mapped[str] = mapped_column(String(8))  # RECALL | CNS
    origin: Mapped[str] = mapped_column(String(12))  # processor | venue_b | cns
    trade_id: Mapped[str | None] = mapped_column(String(24), index=True)
    fail_id: Mapped[str | None] = mapped_column(String(32))
    counterparty: Mapped[str] = mapped_column(String(16))  # the lender, or (CNS) the client it's charged to
    instrument_id: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[float] = mapped_column(Float)
    ccy: Mapped[str] = mapped_column(String(3))
    amount_due: Mapped[float] = mapped_column(Float)  # what we owe for it (negative: owed to us)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)  # the source's record as received
    bought_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeliveryFail(Base):
    """A client's short that failed to deliver at CNS (from the gateway's books). Reg SHO Rule 204: we close it out,
    by borrowing or buying the shares, before regular trading opens the business day after it failed. Missed, the
    name is pre-borrow (204(b)) until the shares are delivered: no short in it without borrowing first."""

    __tablename__ = "delivery_fails"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # the gateway's fail id
    counterparty: Mapped[str] = mapped_column(String(16))  # the client whose short failed (204(d) allocation)
    instrument_id: Mapped[str] = mapped_column(String(16), index=True)
    settle_date: Mapped[date] = mapped_column(Date)
    qty: Mapped[int] = mapped_column(BigInteger)
    open_qty: Mapped[int] = mapped_column(BigInteger)  # not delivered yet
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # the close-out, by the open
    # OPEN | CLOSED_OUT (borrowed in time, the shares not delivered yet) | DELIVERED | BOUGHT_IN (at CNS)
    status: Mapped[str] = mapped_column(String(12), default="OPEN")
    missed: Mapped[bool] = mapped_column(Boolean, default=False)  # not closed out by the deadline: pre-borrow
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deliveries: Mapped[list[Any]] = mapped_column(default=list)  # [{ts, quantity, by}]


class SyncCursor(Base):
    """Where each poller left off (gateway inbox seq, venue trade time...)."""

    __tablename__ = "sync_cursors"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(64))
