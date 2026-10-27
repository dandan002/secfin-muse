import { useEffect, useState } from "react";
import { BlastDialog } from "./components/BlastDialog";
import { CommandBar } from "./components/CommandBar";
import { FunctionKeys } from "./components/FunctionKeys";
import { Rail } from "./components/Rail";
import { RoomView } from "./components/RoomView";
import { SearchPanel } from "./components/SearchPanel";
import { Sidebar } from "./components/Sidebar";
import type { Message } from "./lib/api";
import { useStore } from "./lib/store";
import { localTime, tzLabel } from "./lib/time";
import { useLiveConnection } from "./lib/ws";

export const DISCLAIMER =
  "Simulator for demonstration only. Not affiliated with or endorsed by Bloomberg, EquiLend, Wematch or FIS. All data is synthetic.";

/** The whole Desk Chat client. Embeddable: render it inside any sized container. */
export function DeskChat() {
  useLiveConnection();
  const { rooms, activeRoom, openTabs, openRoom, closeTab, loadRooms, searchOpen, blastOpen, set, simTime, theme } =
    useStore();
  const term = theme === "terminal";
  const [highlight, setHighlight] = useState<number | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    loadRooms().catch((e) => setLoadError(String(e.message ?? e)));
  }, [loadRooms]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        set({ searchOpen: true });
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        set({ blastOpen: true });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [set]);

  const room = rooms.find((r) => r.id === activeRoom);

  const jump = async (m: Message) => {
    openRoom(m.streamId);
    const s = useStore.getState();
    // Page back until the hit is loaded.
    for (let i = 0; i < 20 && !(s.messages[m.streamId] ?? []).some((x) => x.messageId === m.messageId); i++) {
      if (s.hasMore[m.streamId] === false) break;
      await s.loadOlder(m.streamId);
    }
    setHighlight(m.messageId);
  };

  return (
    <div className="flex h-full flex-col">
      {term && <CommandBar />}
      <div className="flex min-h-0 flex-1">
        {!term && <Rail />}
        <Sidebar />
        <main className="flex min-w-0 flex-1 flex-col bg-bg">
          <div className={`flex items-stretch border-b border-line ${term ? "bg-black text-[12px]" : "bg-panel"}`}>
            <div className="scroll-thin flex min-w-0 flex-1 overflow-x-auto">
              {openTabs.map((id) => {
                const r = rooms.find((x) => x.id === id);
                if (!r) return null;
                return (
                  <div
                    key={id}
                    className={`group flex shrink-0 items-center gap-2 border-r border-line ${
                      term
                        ? id === activeRoom
                          ? "bg-[#3a3a3a] px-2 py-0.5 font-semibold text-white"
                          : "bg-black px-2 py-0.5 text-ink hover:text-accent"
                        : id === activeRoom
                          ? "bg-bg px-3 py-2 text-ink"
                          : "px-3 py-2 text-muted hover:text-ink"
                    }`}
                  >
                    <button onClick={() => openRoom(id)} className="max-w-48 truncate">
                      {r.name}
                    </button>
                    {r.unread > 0 && id !== activeRoom &&
                      (term ? <span className="text-mention">[{r.unread}]</span> : <span className="h-1.5 w-1.5 rounded-full bg-accent" />)}
                    <button
                      onClick={() => closeTab(id)}
                      className="invisible text-muted hover:text-ink group-hover:visible"
                      title="Close tab"
                    >
                      ✕
                    </button>
                  </div>
                );
              })}
            </div>
            <div className={`flex shrink-0 items-center gap-2 px-3 ${term ? "hidden" : ""}`}>
              <button
                onClick={() => set({ blastOpen: true })}
                className="rounded-md border border-line px-2 py-1 text-[12px] text-muted hover:border-accent hover:text-ink"
                title="Blast (Ctrl+B)"
              >
                Blast
              </button>
              <button
                onClick={() => set({ searchOpen: !searchOpen })}
                className="rounded-md border border-line px-2 py-1 text-[12px] text-muted hover:border-accent hover:text-ink"
                title="Search (Ctrl+F)"
              >
                Search
              </button>
              <span className="num rounded-md bg-raised px-2 py-1 text-[12px]" title="Simulated clock">
                SIM {simTime ? localTime(simTime, "America/New_York") : "--:--:--"} {tzLabel("America/New_York")}
              </span>
            </div>
          </div>
          {loadError && (
            <div className="m-4 rounded-md border border-hot/50 bg-hot/10 p-3 text-hot">
              Can't reach the chat service: {loadError}. Is mock-ib running?
            </div>
          )}
          {room ? (
            <RoomView key={room.id} room={room} highlight={highlight} />
          ) : (
            <div className="grid flex-1 place-items-center text-muted">Pick a room</div>
          )}
        </main>
        {searchOpen && <SearchPanel onJump={(m) => void jump(m)} />}
      </div>
      {term && <FunctionKeys />}
      <footer className={`border-t border-line px-3 py-1 text-[11px] text-muted ${term ? "bg-black" : "bg-panel"}`}>
        {DISCLAIMER}
      </footer>
      {blastOpen && <BlastDialog />}
    </div>
  );
}
