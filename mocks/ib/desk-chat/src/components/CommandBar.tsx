import { useRef, useState } from "react";
import { useStore } from "../lib/store";
import { localTime } from "../lib/time";

export const COMMANDS: [string, string][] = [
  ["<room>", "jump to a chat by name, e.g. PB-BRAVO"],
  ["MSG <handle>", "open the chat you share with someone, e.g. MSG rpatel"],
  ["SRCH <text>", "search every chat"],
  ["BLAST", "send one message to many chats"],
  ["THEME", "switch terminal / dark / light"],
  ["HELP", "commands and function keys"],
];

/** Top command line: type a command and hit <GO> (Enter). */
export function CommandBar() {
  const { rooms, openRoom, set, cycleTheme, simTime, connected, me, users } = useStore();
  const [cmd, setCmd] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const run = () => {
    const raw = cmd.trim();
    const [verb, ...rest] = raw.split(/\s+/);
    const arg = rest.join(" ");
    const v = verb?.toUpperCase() ?? "";
    setMsg(null);
    if (!raw) return;
    if (v === "BLAST") set({ blastOpen: true });
    else if (v === "SRCH" || v === "SEARCH") set({ searchOpen: true, searchSeed: arg });
    else if (v === "THEME") cycleTheme();
    else if (v === "HELP") set({ helpOpen: true });
    else if (v === "MSG") {
      const q = arg.toLowerCase();
      const u = users.find((x) => x.handle !== me && (x.handle.toLowerCase().startsWith(q) || x.displayName.toLowerCase().includes(q)));
      const room = u && (rooms.find((r) => r.kind === "direct" && r.members.some((m) => m.uuid === u.uuid)) ??
        rooms.find((r) => r.members.some((m) => m.uuid === u.uuid)));
      if (room) openRoom(room.id);
      else return setMsg(`NO CHAT WITH ${arg.toUpperCase() || "?"}`);
    } else {
      const q = raw.toLowerCase();
      const room = rooms.find((r) => r.name.toLowerCase().startsWith(q)) ?? rooms.find((r) => r.name.toLowerCase().includes(q));
      if (room) openRoom(room.id);
      else return setMsg(`UNKNOWN COMMAND: ${raw.toUpperCase()}  (F1 FOR HELP)`);
    }
    setCmd("");
  };

  return (
    <div className="flex h-8 shrink-0 items-stretch border-b border-line bg-black text-[12px]">
      <span className="flex items-center bg-accent px-2 font-semibold tracking-wide text-black">DESK CHAT</span>
      <form
        className="flex min-w-0 flex-1 items-stretch"
        onSubmit={(e) => {
          e.preventDefault();
          run();
        }}
      >
        <input
          ref={input}
          id="command-line"
          value={cmd}
          onChange={(e) => setCmd(e.target.value)}
          placeholder="Enter a chat name or command, e.g. PB-BRAVO, MSG rpatel, SRCH QZNT, BLAST"
          spellCheck={false}
          className="min-w-0 flex-1 bg-black px-2 uppercase text-ink caret-accent outline-none placeholder:normal-case placeholder:text-muted/70"
        />
        <button type="submit" className="bg-[#2f7d32] px-3 font-semibold text-white hover:bg-[#3c9a40]" title="Run (Enter)">
          &lt;GO&gt;
        </button>
      </form>
      {msg && <span className="flex items-center bg-hot px-2 font-semibold text-black">{msg}</span>}
      <span className="flex items-center gap-2 border-l border-line px-3 text-muted">
        <span className={connected ? "text-[#3fd34a]" : "animate-pulse text-hot"}>{connected ? "● LIVE" : "● RECONNECTING"}</span>
        <span className="num text-ink">SIM {simTime ? localTime(simTime, "America/New_York") : "--:--:--"} NY</span>
      </span>
    </div>
  );
}
