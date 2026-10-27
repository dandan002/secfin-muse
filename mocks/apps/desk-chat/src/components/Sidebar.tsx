import { useMemo, useState } from "react";
import type { Room } from "../lib/api";
import { useStore } from "../lib/store";
import { localTime } from "../lib/time";

const ORDER = ["Clients", "Lenders", "Internal"];

export function Sidebar() {
  const { rooms, users, me, setMe, folder, activeRoom, openRoom, set } = useStore();
  const [filter, setFilter] = useState("");

  const groups = useMemo(() => {
    const visible = rooms.filter(
      (r) =>
        (folder === "All" || r.folder === folder) &&
        (!filter || r.name.toLowerCase().includes(filter.toLowerCase())),
    );
    const by: Record<string, Room[]> = {};
    for (const r of visible) (by[r.folder ?? "Other"] ??= []).push(r);
    const rank = (f: string) => ORDER.indexOf(f) + 1 || 99;
    return Object.entries(by).sort(([a], [b]) => rank(a) - rank(b));
  }, [rooms, folder, filter]);

  const meUser = users.find((u) => u.handle === me);
  const humans = users.filter((u) => u.kind === "human");
  const personas = users.filter((u) => u.kind === "persona");

  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-line bg-panel">
      <div className="border-b border-line p-3">
        <label className="mb-1 block text-[11px] uppercase tracking-wide text-muted">Acting as</label>
        <select
          value={me}
          onChange={(e) => setMe(e.target.value)}
          className="w-full rounded-md border border-line bg-raised px-2 py-1.5 text-ink outline-none focus:border-accent"
        >
          <optgroup label="Our desk">
            {humans.map((u) => (
              <option key={u.handle} value={u.handle}>
                {u.displayName} · {u.firm}
              </option>
            ))}
          </optgroup>
          <optgroup label="Counterparties (personas)">
            {personas.map((u) => (
              <option key={u.handle} value={u.handle}>
                {u.displayName} · {u.firm}
              </option>
            ))}
          </optgroup>
        </select>
        {meUser?.kind === "persona" && (
          <p className="mt-1 text-[11px] text-warm">You're typing as a counterparty persona.</p>
        )}
        <div className="mt-3 flex gap-2">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter rooms"
            className="min-w-0 flex-1 rounded-md border border-line bg-raised px-2 py-1.5 outline-none placeholder:text-muted focus:border-accent"
          />
          <button
            onClick={() => set({ searchOpen: true })}
            title="Search messages (Ctrl+F)"
            className="rounded-md border border-line px-2 text-muted hover:border-accent hover:text-ink"
          >
            ⌕
          </button>
        </div>
      </div>
      <div className="scroll-thin flex-1 overflow-y-auto py-2">
        {groups.map(([name, list]) => (
          <section key={name} className="mb-2">
            <h3 className="px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-muted">{name}</h3>
            {list.map((r) => (
              <button
                key={r.id}
                onClick={() => openRoom(r.id)}
                className={`flex w-full flex-col gap-0.5 px-3 py-2 text-left transition-colors ${
                  activeRoom === r.id ? "bg-raised" : "hover:bg-raised/60"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className={`truncate ${r.unread ? "font-semibold text-ink" : "text-ink/90"}`}>{r.name}</span>
                  <span className="flex-1" />
                  {r.lastMessage && (
                    <span className="num text-[11px] text-muted">{localTime(r.lastMessage.ts, r.tz, false)}</span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <span className="truncate text-[12px] text-muted">
                    {r.lastMessage ? `${r.lastMessage.sender.displayName.split(" ")[0]}: ${r.lastMessage.text}` : r.topic}
                  </span>
                  <span className="flex-1" />
                  {r.unreadMentions > 0 && (
                    <span className="rounded-full bg-mention px-1.5 text-[10px] font-semibold leading-4 text-bg">@</span>
                  )}
                  {r.unread > 0 && (
                    <span className="num rounded-full bg-accent px-1.5 text-[10px] font-semibold leading-4 text-bg">
                      {r.unread}
                    </span>
                  )}
                </div>
              </button>
            ))}
          </section>
        ))}
      </div>
    </aside>
  );
}
