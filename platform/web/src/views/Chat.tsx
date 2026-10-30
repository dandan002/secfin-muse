import { useState } from "react";
import { Empty, Panel, Tabs } from "../components/ui";
import { api, qs } from "../lib/api";
import { actor, full, qty, time } from "../lib/format";
import { useQuery } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { ChatMessage } from "../lib/types";

const FILTERS = [
  { id: "needs", label: "needs you", status: "UNSURE,FLAGGED" },
  { id: "open", label: "open", status: "OPEN" },
  { id: "agreed", label: "agreed", status: "AGREED,ACTIONED" },
  { id: "all", label: "all", status: undefined },
] as const;

const STATUS_TONE: Record<string, string> = {
  UNSURE: "text-warm",
  FLAGGED: "text-hot",
  OPEN: "text-accent",
  AGREED: "text-up",
  ACTIONED: "text-up",
  OURS: "text-faint",
};

const price = (l: { feeBps: number | null; rate?: string | null }) =>
  l.rate === "GC" ? "GC" : l.feeBps !== null && l.feeBps !== undefined ? `${l.feeBps} bps` : "—";

/** Chat ingest's inbox: the borrows and loans traders work in Desk Chat, both sides of each conversation
 *  (their RFQs and offers, our quotes, "Send"), what the desk made of each, trades agreed in chat, and what it
 *  wasn't sure of. Locates belong to sales. */
export function Chat() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["id"]>("needs");
  const status = FILTERS.find((f) => f.id === filter)?.status;
  const { data, reload } = useQuery<ChatMessage[]>("/chat/messages" + qs({ status, limit: 300 }), ["chat"]);
  const rows = data ?? [];
  const [sel, setSel] = useState<string | null>(null);
  const m = rows.find((r) => r.id === sel) ?? rows[0];
  return (
    <div className="h-full grid grid-cols-[minmax(0,1fr)_380px] gap-3">
      <Panel bodyClass="overflow-auto"
        title={<>desk chat <span className="text-dim normal-case tracking-normal">{rows.length}</span></>}
        right={<Tabs value={filter} onChange={setFilter} options={FILTERS.map((f) => ({ id: f.id, label: f.label }))} />}>
        {!rows.length ? <Empty>{filter === "needs" ? "Nothing waiting on you." : "No messages yet."}</Empty> : (
          <table className="w-full text-xs">
            <thead><tr>{["time", "from", "room", "message", "read as", "conf", "status"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className={`row cursor-default ${m?.id === r.id ? "row-sel" : ""}`} onClick={() => setSel(r.id)}>
                  <td className="td text-faint">{time(r.sentAt)}</td>
                  <td className={`td ${r.side === "US" ? "text-dim" : ""}`}>{r.side === "US" ? `us · ${r.sender}` : r.counterparty ?? r.sender}</td>
                  <td className="td text-dim">{r.streamId}</td>
                  <td className="td max-w-[420px] truncate" title={r.text}>{r.text.replace(/\n/g, " / ")}</td>
                  <td className="td text-dim">{r.intent.toLowerCase().replace("_", " ")}</td>
                  <td className="td text-dim tabular-nums">{r.confidence.toFixed(2)}</td>
                  <td className={`td ${STATUS_TONE[r.status] ?? "text-faint"}`}>{r.status.toLowerCase()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {m ? <Detail key={m.id} m={m} onDone={reload} /> : <Panel title="message"><Empty>Select a message.</Empty></Panel>}
    </div>
  );
}

function Detail({ m, onDone }: { m: ChatMessage; onDone: () => void }) {
  const toast = useDesk((s) => s.toast);
  const dismiss = async () => {
    try {
      await api.post(`/chat/messages/${m.id}/dismiss`);
      onDone();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  return (
    <Panel title={<>{m.senderName ?? m.sender} <span className="text-dim normal-case tracking-normal">{m.side === "US" ? "our desk" : m.counterparty}</span></>}
      bodyClass="overflow-auto p-3 space-y-3 text-xs">
      <pre className="whitespace-pre-wrap break-words text-ink bg-bg/40 border border-line/50 rounded-md p-2">{m.text}</pre>
      {m.status === "FLAGGED" && (
        <div className="text-hot border border-hot/40 bg-hot/[0.06] rounded-md p-2">
          Flagged: this message tries to change the desk's rules. Nothing was created or sent.
        </div>
      )}
      <dl className="grid grid-cols-[90px_1fr] gap-y-1">
        <dt className="label">read as</dt><dd>{m.intent.toLowerCase().replace("_", " ")} · {m.confidence.toFixed(2)}</dd>
        <dt className="label">by</dt><dd className="text-dim">{actor(m.parser)}</dd>
        <dt className="label">room</dt><dd className="text-dim">{m.streamId} #{m.seq}{m.refersTo ? ` · answers #${m.refersTo}` : ""}</dd>
        {m.ourSide && <><dt className="label">we would</dt><dd>{m.ourSide.toLowerCase()}</dd></>}
        {m.note && <><dt className="label">note</dt><dd className="text-dim">{m.note}</dd></>}
      </dl>
      {!!m.lines.length && (
        <table className="w-full">
          <thead><tr>{["identifier", "qty", "fee", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>
            {m.lines.map((l, i) => (
              <tr key={i} className="row">
                <td className="td">{l.identifier}</td>
                <td className="td tabular-nums" title={full(l.quantity)}>{l.quantity ? qty(l.quantity) : "—"}</td>
                <td className="td tabular-nums text-dim">{price(l)}</td>
                <td className={`td ${l.instrumentId ? "text-warm" : "text-down"}`}>{l.error ?? (l.instrumentId ? "" : "not matched")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {m.status === "AGREED" && (
        <div className="text-up border border-up/40 bg-up/[0.06] rounded-md p-2">
          Agreed in chat{m.ourSide ? `: we ${m.ourSide.toLowerCase()}` : ""}. To book.
        </div>
      )}
      {m.status === "ACTIONED" && (
        <div className="text-up border border-up/40 bg-up/[0.06] rounded-md p-2">
          Recall matched to our borrows: the returns are at the gate, and the short list has redrafted.
        </div>
      )}
      {["UNSURE", "FLAGGED", "OPEN", "AGREED"].includes(m.status) && (
        <div className="flex gap-2 pt-3 border-t rule">
          <button className="btn" onClick={dismiss}>dismiss</button>
        </div>
      )}
    </Panel>
  );
}
