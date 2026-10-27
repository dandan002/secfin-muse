import { useStore } from "../lib/store";

const FOLDERS = ["All", "Clients", "Lenders", "Internal"] as const;

const ICONS: Record<(typeof FOLDERS)[number], string> = {
  All: "M4 6h16M4 12h16M4 18h16",
  Clients: "M16 11a4 4 0 1 0-8 0 4 4 0 0 0 8 0zM4 21a8 8 0 0 1 16 0",
  Lenders: "M3 10l9-6 9 6M5 10v9h14v-9M9 19v-5h6v5",
  Internal: "M4 4h16v12H7l-3 3z",
};

export function Rail() {
  const { folder, setFolder, rooms, theme, connected, cycleTheme } = useStore();
  const unreadBy = (f: string) =>
    rooms.filter((r) => (f === "All" || r.folder === f) && r.unread > 0 && !r.muted).length;

  return (
    <nav className="flex w-14 shrink-0 flex-col items-center gap-1 border-r border-line bg-panel py-3" aria-label="Folders">
      <div className="mb-3 grid h-8 w-8 place-items-center rounded-md bg-accent/90 text-[11px] font-semibold text-bg">
        DC
      </div>
      {FOLDERS.map((f) => {
        const n = unreadBy(f);
        return (
          <button
            key={f}
            onClick={() => setFolder(f)}
            title={f}
            className={`relative grid h-10 w-10 place-items-center rounded-md transition-colors ${
              folder === f ? "bg-raised text-accent" : "text-muted hover:bg-raised hover:text-ink"
            }`}
          >
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={1.7}>
              <path d={ICONS[f]} strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {n > 0 && (
              <span className="absolute -right-0.5 -top-0.5 min-w-4 rounded-full bg-accent px-1 text-[10px] font-semibold leading-4 text-bg">
                {n}
              </span>
            )}
          </button>
        );
      })}
      <div className="flex-1" />
      <button
        onClick={cycleTheme}
        title="Toggle theme"
        className="grid h-9 w-9 place-items-center rounded-md text-muted hover:bg-raised hover:text-ink"
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth={1.8}>
          {theme === "dark" ? (
            <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
          ) : (
            <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z" />
          )}
        </svg>
      </button>
      <span
        title={connected ? "Live" : "Reconnecting…"}
        className={`mt-1 h-2 w-2 rounded-full ${connected ? "bg-accent" : "animate-pulse bg-hot"}`}
      />
    </nav>
  );
}
