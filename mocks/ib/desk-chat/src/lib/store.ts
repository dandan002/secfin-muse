import { create } from "zustand";
import { api, type Message, type Room, type User } from "./api";

type Typing = Record<string, Record<string, number>>; // roomId -> userId -> expiry ms
export type Theme = "terminal" | "dark" | "light";
const THEMES: Theme[] = ["terminal", "dark", "light"];

type State = {
  me: string; // handle we're acting as
  users: User[];
  rooms: Room[];
  activeRoom: string | null;
  openTabs: string[];
  folder: string; // "All" or a folder name
  messages: Record<string, Message[]>;
  hasMore: Record<string, boolean>;
  typing: Typing;
  composeDraft: { roomId: string; text: string; nonce: number } | null;
  connected: boolean;
  simTime: string | null;
  theme: Theme;
  searchOpen: boolean;
  searchSeed: string;
  helpOpen: boolean;
  blastOpen: boolean;
  setMe: (handle: string) => void;
  setFolder: (f: string) => void;
  openRoom: (id: string) => void;
  closeTab: (id: string) => void;
  loadRooms: () => Promise<void>;
  loadOlder: (roomId: string) => Promise<void>;
  ingest: (m: Message) => void;
  markRead: (roomId: string) => void;
  setTyping: (roomId: string, userId: string) => void;
  setCompose: (roomId: string, text: string) => void;
  set: (p: Partial<State>) => void;
  cycleTheme: () => void;
};

const savedTheme: Theme = (() => {
  try {
    const t = localStorage.getItem("deskchat.theme") as Theme | null;
    return t && THEMES.includes(t) ? t : "terminal";
  } catch {
    return "terminal";
  }
})();

export const useStore = create<State>((set, get) => ({
  me: "srivera",
  users: [],
  rooms: [],
  activeRoom: null,
  openTabs: [],
  folder: "All",
  messages: {},
  hasMore: {},
  typing: {},
  composeDraft: null,
  connected: false,
  simTime: null,
  theme: savedTheme,
  searchOpen: false,
  searchSeed: "",
  helpOpen: false,
  blastOpen: false,

  set: (p) => set(p),

  cycleTheme: () => {
    const next = THEMES[(THEMES.indexOf(get().theme) + 1) % THEMES.length];
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("deskchat.theme", next);
    } catch {
      /* private mode */
    }
    set({ theme: next });
  },

  setMe: (handle) => {
    set({ me: handle, rooms: [], messages: {}, hasMore: {}, activeRoom: null, openTabs: [], composeDraft: null });
    void get().loadRooms();
  },

  setFolder: (f) => set({ folder: f }),

  openRoom: (id) => {
    const { openTabs, messages } = get();
    set({ activeRoom: id, openTabs: openTabs.includes(id) ? openTabs : [...openTabs, id] });
    if (!messages[id]) void get().loadOlder(id);
    get().markRead(id);
  },

  closeTab: (id) => {
    const tabs = get().openTabs.filter((t) => t !== id);
    set({ openTabs: tabs, activeRoom: get().activeRoom === id ? (tabs[tabs.length - 1] ?? null) : get().activeRoom });
  },

  loadRooms: async () => {
    const me = get().me;
    const [rooms, users] = await Promise.all([api.rooms(me), api.users(me)]);
    if (get().me !== me) return;
    const tabs = rooms
      .filter((r) => r.tabPos !== null)
      .sort((a, b) => (a.tabPos ?? 0) - (b.tabPos ?? 0))
      .map((r) => r.id);
    set({ rooms, users });
    if (!get().openTabs.length && tabs.length) {
      set({ openTabs: tabs });
      get().openRoom(tabs[0]);
    } else if (!get().activeRoom && rooms.length) {
      get().openRoom(rooms[0].id);
    }
  },

  loadOlder: async (roomId) => {
    const me = get().me;
    const current = get().messages[roomId] ?? [];
    const before = current.length ? current[0].seq : undefined;
    const page = await api.messages(me, roomId, before);
    if (get().me !== me) return;
    const seen = new Set(current.map((m) => m.messageId));
    set({
      messages: { ...get().messages, [roomId]: [...page.messages.filter((m) => !seen.has(m.messageId)), ...current] },
      hasMore: { ...get().hasMore, [roomId]: page.hasMore },
    });
  },

  ingest: (m) => {
    const { messages, rooms, activeRoom, users } = get();
    const list = messages[m.streamId];
    if (list && !list.some((x) => x.messageId === m.messageId)) {
      set({ messages: { ...messages, [m.streamId]: [...list, m] } });
    }
    const meUser = users.find((u) => u.handle === get().me);
    const mine = m.sender.handle === get().me;
    const viewing = activeRoom === m.streamId && document.visibilityState === "visible";
    set({
      rooms: rooms.map((r) =>
        r.id !== m.streamId
          ? r
          : {
              ...r,
              lastSeq: Math.max(r.lastSeq, m.seq),
              lastMessage: m,
              lastReadSeq: mine || viewing ? m.seq : r.lastReadSeq,
              unread: mine || viewing ? 0 : r.unread + 1,
              unreadMentions:
                mine || viewing ? 0 : r.unreadMentions + (meUser && m.mentions.includes(meUser.uuid) ? 1 : 0),
            },
      ),
    });
    const t = { ...get().typing };
    if (t[m.streamId]) {
      delete t[m.streamId][m.sender.uuid];
      set({ typing: t });
    }
    if (viewing && !mine) void api.read(get().me, m.streamId, m.seq);
  },

  markRead: (roomId) => {
    const room = get().rooms.find((r) => r.id === roomId);
    if (!room || room.lastReadSeq >= room.lastSeq) return;
    set({
      rooms: get().rooms.map((r) =>
        r.id === roomId ? { ...r, lastReadSeq: r.lastSeq, unread: 0, unreadMentions: 0 } : r,
      ),
    });
    void api.read(get().me, roomId, room.lastSeq);
  },

  setTyping: (roomId, userId) => {
    const t = { ...get().typing, [roomId]: { ...(get().typing[roomId] ?? {}), [userId]: Date.now() + 4000 } };
    set({ typing: t });
  },

  setCompose: (roomId, text) => set({ composeDraft: { roomId, text, nonce: Date.now() } }),
}));
