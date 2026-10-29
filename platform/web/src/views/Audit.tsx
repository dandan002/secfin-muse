import { useState } from "react";
import { qs } from "../lib/api";
import { actor, time } from "../lib/format";
import { useQuery } from "../lib/hooks";
import type { AuditEvent } from "../lib/types";
import { Empty, Panel, Tabs } from "../components/ui";

/** The append-only event log: every state change, who made it, and what it became. */
export function Audit() {
  const [entity, setEntity] = useState("all");
  const [id, setId] = useState("");
  const { data } = useQuery<AuditEvent[]>("/events" + qs({ entity: entity === "all" ? undefined : entity, id, limit: 500 }),
    ["locate", "order", "trade", "break", "action"]);
  const [open, setOpen] = useState<number | null>(null);
  const rows = data ?? [];
  return (
    <Panel className="h-full" bodyClass="overflow-auto"
      title={<>audit trail <span className="text-dim normal-case tracking-normal">{rows.length}</span></>}
      right={<>
        <input value={id} onChange={(e) => setId(e.target.value.trim())} placeholder="entity id" className="input w-48 h-6" />
        <Tabs value={entity} onChange={setEntity} options={["all", "locate", "order", "trade", "action"].map((x) => ({ id: x, label: x }))} />
      </>}>
      {!rows.length ? <Empty>No events yet.</Empty> : (
        <table className="w-full text-xs">
          <thead><tr>{["time", "entity", "id", "event", "actor"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((e) => [
              <tr key={e.id} className="row cursor-default" onClick={() => setOpen(open === e.id ? null : e.id)}>
                <td className="td text-faint">{time(e.ts)}</td>
                <td className="td text-dim">{e.entity}</td>
                <td className="td"><button className="hover:text-accent" onClick={(ev) => { ev.stopPropagation(); setId(e.entityId); }}>{e.entityId}</button></td>
                <td className="td">{e.type.replace("_", " ")}</td>
                <td className="td text-dim">{actor(e.actor)}</td>
              </tr>,
              open === e.id && (
                <tr key={e.id + "d"}><td colSpan={5} className="px-3 py-2 bg-bg/40 border-b border-line/40">
                  <pre className="text-2xs text-dim whitespace-pre-wrap break-all">{JSON.stringify(e.after, null, 2)}</pre>
                </td></tr>
              ),
            ])}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
