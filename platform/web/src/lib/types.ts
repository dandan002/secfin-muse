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
  status: LocateStatus;
  route: "AUTO" | "REVIEW" | "HOLD";
  routeReasons: string[];
  tier: Tier | null;
  feeIndBps: number | null;
  decidedBy: string | null;
  rationale: string | null;
  channel: string;
  regShoBasis: string | null;
  replyStatus: string | null;
  error: string | null;
  receivedAt: string;
  decidedAt: string | null;
  expiresAt: string | null;
  instrument?: { composite: string; name: string; isin: string; tier: Tier; feeBps: number; utilPct: number; etb: boolean };
  available?: number;
  availableBy?: { internal: number; street: number };
  suggestedQty?: number;
  feeFloorBps?: number;
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
  status: "OPEN" | "AGREED" | "OURS" | "FLAGGED" | "UNSURE" | "IGNORED" | "DISMISSED";
  side: "US" | "THEM";
  refersTo: number | null;
  ourSide: "LEND" | "BORROW" | null;
  note: string | null;
  receivedAt: string;
}
