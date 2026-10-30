import { UserButton } from "@clerk/react";
import { api } from "../lib/api";
import { authEnabled } from "../lib/auth";
import { time } from "../lib/format";
import { useQuery } from "../lib/hooks";
import { useDesk, VIEWS } from "../lib/store";
import type { ShortList, Status } from "../lib/types";
import { Kbd } from "./ui";

const SHORT: Record<string, string> = {
  control: "SIM",
  chat: "CHAT",
  gateway: "GWY",
  venue_a: "VEN A",
  venue_b: "VEN B",
  processor: "PROC",
  custodian: "CUST",
};

export function TopBar() {
  const clock = useDesk((s) => s.clock);
  const connected = useDesk((s) => s.connected);
  const setPalette = useDesk((s) => s.setPalette);
  const toggleTheme = useDesk((s) => s.toggleTheme);
  const toast = useDesk((s) => s.toast);
  const { data: st, reload } = useQuery<Status>("/status", [], 10000);
  const kill = st?.killSwitch.on;

  const toggleKill = async () => {
    await api.put("/kill-switch", { on: !kill });
    toast(kill ? "info" : "warn", kill ? "Kill switch off: agent actions can execute" : "Kill switch on: agent frozen");
    reload();
  };

  return (
    <header className="h-11 shrink-0 flex items-center gap-5 px-4 border-b border-line/60 bg-panel/70 backdrop-blur">
      <div className="flex items-baseline gap-2">
        <span className="text-sm font-medium tracking-[0.22em]">LOANDESK</span>
        <span className="text-2xs text-faint">sec lending desk</span>
      </div>

      <div className="flex items-center gap-2 text-xs">
        <span className={`w-1.5 h-1.5 rounded-full ${connected ? "bg-up pulse" : "bg-down"}`} />
        <span className="text-ink tabular-nums">{clock ? time(clock.now) : "--:--:--"}</span>
        <span className="text-faint">NY</span>
        {clock && (
          <span className="text-faint">
            {clock.synced ? `${clock.mode}${clock.mode === "live" ? ` ${clock.speed}×` : ""}` : "unsynced"}
            {clock.paused ? " · paused" : ""}
          </span>
        )}
      </div>

      <div className="hidden md:flex items-center gap-3">
        {st?.systems.map((s) => (
          <span key={s.system} title={`${s.label} · ${s.url}`} className="flex items-center gap-1.5 text-2xs text-dim">
            <span className={`w-1.5 h-1.5 rounded-full ${s.up ? "bg-up/80" : "bg-faint/60"}`} />
            {SHORT[s.system] ?? s.system}
          </span>
        ))}
      </div>

      <div className="ml-auto flex items-center gap-2">
        {st && (
          <span className="text-2xs text-faint" title={Object.values(st.agent.models).join(" · ")}>
            agent <span className={st.agent.mode === "off" ? "text-dim" : "text-accent"}>{st.agent.mode}</span>
            {st.agent.mode !== "off" && ` · $${st.agent.spentUsd.toFixed(2)}/${st.agent.budgetUsd.toFixed(2)}`}
          </span>
        )}
        <button onClick={toggleKill} className={kill ? "btn-danger" : "btn"} title="Freeze every agent action">
          {kill ? "■ killed" : "kill switch"}
        </button>
        <button onClick={() => setPalette(true)} className="btn">
          command <Kbd>⌘K</Kbd>
        </button>
        <button onClick={toggleTheme} className="btn" title="Toggle theme">◐</button>
        {authEnabled && <UserButton />}
      </div>
    </header>
  );
}

export function Rail() {
  const view = useDesk((s) => s.view);
  const setView = useDesk((s) => s.setView);
  const counts = useQuery<{ counts: Record<string, number> }>("/blotter", ["order", "trade", "break", "locate"]);
  const actions = useQuery<unknown[]>("/actions", ["action"]);
  const shorts = useQuery<ShortList>("/short-list", ["short_list", "order", "trade", "action"]);
  const badge: Record<string, number | undefined> = {
    locates: counts.data?.counts.openLocates,
    shorts: shorts.data?.summary.short,
    blotter: counts.data ? counts.data.counts.breaks + counts.data.counts.unbooked : undefined,
    agent: actions.data?.length,
  };
  return (
    <nav className="w-40 shrink-0 border-r border-line/60 bg-panel/40 flex flex-col py-3 gap-0.5">
      {VIEWS.map((v) => (
        <button
          key={v.id}
          onClick={() => setView(v.id)}
          className={`group mx-2 h-8 px-2.5 rounded-md flex items-center gap-2 text-xs transition-colors ${
            view === v.id ? "bg-raised text-ink" : "text-dim hover:text-ink hover:bg-raised/40"
          }`}
        >
          <span className={`text-2xs ${view === v.id ? "text-accent" : "text-faint"}`}>{v.key}</span>
          <span>{v.label}</span>
          {!!badge[v.id] && (
            <span className="ml-auto text-2xs px-1.5 rounded-full border border-line text-dim">{badge[v.id]}</span>
          )}
        </button>
      ))}
      <div className="mt-auto mx-3 pt-3 border-t rule text-2xs text-faint leading-relaxed">
        Synthetic data. Not affiliated with EquiLend, Wematch, FIS or Bloomberg.
      </div>
    </nav>
  );
}

export function Toasts() {
  const toasts = useDesk((s) => s.toasts);
  const dismiss = useDesk((s) => s.dismiss);
  const tone = { ok: "border-up/40 text-up", warn: "border-warm/40 text-warm", err: "border-down/50 text-down",
    info: "border-line text-dim" };
  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 w-96">
      {toasts.map((t) => (
        <button key={t.id} onClick={() => dismiss(t.id)}
          className={`panel text-left px-3 py-2 text-xs bg-panel shadow-lg shadow-black/30 ${tone[t.tone]}`}>
          {t.text}
        </button>
      ))}
    </div>
  );
}
