import { useEffect, useRef, useState } from "react";
import { api, type Message } from "../lib/api";
import { useStore } from "../lib/store";
import { localTime } from "../lib/time";

export function SearchPanel({ onJump }: { onJump: (m: Message) => void }) {
  const { me, rooms, set, searchSeed, theme } = useStore();
  const [q, setQ] = useState(searchSeed);

  useEffect(() => setQ(searchSeed), [searchSeed]);
  const [results, setResults] = useState<Message[] | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => input.current?.focus(), []);

  useEffect(() => {
    if (!q.trim()) {
      setResults(null);
      return;
    }
    const id = window.setTimeout(() => {
      api.search(me, q.trim()).then(setResults, () => setResults([]));
    }, 200);
    return () => window.clearTimeout(id);
  }, [q, me]);

  const roomOf = (id: string) => rooms.find((r) => r.id === id);

  return (
    <aside className="flex w-96 shrink-0 flex-col border-l border-line bg-panel">
      {theme === "terminal" && (
        <div className="bg-accent px-2 py-0.5 text-[12px] font-semibold text-black">SRCH · SEARCH ALL CHATS</div>
      )}
      <div className="flex items-center gap-2 border-b border-line p-3">
        <input
          ref={input}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && set({ searchOpen: false })}
          placeholder="Search all rooms"
          className="min-w-0 flex-1 rounded-md border border-line bg-raised px-2 py-1.5 outline-none placeholder:text-muted focus:border-accent"
        />
        <button onClick={() => set({ searchOpen: false })} className="px-1 text-muted hover:text-ink" title="Close">
          ✕
        </button>
      </div>
      <div className="scroll-thin flex-1 overflow-y-auto">
        {results === null && <p className="p-3 text-muted">Type to search messages across your rooms.</p>}
        {results?.length === 0 && <p className="p-3 text-muted">No matches.</p>}
        {results?.map((m) => {
          const r = roomOf(m.streamId);
          return (
            <button
              key={m.messageId}
              onClick={() => onJump(m)}
              className="block w-full border-b border-line/60 px-3 py-2 text-left hover:bg-raised"
            >
              <div className="flex gap-2 text-[11px] text-muted">
                <span className="truncate">{r?.name ?? m.streamId}</span>
                <span className="flex-1" />
                <span className="num">{localTime(m.ts, r?.tz ?? "America/New_York")}</span>
              </div>
              <div className="text-[12px] font-medium">{m.sender.displayName}</div>
              <div className="line-clamp-3 whitespace-pre-wrap text-[12px] text-ink/90">{m.text}</div>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
