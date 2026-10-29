import { create } from "zustand";

export type View = "blotter" | "locates" | "ticket" | "market" | "agent" | "paste" | "audit" | "chat";

export const VIEWS: { id: View; label: string; key: string }[] = [
  { id: "blotter", label: "Blotter", key: "1" },
  { id: "locates", label: "Locates", key: "2" },
  { id: "ticket", label: "Trade", key: "3" },
  { id: "market", label: "Market", key: "4" },
  { id: "agent", label: "Agent", key: "5" },
  { id: "paste", label: "Paste", key: "6" },
  { id: "audit", label: "Audit", key: "7" },
  { id: "chat", label: "Chat", key: "8" },
];

export interface TicketPrefill {
  instrument?: string;
  side?: "BORROW" | "LEND";
  quantity?: number;
  feeBps?: number;
  counterparty?: string;
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
  view: load<View>("loandesk.view", "blotter"),
  setView: (view) => {
    save("loandesk.view", view);
    set({ view });
  },
  clock: null,
  setClock: (clock) => set({ clock }),
  connected: false,
  setConnected: (connected) => set({ connected }),
  versions: {},
  bump: (topic) => set((s) => ({ versions: { ...s.versions, [topic]: (s.versions[topic] ?? 0) + 1 } })),
  ticket: {},
  openTicket: (ticket) => {
    save("loandesk.view", "ticket");
    set({ ticket, view: "ticket" });
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
