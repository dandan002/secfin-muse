export type User = {
  uuid: string;
  handle: string;
  displayName: string;
  firm: string;
  kind: "human" | "persona" | "bot";
  persona: string | null;
  cpCode: string | null;
  tz: string;
};

export type Chip = {
  type: "instrument";
  instrumentId: string;
  composite: string;
  isin: string;
  quantity: number | null;
  start: number;
  end: number;
};

export type Message = {
  messageId: number;
  streamId: string;
  seq: number;
  sender: User;
  text: string;
  chips: Chip[];
  mentions: string[];
  kind: string;
  via: "human" | "bot" | "persona";
  blastId: string | null;
  clientMsgId: string | null;
  ts: string;
};

export type Room = {
  id: string;
  name: string;
  kind: "persistent" | "direct" | "group";
  tz: string;
  topic: string;
  folder: string | null;
  tabPos: number | null;
  muted: boolean;
  lastSeq: number;
  lastReadSeq: number;
  unread: number;
  unreadMentions: number;
  lastMessage: Message | null;
  members: User[];
};

export type Instrument = {
  id: string;
  composite: string;
  name: string;
  isin: string;
  sedol: string;
  cusip: string | null;
  figi: string;
  ccy: string;
  country: string;
  sector: string;
  lastPx: number;
  feeBps: number;
  utilPct: number;
  tier: "GC" | "WARM" | "HOT";
  shs_out: number;
  adv_20d: number;
  si_pct: number;
  days_to_cover: number;
  threshold_flag: boolean;
};

export const IB_URL: string = (import.meta.env.VITE_IB_URL as string | undefined) ?? "http://localhost:8010";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(as: string, path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(IB_URL + path, {
    ...init,
    headers: { "content-type": "application/json", "x-user": as, ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, String(detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  users: (as: string) => call<User[]>(as, "/api/users"),
  rooms: (as: string) => call<Room[]>(as, "/api/rooms"),
  messages: (as: string, roomId: string, before?: number) =>
    call<{ messages: Message[]; hasMore: boolean }>(
      as,
      `/api/rooms/${encodeURIComponent(roomId)}/messages?limit=50${before ? `&before=${before}` : ""}`,
    ),
  send: (as: string, roomId: string, text: string, clientMsgId: string) =>
    call<Message>(as, `/api/rooms/${encodeURIComponent(roomId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ text, clientMsgId }),
    }),
  blast: (as: string, roomIds: string[], text: string) =>
    call<{ blastId: string; messages: Message[] }>(as, "/api/blast", {
      method: "POST",
      body: JSON.stringify({ roomIds, text }),
    }),
  read: (as: string, roomId: string, seq: number) =>
    call(as, `/api/rooms/${encodeURIComponent(roomId)}/read`, { method: "POST", body: JSON.stringify({ seq }) }),
  typing: (as: string, roomId: string) =>
    call(as, `/api/rooms/${encodeURIComponent(roomId)}/typing`, { method: "POST" }),
  prefs: (as: string, roomId: string, prefs: Partial<{ folder: string | null; tabPos: number | null }>) =>
    call(as, `/api/rooms/${encodeURIComponent(roomId)}/prefs`, { method: "PUT", body: JSON.stringify(prefs) }),
  search: (as: string, q: string) => call<Message[]>(as, `/api/search?q=${encodeURIComponent(q)}&limit=100`),
  instrument: (q: string) => call<Instrument>("srivera", `/api/instruments/resolve?q=${encodeURIComponent(q)}`),
  health: () => call<{ simTime: string }>("srivera", "/health"),
};

export function wsUrl(as: string): string {
  return IB_URL.replace(/^http/, "ws") + `/ws?as=${encodeURIComponent(as)}`;
}
