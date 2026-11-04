import { create } from "zustand";

export type View = "blotter" | "locates" | "ticket" | "market" | "agent" | "paste" | "audit" | "chat" | "shorts";

export type Workspace = "inbox" | "book" | "market" | "audit";

/** Four workspaces, switched with 1–4; each holds one or more screens, shown as tabs. */
export const WORKSPACES: { id: Workspace; label: string; key: string }[] = [
  { id: "inbox", label: "Inbox", key: "1" },
  { id: "book", label: "Book", key: "2" },
  { id: "market", label: "Market", key: "3" },
  { id: "audit", label: "Audit", key: "4" },
];

export const VIEWS: { id: View; label: string; workspace: Workspace }[] = [
  { id: "agent", label: "Approvals", workspace: "inbox" },
  { id: "locates", label: "Locates", workspace: "inbox" },
  { id: "chat", label: "Chat", workspace: "inbox" },
  { id: "blotter", label: "Blotter", workspace: "book" },
  { id: "shorts", label: "Short list", workspace: "book" },
  { id: "market", label: "Rates", workspace: "market" },
  { id: "ticket", label: "Ticket", workspace: "market" },
  { id: "paste", label: "Paste", workspace: "market" },
  { id: "audit", label: "Audit", workspace: "audit" },
];

export const workspaceOf = (v: View) => VIEWS.find((x) => x.id === v)?.workspace ?? "inbox";

export interface TicketPrefill {
  instrument?: string;
  side?: "BORROW" | "LEND";
  quantity?: number;
  feeBps?: number;
  counterparty?: string;
}

/** A line for the bulk borrow sheet: a name and how many shares to borrow. */
export interface BulkLine {
  instrumentId: string;
  composite: string;
  quantity: number;
  why?: string;
}

export interface Toast {
  id: number;
  tone: "ok" | "warn" | "err" | "info";
  text: string;
}

interface Clock {
  now: string;
  nowLocal: string;
  mode: string;
  speed: number;
  paused: boolean;
  synced: boolean;
}

interface DeskState {
  view: View;
  setView: (v: View) => void;
  /** The last screen used in each workspace, so switching back lands where the trader left off. */
  lastView: Partial<Record<Workspace, View>>;
  openWorkspace: (w: Workspace) => void;
  /** The approval card in focus: picked from the sidebar's pending list or the queue. */
  selectedAction: string | null;
  selectAction: (id: string | null) => void;
  clock: Clock | null;
  setClock: (c: Clock) => void;
  connected: boolean;
  setConnected: (c: boolean) => void;
  /** Bumped by stream pushes; views refetch when their topic's version changes. */
  versions: Record<string, number>;
  bump: (topic: string) => void;
  ticket: TicketPrefill;
  openTicket: (p: TicketPrefill) => void;
  marketFilter: { tier?: string; q?: string };
  setMarketFilter: (f: { tier?: string; q?: string }) => void;
  watchlist: string[];
  toggleWatch: (id: string) => void;
  addWatch: (ids: string[]) => void;
  toasts: Toast[];
  toast: (tone: Toast["tone"], text: string) => void;
  dismiss: (id: number) => void;
  /** The id Audit opens on: set by "Full story" links elsewhere. */
  auditFocus: string;
  openAudit: (id: string) => void;
  /** The bulk borrow sheet's lines, or null when it's closed. */
  bulkBorrow: BulkLine[] | null;
  openBulkBorrow: (lines: BulkLine[] | null) => void;
  paletteOpen: boolean;
  setPalette: (open: boolean) => void;
  theme: "dark" | "light";
  toggleTheme: () => void;
}

function load<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}

function save(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: keep it in memory */
  }
}

let toastId = 0;

export const useDesk = create<DeskState>((set, get) => ({
  view: VIEWS.some((v) => v.id === load<View>("loandesk.view", "agent")) ? load<View>("loandesk.view", "agent") : "agent",
  setView: (view) => {
    save("loandesk.view", view);
    set((s) => ({ view, lastView: { ...s.lastView, [workspaceOf(view)]: view } }));
  },
  lastView: {},
  openWorkspace: (w) => get().setView(get().lastView[w] ?? VIEWS.find((v) => v.workspace === w)!.id),
  selectedAction: null,
  selectAction: (selectedAction) => set({ selectedAction }),
  clock: null,
  setClock: (clock) => set({ clock }),
  connected: false,
  setConnected: (connected) => set({ connected }),
  versions: {},
  bump: (topic) => set((s) => ({ versions: { ...s.versions, [topic]: (s.versions[topic] ?? 0) + 1 } })),
  ticket: {},
  openTicket: (ticket) => {
    get().setView("ticket");
    set({ ticket });
  },
  marketFilter: {},
  setMarketFilter: (marketFilter) => set({ marketFilter }),
  watchlist: load<string[]>("loandesk.watchlist", []),
  toggleWatch: (id) => {
    const w = get().watchlist;
    const next = w.includes(id) ? w.filter((x) => x !== id) : [...w, id];
    save("loandesk.watchlist", next);
    set({ watchlist: next });
  },
  addWatch: (ids) => {
    const next = Array.from(new Set([...get().watchlist, ...ids]));
    save("loandesk.watchlist", next);
    set({ watchlist: next });
  },
  toasts: [],
  toast: (tone, text) => {
    const id = ++toastId;
    set((s) => ({ toasts: [...s.toasts.slice(-4), { id, tone, text }] }));
    setTimeout(() => get().dismiss(id), tone === "err" ? 7000 : 3500);
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  auditFocus: "",
  openAudit: (auditFocus) => {
    set({ auditFocus });
    get().setView("audit");
  },
  bulkBorrow: null,
  openBulkBorrow: (bulkBorrow) => set({ bulkBorrow }),
  paletteOpen: false,
  setPalette: (paletteOpen) => set({ paletteOpen }),
  theme: load<"dark" | "light">("loandesk.theme", "dark"),
  toggleTheme: () => {
    const theme = get().theme === "dark" ? "light" : "dark";
    save("loandesk.theme", theme);
    document.documentElement.dataset.theme = theme;
    set({ theme });
  },
}));
