import { UserButton } from "@clerk/react";
import { ReactNode, useState } from "react";
import { api } from "../lib/api";
import { authEnabled } from "../lib/auth";
import { time } from "../lib/format";
import { useKeys, useQuery } from "../lib/hooks";
import { useDesk, VIEWS, workspaceOf, WORKSPACES } from "../lib/store";
import type { Action, Status } from "../lib/types";

const SHORT: Record<string, string> = {
  control: "Sim",
  chat: "Chat",
  gateway: "Gateway",
  venue_a: "Venue A",
  venue_b: "Venue B",
  processor: "Processor",
  custodian: "Custodian",
};

/** The desk around every screen: the sidebar (workspaces, what's waiting, desk status), the workspace's tabs,
 *  and the kill switch's banner and frame when it's on. */
export function Shell({ children }: { children: ReactNode }) {
  const { data: st, reload } = useQuery<Status>("/status", [], 10000);
  const toast = useDesk((s) => s.toast);
  const [confirming, setConfirming] = useState(false);
  const kill = !!st?.killSwitch.on;

  const setKill = async (on: boolean) => {
    await api.put("/kill-switch", { on });
    toast(on ? "warn" : "info", on ? "Kill switch on: agent frozen" : "Kill switch off: agent actions can execute");
    setConfirming(false);
    reload();
  };
  // ⇧K turns it on at once; turning it off always asks.
  useKeys((e) => {
    if (e.key !== "K" || !e.shiftKey) return;
    if (kill) setConfirming(true);
    else setKill(true);
    e.preventDefault();
  }, [kill]);

  return (
    <div className={`h-full flex flex-col ${kill ? "ring-2 ring-inset ring-down" : ""}`}>
      {kill && (
        <div role="alert" className="shrink-0 flex items-center gap-3 px-6 h-10 bg-down text-bg">
          <span className="font-semibold">
            Kill switch on{st?.killSwitch.at ? ` since ${time(st.killSwitch.at)}` : ""}. Nothing will be sent.
          </span>
          <span className="flex-1" />
          {confirming ? (
            <>
              <span>Let the agent's approved actions execute again?</span>
              <button className="btn border-bg text-bg hover:bg-bg/10" onClick={() => setKill(false)}>Turn off</button>
              <button className="btn border-transparent text-bg hover:bg-bg/10" onClick={() => setConfirming(false)}>Keep on</button>
            </>
          ) : (
            <button className="btn border-bg text-bg hover:bg-bg/10" onClick={() => setConfirming(true)}>Turn off…</button>
          )}
        </div>
      )}
      <div className="flex-1 flex min-h-0">
        <Sidebar st={st} kill={kill} onKill={() => (kill ? setConfirming(true) : setKill(true))} />
        <div className="flex-1 min-w-0 flex flex-col">
          <Header />
          <main className="flex-1 min-h-0 overflow-auto p-6 flex flex-col gap-4">{children}</main>
        </div>
      </div>
    </div>
  );
}

function Sidebar({ st, kill, onKill }: { st: Status | null; kill: boolean; onKill: () => void }) {
  const view = useDesk((s) => s.view);
  const openWorkspace = useDesk((s) => s.openWorkspace);
  const setView = useDesk((s) => s.setView);
  const selected = useDesk((s) => s.selectedAction);
  const selectAction = useDesk((s) => s.selectAction);
  const { data: pending } = useQuery<Action[]>("/actions?status=PENDING", ["action"]);
  const here = workspaceOf(view);
  const down = st?.systems.filter((s) => !s.up) ?? [];
  const clock = useDesk((s) => s.clock);
  const connected = useDesk((s) => s.connected);
  const shown = (pending ?? []).slice(0, 12);

  return (
    <aside aria-label="Desk" className="w-[232px] shrink-0 border-r border-line flex flex-col gap-5 px-2.5 py-4 overflow-hidden">
      <span className="text-[16px] font-semibold tracking-[-0.02em] px-2.5 py-1">LoanDesk</span>
      <nav aria-label="Workspaces" className="flex flex-col gap-px">
        {WORKSPACES.map((w) => (
          <button key={w.id} onClick={() => openWorkspace(w.id)} aria-current={here === w.id ? "page" : undefined}
            className={`flex items-center gap-2.5 h-[30px] px-2.5 rounded font-medium text-left ${
              here === w.id ? "bg-raised text-ink" : "text-dim hover:bg-raised hover:text-ink"}`}>
            <span className="flex-1">{w.label}</span>
            <span className="kbd">{w.key}</span>
          </button>
        ))}
      </nav>

      <section aria-label="Pending" className="flex flex-col gap-px flex-1 min-h-0">
        <div className="flex px-2.5 pb-1.5 label"><span className="flex-1">Pending</span><span>{pending?.length ?? "—"}</span></div>
        <div className="flex flex-col gap-px overflow-auto min-h-0">
          {shown.map((a) => {
            const tier = typeof a.payload.tier === "string" ? (a.payload.tier as string) : null;
            return (
              <button key={a.id} onClick={() => { selectAction(a.id); setView("agent"); }}
                className={`flex items-center gap-2.5 h-[30px] px-2.5 rounded text-left hover:bg-raised ${
                  selected === a.id && view === "agent" ? "bg-raised" : ""}`}>
                <span className="flex-1 min-w-0 truncate">{a.title}</span>
                <span className={`text-2xs ${tier === "HOT" || a.riskFlags.length ? "text-hot" : "text-faint"}`}>
                  {tier ? tier[0] + tier.slice(1).toLowerCase() : a.kind.replace(/_/g, " ")}
                </span>
              </button>
            );
          })}
          {(pending?.length ?? 0) > shown.length && (
            <button onClick={() => setView("agent")} className="h-[30px] px-2.5 text-left text-2xs text-accent">
              {pending!.length - shown.length} more
            </button>
          )}
          {pending && !pending.length && <span className="px-2.5 text-2xs text-faint">Nothing waiting on you.</span>}
        </div>
      </section>

      <section aria-label="Desk status" className="flex flex-col gap-2 px-2.5 pt-3 border-t border-line text-2xs">
        <Row k="Systems">
          {!st ? "—" : down.length
            ? <span className="text-down" title={down.map((s) => `${s.label} · ${s.url}`).join("\n")}>
                <span className="dot bg-down mr-1.5" />{down.map((s) => SHORT[s.system] ?? s.system).join(", ")} down</span>
            : <span title={st.systems.map((s) => s.label).join(" · ")}><span className="dot bg-up mr-1.5" />
                {st.systems.length} of {st.systems.length} up</span>}
        </Row>
        <Row k="Clock">
          <span className={connected ? "" : "text-down"} title={connected ? "" : "Disconnected: what's on screen may be stale"}>
            {clock ? time(clock.now) : "--:--:--"} NY · {!connected ? "offline" : !clock?.synced ? "unsynced"
              : `${clock.mode}${clock.mode === "live" && clock.speed !== 1 ? ` ${clock.speed}×` : ""}${clock.paused ? " · paused" : ""}`}
          </span>
        </Row>
        {st && (
          <Row k="Agent">
            <span title={Object.values(st.agent.models).join(" · ")}
              className={st.agent.mode !== "off" && st.agent.spentUsd > 0.8 * st.agent.budgetUsd ? "text-hot" : ""}>
              {kill ? "Frozen" : st.agent.mode[0].toUpperCase() + st.agent.mode.slice(1)}
              {st.agent.mode !== "off" && ` · $${st.agent.spentUsd.toFixed(2)} / $${st.agent.budgetUsd.toFixed(0)}`}
            </span>
          </Row>
        )}
        <button onClick={onKill} title="Freeze every agent action"
          className="btn justify-center mt-1 text-down border-down/25 hover:bg-down/10">
          {kill ? "Turn kill switch off" : "Kill switch"} <span className="kbd">⇧K</span>
        </button>
        <span className="text-faint leading-snug pt-1">Synthetic data. Not affiliated with EquiLend, Wematch, FIS or Bloomberg.</span>
      </section>
    </aside>
  );
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return <div className="flex justify-between gap-2"><span className="text-faint">{k}</span><span className="truncate">{children}</span></div>;
}

function Header() {
  const view = useDesk((s) => s.view);
  const setView = useDesk((s) => s.setView);
  const setPalette = useDesk((s) => s.setPalette);
  const toggleTheme = useDesk((s) => s.toggleTheme);
  const tabs = VIEWS.filter((v) => v.workspace === workspaceOf(view));
  return (
    <header className="h-[52px] shrink-0 flex items-center gap-6 px-6 border-b border-line">
      <nav aria-label="Screens" className="flex gap-6 h-full">
        {tabs.map((t) => (
          <button key={t.id} onClick={() => setView(t.id)} aria-current={t.id === view ? "page" : undefined}
            className={`h-full font-medium border-b ${t.id === view ? "text-ink border-accent" : "text-faint border-transparent hover:text-ink"}`}>
            {t.label}
          </button>
        ))}
      </nav>
      <span className="flex-1" />
      <button onClick={() => setPalette(true)}
        className="flex items-center gap-2 h-7 w-[280px] px-2.5 rounded border border-line bg-panel text-faint text-left hover:border-strong">
        <span className="flex-1">Search or run a command</span><span className="kbd">⌘K</span>
      </button>
      <button onClick={toggleTheme} className="btn-ghost btn-sm" title="Toggle theme" aria-label="Toggle theme">◐</button>
      {authEnabled && <UserButton />}
    </header>
  );
}

export function Toasts() {
  const toasts = useDesk((s) => s.toasts);
  const dismiss = useDesk((s) => s.dismiss);
  const dot = { ok: "bg-up", warn: "bg-hot", err: "bg-down", info: "bg-faint" };
  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 w-96">
      {toasts.map((t) => (
        <button key={t.id} onClick={() => dismiss(t.id)}
          className={`panel shadow-pop text-left px-4 py-2.5 flex items-start gap-2.5 ${t.tone === "err" ? "text-down" : ""}`}>
          <span className={`dot mt-1.5 ${dot[t.tone]}`} />
          <span>{t.text}</span>
        </button>
      ))}
    </div>
  );
}
