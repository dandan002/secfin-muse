import { useMemo, useState } from "react";
import { Empty, Panel, State, Tabs } from "../components/ui";
import { api, qs } from "../lib/api";
import { actor, full, time } from "../lib/format";
import { useQuery } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Action, ChatMessage } from "../lib/types";

const FILTERS = [
  { id: "needs", label: "Needs you", status: ["UNSURE", "FLAGGED"] },
  { id: "open", label: "Open", status: ["OPEN"] },
  { id: "agreed", label: "Agreed", status: ["AGREED", "ACTIONED"] },
  { id: "all", label: "All", status: null },
] as const;

/** A room's state is the state of its latest message from the other side. */
const ROOM: Record<string, { word: string; dot: string; text: string }> = {
  FLAGGED: { word: "Blocked", dot: "bg-down", text: "text-down" },
  UNSURE: { word: "Unsure", dot: "bg-hot", text: "text-hot" },
  AGREED: { word: "Agreed", dot: "bg-up", text: "" },
  ACTIONED: { word: "Actioned", dot: "bg-up", text: "" },
  OPEN: { word: "Open", dot: "bg-faint", text: "text-dim" },
};

const price = (l: { feeBps: number | null; rate?: string | null }) =>
  l.rate === "GC" ? "GC" : l.feeBps !== null && l.feeBps !== undefined ? `${l.feeBps} bps` : "—";
const intent = (m: ChatMessage) => {
  const w = m.intent.toLowerCase().replace(/_/g, " ");
  return w[0].toUpperCase() + w.slice(1);
};

/** Chat ingest's inbox: the borrows and loans traders work in Desk Chat, as conversations, both sides (their RFQs
 *  and offers, our quotes, "Send"), what the desk made of each message, what it became, and what it blocked.
 *  Locates belong to sales. */
export function Chat() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["id"]>("needs");
  const { data, reload } = useQuery<ChatMessage[]>("/chat/messages" + qs({ limit: 300 }), ["chat"]);
  const { data: actions } = useQuery<Action[]>("/actions?status=ALL", ["action"]);
  const msgs = data ?? [];
  const wanted = FILTERS.find((f) => f.id === filter)!.status as readonly string[] | null;

  const rooms = useMemo(() => {
    const by = new Map<string, ChatMessage[]>();
    for (const m of msgs) by.set(m.streamId, [...(by.get(m.streamId) ?? []), m]);
    return [...by.entries()].map(([id, list]) => {
      const sorted = list.sort((a, b) => a.seq - b.seq);
      const theirs = sorted.filter((m) => m.side === "THEM");
      const last = sorted[sorted.length - 1];
      const flagged = sorted.some((m) => m.status === "FLAGGED");
      const state = flagged ? "FLAGGED" : theirs[theirs.length - 1]?.status ?? "OPEN";
      return { id, name: theirs[0]?.counterparty ?? last.counterparty ?? id, msgs: sorted, last, state };
    }).filter((r) => !wanted || r.msgs.some((m) => wanted.includes(m.status)))
      .sort((a, b) => b.last.sentAt.localeCompare(a.last.sentAt));
  }, [msgs, wanted]);

  const [roomId, setRoomId] = useState<string | null>(null);
  const room = rooms.find((r) => r.id === roomId) ?? rooms[0];
  const [msgId, setMsgId] = useState<string | null>(null);
  const m = room?.msgs.find((x) => x.id === msgId) ?? room?.msgs.filter((x) => x.side === "THEM").pop() ?? room?.last;
  const became = (id: string) => (actions ?? []).filter((a) => a.sourceRef === id);

  const count = (st: string[]) => msgs.filter((x) => st.includes(x.status)).length;
  return (
    <div className="flex flex-col gap-4 min-h-0">
      <div className="flex items-center gap-5">
        <Tabs value={filter} onChange={(f) => { setFilter(f); setRoomId(null); setMsgId(null); }}
          options={FILTERS.map((f) => ({ id: f.id, label: f.label }))} />
        <span className="flex-1" />
        <span className="label">Loaded</span>
        <span className="text-2xs">{msgs.length} read · {count(["AGREED", "ACTIONED"])} agreed · {count(["FLAGGED"])} blocked</span>
      </div>

      <div className="flex flex-wrap gap-4 items-start">
        <nav aria-label="Rooms" className="panel flex-[0_1_230px] min-w-[200px] overflow-hidden">
          <div className="flex items-center h-10 px-4 border-b border-line">
            <span className="text-xs font-medium flex-1">Rooms</span><span className="label">{rooms.length}</span>
          </div>
          {!rooms.length && <Empty>{filter === "needs" ? "Nothing waiting on you." : "No messages yet."}</Empty>}
          {rooms.map((r) => {
            const st = ROOM[r.state] ?? { word: r.state.toLowerCase(), dot: "bg-faint", text: "text-dim" };
            return (
              <button key={r.id} onClick={() => { setRoomId(r.id); setMsgId(null); }}
                className={`w-full grid grid-cols-[minmax(0,1fr)_auto] gap-x-2 gap-y-0.5 px-4 py-2.5 border-b border-rule text-left hover:bg-raised ${
                  r.id === room?.id ? "bg-soft hover:bg-soft" : ""}`}>
                <span className="font-medium truncate">{r.name}</span>
                <span className={`text-2xs flex items-center gap-1.5 ${st.text}`}><span className={`dot ${st.dot}`} />{st.word}</span>
                <span className="col-span-2 label truncate">{r.last.text.replace(/\n/g, " / ")}</span>
              </button>
            );
          })}
        </nav>

        {room && (
          <section aria-label="Conversation" className="panel flex-[999_1_440px] min-w-0 overflow-hidden">
            <div className="flex items-center gap-2.5 h-10 px-4 border-b border-line">
              <span className="text-xs font-medium">{room.name}</span><span className="label">{room.id}</span>
              <span className="flex-1" />
              <span className={`text-2xs flex items-center gap-1.5 ${ROOM[room.state]?.text ?? ""}`}>
                <span className={`dot ${ROOM[room.state]?.dot ?? "bg-faint"}`} />{ROOM[room.state]?.word ?? room.state}</span>
            </div>
            <div className="grid grid-cols-[64px_96px_minmax(0,1fr)_88px_40px] gap-3 px-4 py-2 label">
              <span>Time</span><span>From</span><span>Message</span><span>Read as</span><span className="text-right">Conf.</span>
            </div>
            {room.msgs.map((x) => {
              const acts = became(x.id);
              return (
                <div key={x.id}>
                  <button onClick={() => setMsgId(x.id)}
                    className={`w-full grid grid-cols-[64px_96px_minmax(0,1fr)_88px_40px] gap-3 items-baseline px-4 py-2.5 border-t border-rule text-left hover:bg-raised ${
                      x.id === m?.id ? "bg-soft hover:bg-soft" : ""}`}>
                    <span className="text-faint">{time(x.sentAt)}</span>
                    <span className="text-dim truncate">{x.side === "US" ? `Us · ${x.senderName ?? x.sender}` : x.senderName ?? x.counterparty ?? x.sender}</span>
                    <span className={`whitespace-pre-wrap break-words ${x.status === "AGREED" ? "font-semibold" : ""}`}>{x.text}</span>
                    <span className={x.status === "FLAGGED" ? "text-down" : x.status === "UNSURE" ? "text-hot" : ""}>
                      {x.status === "FLAGGED" ? "Instruction" : intent(x)}</span>
                    <span className="text-faint text-right">{x.status === "FLAGGED" ? "—" : x.confidence.toFixed(2).replace(/^0/, "")}</span>
                  </button>
                  {x.status === "FLAGGED" && (
                    <div className="flex items-center gap-2 px-4 py-2.5 border-t border-rule text-dim">
                      Tries to change the desk's rules. Nothing was parsed or sent.
                    </div>
                  )}
                  {acts.map((a) => <Became key={a.id} a={a} />)}
                </div>
              );
            })}
          </section>
        )}

        {m && <Detail key={m.id} m={m} acts={became(m.id)} onDone={reload} />}
      </div>
    </div>
  );
}

function Became({ a }: { a: Action }) {
  const select = useDesk((s) => s.selectAction);
  const setView = useDesk((s) => s.setView);
  return (
    <div className="flex items-center gap-2.5 px-4 py-2.5 border-t border-line bg-panel">
      <span className="label">Became</span>
      <button className="text-accent hover:underline" onClick={() => { select(a.id); setView("agent"); }}>{a.id}</button>
      <span className="truncate first-letter:uppercase">{a.title}</span>
      <span className="flex-1" />
      <State s={a.status} />
    </div>
  );
}

function Detail({ m, acts, onDone }: { m: ChatMessage; acts: Action[]; onDone: () => void }) {
  const toast = useDesk((s) => s.toast);
  const select = useDesk((s) => s.selectAction);
  const setView = useDesk((s) => s.setView);
  const dismiss = async () => {
    try {
      await api.post(`/chat/messages/${m.id}/dismiss`);
      onDone();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  return (
    <Panel className="flex-[1_1_240px] max-w-[300px] min-w-0"
      title={<>“{m.text.length > 28 ? m.text.slice(0, 28) + "…" : m.text}”</>} right={<span className="label">{time(m.sentAt)}</span>}
      bodyClass="p-4 flex flex-col gap-3">
      {m.status === "FLAGGED" && (
        <div className="text-down">Blocked: this message tries to change the desk's rules. Nothing was created or sent.</div>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3.5 gap-y-2.5">
        <dt className="label">Read as</dt><dd>{intent(m)} · {m.confidence.toFixed(2)}</dd>
        <dt className="label">Status</dt><dd><State s={m.status} /></dd>
        <dt className="label">Reader</dt><dd className="text-dim">{actor(m.parser)}</dd>
        <dt className="label">Room</dt><dd className="text-dim">{m.streamId} #{m.seq}{m.refersTo ? ` · answers #${m.refersTo}` : ""}</dd>
        {m.ourSide && <><dt className="label">Side</dt><dd>We {m.ourSide.toLowerCase()}</dd></>}
        {m.note && <><dt className="label">Note</dt><dd className="text-dim">{m.note}</dd></>}
        {acts.map((a) => [
          <dt key={a.id} className="label">Card</dt>,
          <dd key={a.id + "v"}><button className="text-accent hover:underline" onClick={() => { select(a.id); setView("agent"); }}>{a.id}</button></dd>,
        ])}
      </dl>
      {!!m.lines.length && (
        <table className="w-full">
          <thead><tr>{["Name", "Quantity", "Fee", ""].map((h) => <th key={h} className="label text-left font-normal pb-1">{h}</th>)}</tr></thead>
          <tbody>
            {m.lines.map((l, i) => (
              <tr key={i}>
                <td className="py-0.5 font-semibold">{l.identifier}</td>
                <td>{l.quantity ? full(l.quantity) : "—"}</td>
                <td className="text-dim">{price(l)}</td>
                <td className={l.instrumentId ? "text-hot" : "text-down"}>{l.error ?? (l.instrumentId ? "" : "Not matched")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {m.status === "AGREED" && !acts.length && <div>Agreed in chat{m.ourSide ? `: we ${m.ourSide.toLowerCase()}` : ""}. To book.</div>}
      {m.status === "ACTIONED" && <div>Recall matched to our borrows: the returns are at the gate, and the short list has redrafted.</div>}
      {["UNSURE", "FLAGGED", "OPEN", "AGREED"].includes(m.status) && (
        <div className="flex gap-2 pt-3 border-t border-line">
          <button className="btn btn-sm" onClick={dismiss}>{m.status === "FLAGGED" ? "Mark reviewed" : "Dismiss"}</button>
        </div>
      )}
    </Panel>
  );
}
