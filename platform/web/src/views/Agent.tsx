import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { actor, time } from "../lib/format";
import { useKeys, useQuery, useSelection } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Action } from "../lib/types";
import { Empty, Kbd, Panel, State, Tabs } from "../components/ui";

interface AgentStatus {
  mode: string;
  keyConfigured: boolean;
  budgetUsd: number;
  spentUsd: number;
  models: Record<string, string>;
  tools: string[];
  actionKinds: string[];
  killSwitch: { on: boolean };
}

/** The approval queue (spec module 6): every proposed side effect waits here for a human. */
export function Agent() {
  const [tab, setTab] = useState<"PENDING" | "ALL">("PENDING");
  const { data } = useQuery<Action[]>(`/actions?status=${tab}`, ["action"]);
  const { data: st } = useQuery<AgentStatus>("/agent/status", ["action"]);
  const toast = useDesk((s) => s.toast);
  const cards = data ?? [];
  const [sel, setSel] = useSelection(cards.length);
  const cur = cards[sel];
  const [editing, setEditing] = useState<string | null>(null);

  const act = async (a: Action, verb: "approve" | "reject" | "take-over", payload?: unknown) => {
    try {
      const r = await api.post<Action>(`/actions/${a.id}/${verb}`, payload ? { payload } : {});
      toast(r.status === "FAILED" ? "err" : "ok", `${a.title}: ${r.status.toLowerCase()}${r.status === "FAILED" ? ` (${String(r.result?.error)})` : ""}`);
      setEditing(null);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const batch = async () => {
    if (!cur) return;
    const same = cards.filter((c) => c.kind === cur.kind && c.status === "PENDING" && !c.riskFlags.length);
    for (const c of same) await act(c, "approve");
  };

  useKeys((e) => {
    if (!cur || cur.status !== "PENDING") return;
    if (e.key === " ") act(cur, "approve");
    else if (e.key === "e") setEditing(JSON.stringify(cur.payload, null, 2));
    else if (e.key === "t") act(cur, "take-over");
    else if (e.key === "x") act(cur, "reject");
    else if (e.key === "A" && e.shiftKey) batch();
    else return;
    e.preventDefault();
  }, [cur, cards]);

  useEffect(() => setEditing(null), [cur?.id]);

  return (
    <div className="h-full grid grid-cols-[1fr_360px] gap-3 min-h-0">
      <Panel bodyClass="overflow-auto p-3 space-y-2"
        title={<>approval queue <span className="text-dim normal-case tracking-normal">{cards.length}</span></>}
        right={<>
          <span className="text-2xs text-faint flex gap-3 mr-2"><span><Kbd>space</Kbd> approve</span><span><Kbd>e</Kbd> edit</span>
            <span><Kbd>x</Kbd> reject</span><span><Kbd>t</Kbd> take over</span><span><Kbd>⇧A</Kbd> batch</span></span>
          <Tabs value={tab} onChange={setTab} options={[{ id: "PENDING", label: "pending" }, { id: "ALL", label: "all" }]} />
        </>}>
        {!cards.length && (
          <Empty>
            Nothing waiting. Agent proposals (locate decisions, quotes, orders, bookings, chat replies) appear here as cards;
            nothing leaves the desk until you approve it.
            {st?.mode === "off" && <><br /><br /><span className="text-dim">The agent is off. Set LOANDESK_AGENT_MODE=replay|record|live to connect it.</span></>}
          </Empty>
        )}
        {cards.map((a, i) => (
          <article key={a.id} onClick={() => setSel(i)}
            className={`rounded-soft border px-3 py-2.5 transition-colors ${i === sel ? "border-accent/50 bg-accent/[0.04]" : "border-line/60 hover:border-line"}`}>
            <div className="flex items-center gap-2">
              <span className="label text-dim">{a.kind.replace("_", " ")}</span>
              <span className="text-ink text-sm">{a.title}</span>
              <span className="ml-auto"><State s={a.status} /></span>
            </div>
            {a.rationale && <p className="mt-1.5 text-xs text-dim leading-relaxed">{a.rationale}</p>}
            {!!a.riskFlags.length && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {a.riskFlags.map((f) => <span key={f} className="text-2xs text-hot border border-hot/40 rounded px-1.5">{f}</span>)}
              </div>
            )}
            <Diff payload={a.payload} original={a.original} />
            <div className="mt-2 flex items-center gap-2 text-2xs text-faint">
              <span>{actor(a.proposedBy)}</span><span>·</span><span>{time(a.createdAt)}</span>
              {a.approver && <><span>·</span><span>{a.status.toLowerCase()} by {actor(a.approver)}</span></>}
              {a.status === "PENDING" && (
                <span className="ml-auto flex gap-1.5">
                  <button className="btn-accent h-6" onClick={() => act(a, "approve")}>approve</button>
                  <button className="btn h-6" onClick={() => setEditing(JSON.stringify(a.payload, null, 2))}>edit</button>
                  <button className="btn h-6" onClick={() => act(a, "reject")}>reject</button>
                  <button className="btn h-6" onClick={() => act(a, "take-over")}>take over</button>
                </span>
              )}
            </div>
            {editing !== null && i === sel && (
              <div className="mt-2 space-y-2">
                <textarea value={editing} onChange={(e) => setEditing(e.target.value)} rows={8}
                  className="input w-full h-auto py-2 text-xs leading-relaxed" />
                <div className="flex gap-2">
                  <button className="btn-accent h-6" onClick={() => {
                    try { act(a, "approve", JSON.parse(editing)); } catch { toast("err", "payload is not valid JSON"); }
                  }}>approve edited</button>
                  <button className="btn h-6" onClick={() => setEditing(null)}>cancel</button>
                </div>
              </div>
            )}
          </article>
        ))}
      </Panel>

      <div className="flex flex-col gap-3 min-h-0">
        <Panel title="agent" bodyClass="p-3 space-y-3 text-xs">
          {st && (
            <>
              <div className="flex justify-between"><span className="text-faint">mode</span>
                <span className={st.mode === "off" ? "text-dim" : "text-accent"}>{st.mode}</span></div>
              <div className="flex justify-between"><span className="text-faint">model key</span><span>{st.keyConfigured ? "configured" : "none"}</span></div>
              <div className="flex justify-between"><span className="text-faint">spend</span><span>${st.spentUsd.toFixed(2)} / ${st.budgetUsd.toFixed(2)}</span></div>
              {Object.entries(st.models).map(([k, v]) => (
                <div key={k} className="flex justify-between"><span className="text-faint">{k}</span><span className="text-dim">{v}</span></div>
              ))}
              <div className="border-t rule pt-3">
                <div className="label mb-1.5">tools</div>
                <div className="flex flex-wrap gap-1.5">{st.tools.map((t) =>
                  <span key={t} className={`text-2xs border rounded px-1.5 ${t === "create_action" ? "border-accent/40 text-accent" : "border-line text-dim"}`}>{t}</span>)}</div>
                <p className="mt-2 text-faint leading-relaxed">Only create_action can lead to a side effect, and only through this queue.</p>
              </div>
            </>
          )}
        </Panel>
        <Panel title="transcript" className="flex-1" bodyClass="overflow-auto p-3">
          {!cur?.transcript.length ? <Empty>Source message, parse, tools called and model output for the selected card.</Empty> : (
            <ol className="space-y-2 text-2xs text-dim">
              {cur.transcript.map((t, i) => <li key={i} className="border-l border-line pl-2 whitespace-pre-wrap break-words">{JSON.stringify(t, null, 1)}</li>)}
            </ol>
          )}
        </Panel>
      </div>
    </div>
  );
}

function Diff({ payload, original }: { payload: Record<string, unknown>; original: Record<string, unknown> | null }) {
  const keys = Object.keys(payload);
  if (!keys.length) return null;
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-2xs">
      {keys.map((k) => {
        const was = original?.[k];
        const changed = original && was !== undefined && JSON.stringify(was) !== JSON.stringify(payload[k]);
        return [
          <dt key={k + "k"} className="text-faint">{k}</dt>,
          <dd key={k + "v"} className="text-dim truncate">
            {changed && <span className="line-through text-faint mr-2">{String(was)}</span>}
            <span className={changed ? "text-warm" : ""}>{typeof payload[k] === "object" ? JSON.stringify(payload[k]) : String(payload[k])}</span>
          </dd>,
        ];
      })}
    </dl>
  );
}
