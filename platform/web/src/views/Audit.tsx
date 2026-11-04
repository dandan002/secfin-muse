import { useMemo, useState } from "react";
import { qs } from "../lib/api";
import { actor, bps, full, time } from "../lib/format";
import { useQuery } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { AuditEvent } from "../lib/types";
import { Empty, Panel, Tabs } from "../components/ui";

interface Story {
  root: string;
  ids: string[];
  events: (AuditEvent & { after: Record<string, unknown> | null })[];
}

const ENTITY: Record<string, string> = { action: "Card", trade: "Trade", order: "Order", locate: "Locate", chat: "Chat",
  agent_run: "Agent run", break: "Break" };
const FILTERS = [{ id: "all", label: "All" }, { id: "locate", label: "Locates" }, { id: "order", label: "Orders" },
  { id: "trade", label: "Trades" }, { id: "action", label: "Agent cards" }, { id: "chat", label: "Chat" }] as const;
type Who = "anyone" | "agent" | "people" | "system";

/** Who did it, in three kinds: the agent (or the model reading chat), a named trader, or the desk's code and the
 *  outside systems. A trader approving an agent's card counts as people. */
function kind(a: string | null | undefined): Exclude<Who, "anyone"> {
  if (!a) return "system";
  if (/trader:/.test(a)) return "people";
  if (/^(agent|chat:model)/.test(a)) return "agent";
  return "system";
}
const WHO_TONE = { agent: "text-dim", people: "text-ink", system: "text-faint" };

const words = (s: string) => s.replace(/_/g, " ");

/** One line on what an event changed, from the entity's state after it. */
function gist(e: Story["events"][number]): string {
  const a = e.after ?? {};
  const g = (k: string) => a[k] as never;
  if (g("error")) return String(g("error"));
  switch (e.entity) {
    case "action": return String(g("title") ?? "");
    case "trade": return [g("quantity") != null && `${full(g("quantity"))} at ${bps(g("feeBps"))}`, g("counterparty"),
      g("compareStatus") && `compare ${String(g("compareStatus")).toLowerCase()}`].filter(Boolean).join(" · ");
    case "order": return [g("side") && String(g("side")).toLowerCase(), g("quantity") != null && full(g("quantity")),
      g("composite"), g("feeBps") != null && `at ${bps(g("feeBps"))}`, g("counterparty")].filter(Boolean).join(" ");
    case "locate": return [g("identifier"), g("qtyRequested") != null && full(g("qtyRequested")), g("counterparty"),
      g("qtyApproved") ? `granted ${full(g("qtyApproved"))}` : null].filter(Boolean).join(" · ");
    case "chat": return g("text") ? `“${String(g("text")).slice(0, 120)}”` : "";
    default: return g("fields") ? JSON.stringify(g("fields")) : "";
  }
}

/** The append-only event log: every state change, who made it, and what it became; and any one trade's story. */
export function Audit() {
  const [entity, setEntity] = useState<(typeof FILTERS)[number]["id"]>("all");
  const [who, setWho] = useState<Who>("anyone");
  const [id, setId] = useState(() => useDesk.getState().auditFocus);
  const { data } = useQuery<AuditEvent[]>("/events" + qs({ entity: entity === "all" ? undefined : entity, limit: 500 }),
    ["locate", "order", "trade", "break", "action", "chat"]);
  const { data: story } = useQuery<Story>(id ? "/events/story" + qs({ id }) : null, ["locate", "order", "trade", "action"]);
  const [raw, setRaw] = useState(false);
  const rows = useMemo(() => (id && story ? story.events : data ?? [])
    .filter((e) => (entity === "all" || e.entity === entity) && (who === "anyone" || kind(e.actor) === who)),
  [data, story, id, entity, who]);

  const exportCsv = () => {
    const esc = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const body = rows.map((e) => [e.ts, e.entity, e.entityId, e.type, e.actor].map(esc).join(","));
    const url = URL.createObjectURL(new Blob([["ts,entity,id,event,actor", ...body].join("\n")], { type: "text/csv" }));
    Object.assign(document.createElement("a"), { href: url, download: id ? `story-${id}.csv` : "audit.csv" }).click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="h-full flex flex-col gap-4 min-h-0">
      <div className="flex items-center gap-4 flex-wrap">
        <Tabs value={entity} onChange={setEntity} options={FILTERS.map((f) => ({ id: f.id, label: f.label }))} />
        <span className="flex-1" />
        <label className="flex items-center gap-2">
          <span className="label">Story of</span>
          <input value={id} onChange={(e) => setId(e.target.value.trim())} placeholder="trade, order, card, locate or chat id"
            className={`input w-72 ${id ? "border-accent" : ""}`} />
          {id && <button className="btn-ghost btn-sm" onClick={() => setId("")}>Clear</button>}
        </label>
        <label className="flex items-center gap-2">
          <span className="label">Actor</span>
          <select value={who} onChange={(e) => setWho(e.target.value as Who)} className="input">
            <option value="anyone">Anyone</option><option value="agent">Agent</option>
            <option value="people">People</option><option value="system">System</option>
          </select>
        </label>
        <button className="btn" onClick={exportCsv} disabled={!rows.length}>Export CSV</button>
      </div>

      <div className="flex-1 min-h-0 flex flex-wrap gap-4 items-start">
        <Panel className="flex-[1_1_460px] min-w-0 max-h-full" bodyClass="overflow-auto"
          title={<>Events <span className="label">{id ? `${rows.length} in this story` : `${rows.length} latest`}</span></>}>
          {!rows.length ? <Empty>{id ? "No events for that id." : "No events yet."}</Empty> : (
            <table className="w-full">
              <thead><tr>{["time", "entity", "id", "event", "actor"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
              <tbody>
                {rows.map((e) => (
                  <tr key={e.id} className={`row cursor-default ${e.entityId === id ? "bg-soft" : ""}`}>
                    <td className="td text-faint">{time(e.ts)}</td>
                    <td className="td text-dim">{ENTITY[e.entity] ?? e.entity}</td>
                    <td className="td"><button className="text-accent hover:underline" title="Show this one's story"
                      onClick={() => setId(e.entityId)}>{e.entityId}</button></td>
                    <td className={`td ${/fail|unmatched|reject|flag/i.test(e.type) ? "text-down" : ""}`}>{words(e.type)}</td>
                    <td className={`td ${WHO_TONE[kind(e.actor)]}`}>{actor(e.actor)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        {id && story && (
          <Panel className="flex-[1_1_460px] min-w-0 max-h-full" bodyClass="overflow-auto"
            title={<>Story <span className="label">{id}</span></>} right={<button className="text-2xs text-accent" onClick={() => setRaw(!raw)}>{raw ? "Story" : "Raw JSON"}</button>}>
            <StoryView story={story} raw={raw} />
          </Panel>
        )}
      </div>
    </div>
  );
}

function StoryView({ story, raw }: { story: Story; raw: boolean }) {
  const ev = story.events;
  if (!ev.length) return <Empty>Nothing recorded on this id or anything it links to.</Empty>;
  if (raw) return <pre className="p-4 text-2xs text-dim whitespace-pre-wrap break-all">{JSON.stringify(ev, null, 1)}</pre>;
  const first = ev[0];
  const approver = ev.find((e) => e.entity === "action" && (e.type === "executed" || e.type === "approved" || e.type === "failed")
    && kind(e.actor) === "people");
  const lastOfRoot = [...ev].reverse().find((e) => e.entityId === story.root) ?? ev[ev.length - 1];
  const bad = /fail|unmatched|reject/i.test(lastOfRoot.type);
  return (
    <>
      <div className="grid grid-cols-4 border-b border-line">
        {[["Origin", `${ENTITY[first.entity] ?? first.entity} · ${actor(first.actor)}`],
          ["Approved by", approver ? actor(approver.actor) : "—"],
          ["Agent steps", String(ev.filter((e) => kind(e.actor) === "agent").length)],
          ["State", words(lastOfRoot.type)]].map(([k, v], i) => (
          <div key={k} className={`flex flex-col gap-1 px-4 py-3 min-w-0 ${i ? "border-l border-line" : ""}`}>
            <span className="label">{k}</span>
            <span className={`truncate first-letter:uppercase ${k === "State" && bad ? "text-down" : ""}`}>{v}</span>
          </div>
        ))}
      </div>
      <ol>
        {ev.map((e) => (
          <li key={e.id} className="grid grid-cols-[72px_1fr] gap-3 px-4 py-3 border-b border-rule">
            <span className="label">{time(e.ts)}</span>
            <span className="flex flex-col gap-0.5 min-w-0">
              <span className="flex gap-2.5 items-baseline">
                <span className={`font-medium first-letter:uppercase ${/fail|unmatched|reject|flag/i.test(e.type) ? "text-down" : ""}`}>
                  {ENTITY[e.entity] ?? e.entity} {words(e.type)}</span>
                <span className="flex-1" />
                <span className={`text-2xs ${WHO_TONE[kind(e.actor)]}`}>{actor(e.actor)}</span>
              </span>
              {gist(e) && <span className="text-dim break-words">{gist(e)}</span>}
              <span className="label">{e.entityId}</span>
            </span>
          </li>
        ))}
      </ol>
    </>
  );
}
