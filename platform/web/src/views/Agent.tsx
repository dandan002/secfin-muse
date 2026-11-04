import { ReactNode, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { actor, bps, full, time } from "../lib/format";
import { useKeys, useQuery } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Action } from "../lib/types";
import { Empty, Figures, Kbd, Panel, State } from "../components/ui";

interface AgentStatus {
  mode: string;
  keyConfigured: boolean;
  budgetUsd: number;
  spentUsd: number;
  models: Record<string, string>;
  tools: string[];
  actionKinds: string[];
  killSwitch: { on: boolean };
  graphs: string;
  tracing?: { langfuse: "on" | "off"; host: string | null };
}

interface AgentRun {
  id: string;
  task: string;
  subject: string;
  status: string;
  model: string | null;
  actionId: string | null;
  error: string | null;
  updatedAt: string;
  traceUrl?: string | null; // its last Langfuse trace, when tracing is on
}

type Step = { step?: string; ts?: string } & Record<string, unknown>;

const short = (m: string | null | undefined) => (m ?? "").replace(/^muse-/, "");
const words = (s: string) => {
  const w = s.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return w[0].toUpperCase() + w.slice(1);
};
const isPrimitive = (v: unknown) => v === null || ["string", "number", "boolean"].includes(typeof v);

/** The headline figures of a proposal, in the order a trader reads them. Anything else stays in the edit form. */
const FIGURES: [string, string, (v: unknown) => ReactNode][] = [
  ["quantity", "Quantity", (v) => full(v as number)],
  ["feeBps", "Fee", (v) => <>{bps(v as number)} <span className="label">bps</span></>],
  ["side", "Side", (v) => words(String(v))],
  ["instrument", "Name", (v) => String(v)],
  ["counterparty", "Counterparty", (v) => String(v)],
  ["venue", "Venue", (v) => String(v).replace("VENUE_", "Venue ")],
  ["decision", "Decision", (v) => words(String(v))],
];

/** The approval queue (spec module 6): every proposed side effect waits here for a human. One card in focus,
 *  its trace beside it, the rest of the queue and what was decided below. */
export function Agent() {
  const { data: all } = useQuery<Action[]>("/actions?status=ALL", ["action"]);
  const { data: st } = useQuery<AgentStatus>("/agent/status", ["action"]);
  const { data: runs } = useQuery<AgentRun[]>(st?.mode === "off" ? null : "/agent/runs?limit=12", ["agent_run"]);
  const toast = useDesk((s) => s.toast);
  const clock = useDesk((s) => s.clock);
  const selected = useDesk((s) => s.selectedAction);
  const select = useDesk((s) => s.selectAction);
  const killed = !!st?.killSwitch.on;

  const pending = useMemo(() => (all ?? []).filter((a) => a.status === "PENDING")
    // Risk first: anything flagged, then Hot names, then oldest first.
    .sort((a, b) => Number(!!b.riskFlags.length) - Number(!!a.riskFlags.length)
      || Number(b.payload.tier === "HOT") - Number(a.payload.tier === "HOT")
      || a.createdAt.localeCompare(b.createdAt)), [all]);
  const decided = useMemo(() => (all ?? []).filter((a) => a.status !== "PENDING"), [all]);
  const idx = Math.max(0, pending.findIndex((a) => a.id === selected));
  const cur = pending[idx];
  const [editing, setEditing] = useState<Record<string, string> | null>(null);
  useEffect(() => setEditing(null), [cur?.id]);

  const act = async (a: Action, verb: "approve" | "reject" | "take-over", payload?: unknown) => {
    try {
      const r = await api.post<Action>(`/actions/${a.id}/${verb}`, payload ? { payload } : {});
      toast(r.status === "FAILED" ? "err" : "ok", `${a.title}: ${r.status.toLowerCase()}${r.status === "FAILED" ? ` (${String(r.result?.error)})` : ""}`);
      setEditing(null);
      const next = pending[idx + 1] ?? pending[idx - 1];
      select(next && next.id !== a.id ? next.id : null);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const startEdit = (a: Action) => setEditing(Object.fromEntries(Object.entries(a.payload).map(([k, v]) =>
    [k, isPrimitive(v) ? String(v ?? "") : JSON.stringify(v)])));
  const approveEdited = (a: Action) => {
    if (!editing) return;
    try {
      const payload = Object.fromEntries(Object.entries(a.payload).map(([k, was]) => {
        const v = editing[k];
        if (typeof was === "number") return [k, v === "" ? null : Number(v.replace(/[,_]/g, ""))];
        if (typeof was === "boolean") return [k, v === "true"];
        if (isPrimitive(was)) return [k, v === "" && was === null ? null : v];
        return [k, JSON.parse(v)];
      }));
      act(a, "approve", payload);
    } catch {
      toast("err", "a field isn't valid: check the lists and objects");
    }
  };
  const [confirmBatch, setConfirmBatch] = useState(false);
  const unflagged = pending.filter((a) => !a.riskFlags.length);
  const batch = async () => {
    const ids = unflagged.map((a) => a.id);
    setConfirmBatch(false);
    if (!ids.length) return toast("info", "nothing unflagged to batch-approve");
    let ok = 0;
    for (const id of ids) {
      try {
        await api.post(`/actions/${id}/approve`, {});
        ok++;
      } catch { /* left pending; the toast counts it */ }
    }
    toast(ok === ids.length ? "ok" : "warn", `approved ${ok} of ${ids.length}`);
  };

  useKeys((e) => {
    if (e.key === "ArrowDown" || e.key === "j") select(pending[Math.min(idx + 1, pending.length - 1)]?.id ?? null);
    else if (e.key === "ArrowUp" || e.key === "k") select(pending[Math.max(idx - 1, 0)]?.id ?? null);
    else if (!cur || killed || editing) return;
    else if (e.key === " ") act(cur, "approve");
    else if (e.key === "e") startEdit(cur);
    else if (e.key === "t") act(cur, "take-over");
    else if (e.key === "x") act(cur, "reject");
    else if (e.key === "A" && e.shiftKey) setConfirmBatch(true);
    else return;
    e.preventDefault();
  }, [cur, pending, idx, killed, editing]);

  const today = clock?.nowLocal.slice(0, 10);
  const todays = decided.filter((a) => !today || a.createdAt.slice(0, 10) === today);
  const oldest = pending.length ? pending.reduce((a, b) => (a.createdAt < b.createdAt ? a : b)) : null;
  const age = oldest && clock ? Math.max(0, (Date.parse(clock.now) - Date.parse(oldest.createdAt)) / 1000) : null;

  return (
    <div className="flex flex-col gap-4">
      <Figures items={[
        { label: "Waiting", value: pending.length },
        { label: "Flagged or Hot", value: pending.filter((a) => a.riskFlags.length || a.payload.tier === "HOT").length,
          tone: pending.some((a) => a.riskFlags.length || a.payload.tier === "HOT") ? "text-hot" : "" },
        { label: "Oldest", value: age == null ? "—" : `${Math.floor(age / 60)}:${String(Math.floor(age % 60)).padStart(2, "0")}` },
        { label: "Approved today", value: todays.filter((a) => a.status === "APPROVED" || a.status === "EXECUTED").length },
        { label: "Rejected today", value: todays.filter((a) => a.status === "REJECTED").length },
      ]} />

      <div className="flex flex-wrap gap-4 items-start">
        {!cur ? (
          <div className="panel flex-[999_1_520px] min-w-0">
            <Empty>
              Nothing waiting. Agent proposals (locate decisions, quotes, orders, bookings, chat replies) appear here;
              nothing leaves the desk until you approve it.
              {st?.mode === "off" && <><br /><br />The agent is off. Set LOANDESK_AGENT_MODE=replay|record|live to connect it.</>}
            </Empty>
          </div>
        ) : (
          <Card a={cur} killed={killed} editing={editing} setEditing={setEditing}
            onApprove={() => (editing ? approveEdited(cur) : act(cur, "approve"))} onEdit={() => startEdit(cur)}
            onReject={() => act(cur, "reject")} onTakeOver={() => act(cur, "take-over")} />
        )}
        <Trace a={cur} traceUrl={(runs ?? []).find((r) => r.actionId && r.actionId === cur?.id)?.traceUrl} />
      </div>

      {pending.length > 1 && (
        <Panel title={<>Queue <span className="label">{pending.length}</span></>}
          right={confirmBatch ? (
            <span className="flex items-center gap-2">
              <span>Approve {unflagged.length} unflagged? Flagged cards stay.</span>
              <button className="btn-accent btn-sm" disabled={killed} onClick={batch}>Approve {unflagged.length}</button>
              <button className="btn-ghost btn-sm" onClick={() => setConfirmBatch(false)}>Cancel</button>
            </span>
          ) : (
            <span className="flex items-center gap-3">
              <span className="kbd">J/K move</span>
              <button className="btn btn-sm" disabled={killed || !unflagged.length} onClick={() => setConfirmBatch(true)}>
                Approve unflagged <Kbd>⇧A</Kbd></button>
            </span>
          )}>
          <ul>
            {pending.map((a) => (
              <li key={a.id}>
                <button onClick={() => select(a.id)}
                  className={`w-full grid grid-cols-[72px_96px_1fr_auto] gap-3 items-center h-9 px-4 border-b border-rule text-left hover:bg-raised ${
                    a.id === cur?.id ? "bg-soft hover:bg-soft" : ""}`}>
                  <span className="text-faint">{time(a.createdAt)}</span>
                  <span className="text-dim">{words(a.kind)}</span>
                  <span className="truncate">{a.title}</span>
                  <span className={a.riskFlags.length ? "text-hot" : "text-faint"}>
                    {a.riskFlags.length ? `${a.riskFlags.length} flag${a.riskFlags.length > 1 ? "s" : ""}` : actor(a.proposedBy)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <div className="flex flex-wrap gap-4 items-start">
        <Panel className="flex-[999_1_520px] min-w-0" title="Recently decided">
          {!decided.length ? <Empty>No decisions yet.</Empty> : (
            <ul>
              {decided.slice(0, 8).map((a) => (
                <li key={a.id} className="grid grid-cols-[72px_96px_1fr_110px_110px] gap-3 items-center h-9 px-4 border-b border-rule">
                  <span className="text-faint">{time(a.createdAt)}</span>
                  <span className="text-dim">{words(a.kind)}</span>
                  <span className="truncate">{a.title}</span>
                  <span className="text-dim truncate">{actor(a.approver)}</span>
                  <span className="justify-self-end"><State s={a.status} /></span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        {st && <AgentPanel st={st} runs={runs ?? []} cur={cur} />}
      </div>
    </div>
  );
}

function Card({ a, killed, editing, setEditing, onApprove, onEdit, onReject, onTakeOver }: {
  a: Action;
  killed: boolean;
  editing: Record<string, string> | null;
  setEditing: (e: Record<string, string> | null) => void;
  onApprove: () => void;
  onEdit: () => void;
  onReject: () => void;
  onTakeOver: () => void;
}) {
  const tier = typeof a.payload.tier === "string" ? a.payload.tier : null;
  const proposed = (a.transcript as Step[]).find((s) => s.step === "proposed");
  const figs = FIGURES.filter(([k]) => a.payload[k] !== undefined && a.payload[k] !== null).slice(0, 5);
  const lines = Array.isArray(a.payload.lines) ? a.payload.lines as { instrument: string; quantity: number; feeBps: number }[] : null;
  return (
    <article className="panel flex-[999_1_520px] min-w-0" aria-label={a.title}>
      <div className="flex items-center gap-2.5 h-10 px-4 border-b border-line">
        <span className="text-xs font-medium">{words(a.kind)}</span>
        <span className="label">{a.id}</span>
        {tier && <span className={`text-2xs ${tier === "HOT" ? "text-hot" : "text-dim"}`}>{words(tier)}</span>}
        <span className="flex-1" />
        <span className="text-2xs text-dim">
          {actor(a.proposedBy)}{proposed?.model ? ` · ${short(String(proposed.model))}` : ""} · {time(a.createdAt)}
        </span>
      </div>
      <div className="p-4 flex flex-col gap-1.5">
        <span className="text-md font-semibold first-letter:uppercase">{a.title}</span>
        {a.rationale && <span className="text-dim whitespace-pre-line">{a.rationale}</span>}
      </div>

      {!editing && !!figs.length && (
        <div className="grid border-y border-line" style={{ gridTemplateColumns: `repeat(${figs.length}, minmax(0, 1fr))` }}>
          {figs.map(([k, label, fmt], i) => {
            const was = a.original?.[k];
            const changed = was !== undefined && isPrimitive(was) && was !== a.payload[k];
            return (
              <div key={k} className={`flex flex-col gap-1.5 px-4 py-3.5 min-w-0 ${i ? "border-l border-line" : ""}`}>
                <span className="label">{label}</span>
                <span className="fig truncate">
                  {changed && <span className="text-faint line-through text-md mr-2">{String(was)}</span>}
                  {fmt(a.payload[k])}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {editing ? (
        <div className="p-4 border-y border-line grid grid-cols-2 gap-x-4 gap-y-3">
          {Object.keys(editing).map((k) => {
            const nested = !isPrimitive(a.payload[k]);
            return (
              <label key={k} className={`flex flex-col gap-1 ${nested ? "col-span-2" : ""}`}>
                <span className="label">{words(k)}</span>
                {nested
                  ? <textarea value={editing[k]} rows={3} onChange={(e) => setEditing({ ...editing, [k]: e.target.value })}
                      className="input h-auto py-1.5 font-[ui-monospace,Menlo,monospace] text-2xs" />
                  : <input value={editing[k]} onChange={(e) => setEditing({ ...editing, [k]: e.target.value })}
                      className={`input ${typeof a.payload[k] === "number" ? "text-right" : ""}`} />}
              </label>
            );
          })}
        </div>
      ) : (
        <div className="grid grid-cols-2">
          <div className="px-4 py-3.5 grid grid-cols-[14px_1fr] gap-2 content-start">
            <span className="label col-span-2">Checks</span>
            {a.riskFlags.length ? a.riskFlags.map((f) => [
              <span key={f + "i"} className={f.startsWith("BLOCKED") ? "text-down" : "text-hot"}>!</span>,
              <span key={f} className={f.startsWith("BLOCKED") ? "text-down" : ""}>{f.replace(/^(BLOCKED|warning): /, "")}</span>,
            ]) : <><span className="text-up">✓</span><span>No risk flags</span></>}
          </div>
          <div className="px-4 py-3.5 border-l border-line flex flex-col gap-2 min-w-0">
            {lines ? (
              <>
                <span className="label">Lines · one Venue B list</span>
                <table className="w-full">
                  <thead><tr>{["Name", "Quantity", "Limit"].map((h) => <th key={h} className="label text-left font-normal pb-1">{h}</th>)}</tr></thead>
                  <tbody>{lines.map((x) => (
                    <tr key={x.instrument}><td className="py-0.5">{x.instrument}</td><td>{full(x.quantity)}</td><td>{bps(x.feeBps)}</td></tr>
                  ))}</tbody>
                </table>
              </>
            ) : (
              <>
                <span className="label">Will send</span>
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                  {Object.entries(a.payload).filter(([k, v]) => isPrimitive(v) && !FIGURES.some(([f]) => f === k)).slice(0, 6).map(([k, v]) => [
                    <dt key={k} className="text-faint">{words(k)}</dt>,
                    <dd key={k + "v"} className="truncate">{String(v ?? "—")}</dd>,
                  ])}
                </dl>
              </>
            )}
          </div>
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap px-4 py-3 border-t border-line">
        <button className="btn-accent" disabled={killed} onClick={onApprove}>
          {editing ? "Approve edited" : "Approve"} <Kbd>Space</Kbd>
        </button>
        {editing
          ? <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
          : <button className="btn" disabled={killed} onClick={onEdit}>Edit <Kbd>E</Kbd></button>}
        <button className="btn-ghost" onClick={onReject}>Reject <Kbd>X</Kbd></button>
        <button className="btn-ghost" onClick={onTakeOver}>Take over <Kbd>T</Kbd></button>
        {killed && <span className="text-down ml-auto">Kill switch on: approving is frozen.</span>}
      </div>
    </article>
  );
}

/** The card's trace: source, parse, tools, checks, escalation and proposal as readable steps; raw JSON on request. */
function Trace({ a, traceUrl }: { a: Action | undefined; traceUrl?: string | null }) {
  const [raw, setRaw] = useState(false);
  const steps = (a?.transcript ?? []) as Step[];
  return (
    <aside aria-label="Trace" className="panel flex-[1_1_300px] max-w-[380px] min-w-0">
      <div className="flex items-center h-10 px-4 border-b border-line">
        <span className="text-xs font-medium flex-1">Trace</span>
        {traceUrl && <a href={traceUrl} target="_blank" rel="noreferrer" className="text-2xs text-accent mr-3"
          title="The model calls, tokens, cost and graph steps behind this card">Langfuse ↗</a>}
        {!!steps.length && <button className="text-2xs text-accent" onClick={() => setRaw(!raw)}>{raw ? "Steps" : "Raw"}</button>}
      </div>
      {!steps.length ? <Empty>Source message, parse, tools called and model output for the card in focus.</Empty>
        : raw ? <pre className="p-4 text-2xs text-dim whitespace-pre-wrap break-words max-h-[480px] overflow-auto">{JSON.stringify(steps, null, 1)}</pre>
        : steps.map((s, i) => (
          <div key={i} className="grid grid-cols-[60px_72px_1fr] gap-2.5 px-4 py-2.5 border-b border-rule">
            <span className="label">{s.ts ? time(s.ts) : ""}</span>
            <span className="text-2xs text-dim">{s.step ? words(s.step) : "Step"}</span>
            <span className="break-words min-w-0">{summarise(s)}</span>
          </div>
        ))}
    </aside>
  );
}

function summarise(s: Step) {
  return Object.entries(s).filter(([k]) => k !== "step" && k !== "ts").map(([k, v]) => {
    const val = Array.isArray(v) ? (v.length ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(", ") : "none")
      : typeof v === "object" && v !== null ? JSON.stringify(v) : String(v);
    return `${words(k)}: ${val.length > 140 ? val.slice(0, 140) + "…" : val}`;
  }).join(" · ") || "—";
}

function AgentPanel({ st, runs, cur }: { st: AgentStatus; runs: AgentRun[]; cur: Action | undefined }) {
  return (
    <Panel title="Agent" className="flex-[1_1_300px] max-w-[380px] min-w-0" bodyClass="p-4 flex flex-col gap-2">
      <div className="flex justify-between"><span className="text-faint">Mode</span><span>{words(st.mode)}</span></div>
      <div className="flex justify-between"><span className="text-faint">Model key</span><span>{st.keyConfigured ? "Configured" : "None"}</span></div>
      <div className="flex justify-between"><span className="text-faint">Graphs</span>
        <span className={st.graphs === "ready" ? "" : "text-hot"} title={st.graphs}>{st.graphs === "ready" ? "Ready" : "Not installed"}</span></div>
      <div className="flex justify-between"><span className="text-faint">Spend</span><span>${st.spentUsd.toFixed(2)} / ${st.budgetUsd.toFixed(2)}</span></div>
      <div className="flex justify-between"><span className="text-faint">Tracing</span>
        {st.tracing?.langfuse === "on" && st.tracing.host
          ? <a href={st.tracing.host} target="_blank" rel="noreferrer" className="text-accent">Langfuse ↗</a>
          : <span className="text-dim" title="Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, with the agent in record or live">Off</span>}</div>
      {Object.entries(st.models).map(([k, v]) => (
        <div key={k} className="flex justify-between"><span className="text-faint">{words(k)}</span><span className="text-dim">{v}</span></div>
      ))}
      <div className="label pt-2 border-t border-line mt-1">Tools · only create_action can lead to a side effect, and only through this queue</div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-2xs">
        {st.tools.map((t) => <span key={t} className={t === "create_action" ? "text-accent" : "text-dim"}>{t}</span>)}
      </div>
      {!!runs.length && (
        <>
          <div className="label pt-2 border-t border-line mt-1">Runs</div>
          <ul className="flex flex-col gap-1 text-2xs">
            {runs.map((r) => (
              <li key={r.id} title={r.error ?? r.id}
                className={`flex items-center gap-2 px-1 rounded ${r.actionId && r.actionId === cur?.id ? "bg-soft" : ""}`}>
                <span className="text-dim">{words(r.task)}</span>
                <span className="text-faint truncate">{r.subject}</span>
                <span className="ml-auto text-faint">{short(r.model)}</span>
                {r.traceUrl && <a href={r.traceUrl} target="_blank" rel="noreferrer" className="text-accent" title="Langfuse trace">↗</a>}
                <State s={r.status} />
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}
