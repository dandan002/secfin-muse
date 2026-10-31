export type Tier = "GC" | "WARM" | "HOT";

export interface Instrument {
  id: string;
  ticker: string;
  composite: string;
  name: string;
  country: string;
  ccy: string;
  sector: string;
  isin: string;
  sedol: string;
  cusip: string | null;
  figi: string;
  shsOut: number;
  adv20d: number;
  siPct: number;
  thresholdFlag: boolean;
  restricted: boolean;
  lastPx: number;
  feeBps: number;
  utilPct: number;
  tier: Tier;
  etb: boolean;
  daysToCover: number;
}

export interface MarketRow {
  instrumentId: string;
  composite: string;
  name: string;
  isin: string;
  ccy: string;
  country: string;
  lastPx: number;
  feeBps: number;
  utilPct: number;
  tier: Tier;
  etb: boolean;
  thresholdFlag: boolean;
  restricted: boolean;
  venueA: { qty: number; fee: number | null; lenders: number } | null;
  venueB: { qty: number; fee: number | null } | null;
  internal: { qty: number; books: number; costBps: number | null } | null;
  supply: number;
  held: number;
  available: number;
  availableBy: { internal: number; street: number };
  bestFeeBps: number | null;
  lastFeeBps: number | null;
  trend: number[];
  prints?: { venue: string; qty: number; feeBps: number; ts: string }[];
}

export type LocateStatus = "RECEIVED" | "APPROVED" | "PARTIAL" | "REJECTED" | "EXPIRED" | "USED" | "RELEASED" | "INVALID";

export interface Locate {
  id: string;
  requestRef: string;
  lineNo: number;
  counterparty: string;
  identifier: string;
  instrumentId: string | null;
  qtyRequested: number;
  qtyApproved: number;
  qtyUsed: number;
  status: LocateStatus;
  route: "AUTO" | "REVIEW" | "HOLD";
  routeReasons: string[];
  tier: Tier | null;
  feeBps: number | null; // our quote until decided, then the agreed fee
  feeLimitBps: number | null; // the most the client said it would pay
  decidedBy: string | null;
  rationale: string | null;
  channel: string;
  regShoBasis: string | null;
  replyStatus: string | null;
  error: string | null;
  receivedAt: string;
  decidedAt: string | null;
  enteredAt: string | null; // when the locate was granted
  expiresAt: string | null; // default the close
  instrument?: { composite: string; name: string; isin: string; tier: Tier; feeBps: number; utilPct: number; etb: boolean };
  available?: number;
  availableBy?: { internal: number; street: number };
  suggestedQty?: number;
  feeFloorBps?: number;
  preBorrow?: boolean; // Rule 204(b): only shares we hold can be set aside
  counterpartyRoom?: { code: string; kind: string; creditLimitUsd: number; tradesUsd: number; locatesUsd: number;
    inThisName: number; maxQty: number; failing: string[] };
}

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
  blocking: boolean;
}

export interface Order {
  id: string;
  side: "BORROW" | "LEND";
  type: string;
  instrumentId: string;
  composite: string | null;
  quantity: number;
  feeBps: number;
  counterparty: string | null;
  venue: string;
  venueOrderId: string | null;
  routeReason: string;
  status: string;
  filledQty: number;
  counterFeeBps: number | null;
  counterQty: number | null;
  rejectReason: string | null;
  basketId: string | null; // one line of a basket: a Venue B list shared with other names
  lineNo: number | null;
  checks: Check[];
  createdBy: string;
  settlementDate: string;
  legalAgreement: string;
  createdAt: string;
  updatedAt: string;
}

export interface Trade {
  id: string;
  orderId: string | null;
  venue: string;
  venueTradeId: string;
  uti: string | null;
  side: string;
  composite: string | null;
  counterparty: string;
  quantity: number;
  feeBps: number;
  price: number;
  ccy: string;
  notional: number;
  settlementDate: string;
  contractId: string | null;
  compareStatus: string;
  diffs: { field: string; ours: unknown; processor: unknown }[];
  breakId: string | null;
  openQty: number;
  loanStatus: "OPEN" | "CLOSED";
  pendingRerateBps: number | null;
  recalledQty: number;
  recallDue: string | null;
  recallRef: string | null;
  recallStatus: "RECALLED" | "RETURNING" | "RETURN_FAILING" | "RETURN_NOT_SENT" | null;
  boughtIn: number; // shares the lender bought in on a recall we didn't return
  lifecycle: { ts: string; event: string; by: string; quantity?: number; fromBps?: number; toBps?: number; feeBps?: number; callAmount?: number; error?: string }[];
  settleStatus: string | null;
  settleRef: string | null;
  reg10c1a: Record<string, unknown> | null;
  executedAt: string;
  bookedAt: string | null;
}

export interface Action {
  id: string;
  kind: string;
  title: string;
  payload: Record<string, unknown>;
  original: Record<string, unknown> | null;
  proposedBy: string;
  rationale: string | null;
  riskFlags: string[];
  status: string;
  approver: string | null;
  result: Record<string, unknown> | null;
  transcript: unknown[];
  createdAt: string;
}

export interface Counterparty {
  code: string;
  name: string;
  kind: string;
  region: string;
  ngtCode: string;
  lei: string;
  creditLimitUsd: number;
  agreement: string;
}

export interface Preview {
  instrument: Instrument;
  counterparty: string | null;
  venue: string;
  type: string;
  routeReason: string;
  terms: Record<string, string | number>;
  notional: number;
  lastFeeBps: number | null;
  checks: Check[];
}

export interface PasteRow {
  rowNo: number;
  identifier: string | null;
  quantity: number | null;
  feeBps: number | null;
  counterparty: string | null;
  confidence: Record<string, number>;
  error: string | null;
  extra: Record<string, string>;
  source: string;
  instrument: { id: string; composite: string; name: string; isin: string; ccy: string } | null;
}

export interface PastePreview {
  id: string;
  kind: string;
  columns: { index: number; name: string; role: string }[];
  rows: PasteRow[];
  unparsed: string[];
  resolved: number;
  total: number;
}

export interface Status {
  clock: { now: string; nowLocal: string; mode: string; speed: number; paused: boolean; synced: boolean };
  systems: { system: string; label: string; url: string; up: boolean }[];
  sync: Record<string, { ok: boolean | null; error: string | null; lastRun: string | null }>;
  agent: { mode: string; budgetUsd: number; spentUsd: number; keyConfigured: boolean; models: Record<string, string> };
  killSwitch: { on: boolean; by: string | null; at: string | null };
  policy: { autoApproveEtb: boolean; locateNameCap: number };
}

export interface AuditEvent {
  id: number;
  ts: string;
  entity: string;
  entityId: string;
  type: string;
  actor: string;
  after: Record<string, unknown> | null;
}

export type ChatIntent = "RFQ" | "OFFER" | "IOI" | "QUOTE" | "COUNTER" | "ACCEPT" | "DECLINE" | "RECALL" | "RETURN"
  | "INTERNAL_BORROW" | "INJECTION" | "LOCATE" | "CHATTER" | "UNKNOWN";

export interface ChatLine {
  identifier: string | null;
  instrumentId: string | null;
  quantity: number | null;
  feeBps: number | null;
  rate?: "GC" | null;
  error?: string | null;
}

export interface ChatMessage {
  id: string;
  streamId: string;
  seq: number;
  sender: string;
  senderName: string | null;
  counterparty: string | null;
  text: string;
  sentAt: string;
  intent: ChatIntent;
  confidence: number;
  parser: string;
  lines: ChatLine[];
  status: "OPEN" | "AGREED" | "OURS" | "FLAGGED" | "UNSURE" | "IGNORED" | "DISMISSED" | "ACTIONED";
  side: "US" | "THEM";
  refersTo: number | null;
  ourSide: "LEND" | "BORROW" | null;
  note: string | null;
  receivedAt: string;
}

export interface ShortListLine {
  instrumentId: string;
  composite: string;
  name: string | null;
  ccy: string | null;
  tier: Tier;
  etb: boolean;
  feeBps: number;
  lastPx: number;
  clientShort: number;
  soldToday: number;
  coveredToday: number;
  clients: { counterparty: string; short: number; sold: number; covered: number }[];
  box: number;
  unbooked: number;
  fails: number;
  recalled: number;
  cover: number;
  shortfall: number;
  working: number;
  drafted: { actionId: string; kind: string; title: string }[];
  uncovered: number;
  uncoveredNotional: number;
  returnedSinceBox: number;
  excess: number;
  excessNotional: number;
  excessCostUsd: number;
  returning: { actionId: string; title: string; quantity: number }[];
  toReturn: number;
  need: number; // client shorts, or the CNS fails if they're more (bought back since)
  cnsFails: number; // client shorts failed at CNS, not delivered yet
  closeOutBy: string | null; // Rule 204: the soonest close-out still open (UTC)
  preBorrow: boolean; // a close-out was missed: Rule 204(b) until the shares deliver
  status: "SHORT" | "WORKING" | "COVERED" | "EXCESS";
}

/** A dividend or shareholder vote ahead in a name we borrow or the clients are short. */
export interface CorpActionExposure {
  eventId: string;
  instrumentId: string;
  composite: string;
  kind: "DIVIDEND" | "AGM" | "SPECIAL_MEETING";
  label: string;
  recordDate: string;
  exDate: string | null;
  amount: number | null;
  ccy: string;
  recallP: number; // how likely a lender recalls for this kind of event
  noticeDate: string; // lenders send recalls from here
  dueDate: string;
  daysToRecord: number;
  noticeDue: boolean;
  lenders: { tradeId: string; counterparty: string; openQty: number; recalledQty: number; atRisk: number;
    recalledForThis: boolean }[];
  borrowed: number;
  atRisk: number;
  expectedRecall: number;
  clientShort: number;
  cover: number;
  shortIfAllRecalled: number;
  shortIfExpected: number;
  atRiskNotional: number;
  manufacturedUsd: number | null; // a dividend the clients short over the record date owe
}

/** Shares bought in against us: a lender's on an overdue recall, or the buyer's at CNS on a client fail. */
export interface BuyIn {
  id: string;
  kind: "RECALL" | "CNS";
  origin: "processor" | "venue_b" | "cns";
  tradeId: string | null;
  failId: string | null;
  counterparty: string; // the lender, or (CNS) the client it's charged to
  instrumentId: string;
  composite: string | null;
  quantity: number;
  price: number;
  ccy: string;
  amountDue: number; // what we owe (negative: owed to us)
  amountDueUsd: number;
  boughtInAt: string;
  // TO_PAY / TO_RECEIVE (a card at the gate), INSTRUCTED, SETTLED at the custodian; NONE; CNS (NSCC takes it)
  cashStatus: "TO_PAY" | "TO_RECEIVE" | "INSTRUCTED" | "SETTLED" | "NONE" | "CNS" | null;
  cashRef: string | null;
  chargeStatus: "TO_CHARGE" | "CHARGED" | "NONE" | null; // a CNS buy-in's cost, charged on to the client
  chargeRef: string | null;
}

/** A client's short that failed to deliver at CNS, and where its Rule 204 close-out stands. */
export interface DeliveryFail {
  id: string;
  counterparty: string;
  instrumentId: string;
  composite: string | null;
  settlementDate: string;
  quantity: number;
  openQty: number;
  status: "OPEN" | "CLOSED_OUT" | "DELIVERED" | "BOUGHT_IN";
  missed: boolean;
  preBorrow: boolean;
  deadline: string;
  closedOutAt: string | null;
  deliveredAt: string | null;
}

export interface ShortList {
  tradeDate: string | null;
  settlementDate: string | null;
  lines: ShortListLine[];
  summary: { names?: number; short?: number; working?: number; covered?: number; clientShort?: number;
    shortfall?: number; uncovered?: number; uncoveredNotional?: number; excess?: number; excessNotional?: number;
    excessCostUsd?: number; toReturn?: number; cnsFails?: number; closeOut?: number; preBorrow?: number };
}
