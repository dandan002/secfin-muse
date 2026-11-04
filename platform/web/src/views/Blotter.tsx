import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { actor, bps, full, money, qty, time } from "../lib/format";
import { useKeys, useQuery, useSelection } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Order, Trade } from "../lib/types";
import { Empty, Figures, Kbd, Panel, Pipeline, State, Tabs } from "../components/ui";

interface Blotter {
  orders: Order[];
  trades: Trade[];
  counts: Record<string, number>;
}

const OPEN = ["NEW", "SENT", "PENDING", "COUNTERED"];

/** Where an order stands: ordered → routed → filled. Booking and on happen on the fill. */
function orderStage(o: Order): { at: number; fail?: number } {
  if (o.status === "REJECTED" || o.status === "FAILED") return { at: 1, fail: 1 };
  if (o.status === "NEW") return { at: 0 };
  if (OPEN.includes(o.status)) return { at: 1 };
  if (o.status === "FILLED" || o.status === "PARTIAL") return { at: 3 };
  return { at: 1 };
}

/** Where a fill stands: booked → compared → settled, or which of those broke. */
function tradeStage(t: Trade): { at: number; fail?: number } {
  if (t.compareStatus === "UNBOOKED") return { at: 3 };
  if (t.compareStatus === "FAILED") return { at: 3, fail: 3 };
  if (t.compareStatus === "UNMATCHED") return { at: 4, fail: 4 };
  if (t.compareStatus !== "MATCHED") return { at: 4 };
  if (t.settleStatus === "SETTLED") return { at: 6 };
  if (t.settleStatus === "FAILING" || t.settleStatus === "FAILED") return { at: 5, fail: 5 };
  return { at: 5 };
}

/** An action that needs a number, asked inline under its row instead of in a browser prompt. */
interface Ask {
  id: string;
  title: string;
  label: string;
  value: string;
  hint?: (v: string) => string;
  send: (v: string) => void;
}

function AskRow({ ask, cols, onClose }: { ask: Ask; cols: number; onClose: () => void }) {
  const [v, setV] = useState(ask.value);
  const submit = () => {
    if (v.trim()) ask.send(v.trim());
    onClose();
  };
  return (
    <tr>
      <td colSpan={cols} className="px-3 py-2.5 bg-raised border-b border-rule">
        <form className="sticky left-0 w-fit flex items-center gap-3.5 flex-wrap" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          <span className="text-xs font-medium">{ask.title}</span>
          <label className="flex items-center gap-2">
            <span className="label">{ask.label}</span>
            <input autoFocus value={v} onChange={(e) => setV(e.target.value)} aria-label={ask.label}
              onKeyDown={(e) => e.key === "Escape" && onClose()} className="input w-24 text-right" />
          </label>
          {ask.hint && <span className="text-faint">{ask.hint(v)}</span>}
          <button className="btn-accent btn-sm">Send <Kbd>↵</Kbd></button>
          <button type="button" className="btn-ghost btn-sm" onClick={onClose}>Cancel <Kbd>Esc</Kbd></button>
        </form>
      </td>
    </tr>
  );
}

export function BlotterView() {
  const { data } = useQuery<Blotter>("/blotter", ["order", "trade", "break"]);
  const [filter, setFilter] = useState<"all" | "open" | "done">("all");
  const [cp, setCp] = useState("");
  const [ask, setAsk] = useState<Ask | null>(null);
  const toast = useDesk((s) => s.toast);
  const orders = useMemo(() => (data?.orders ?? []).filter((o) =>
    (filter === "all" || (filter === "open") === OPEN.includes(o.status)) && (!cp || (o.counterparty ?? "").includes(cp.toUpperCase()))),
  [data, filter, cp]);
  const [sel, setSel] = useSelection(orders.length);
  const cur = orders[sel];

  const cmd = async (o: Order, c: "accept" | "cancel" | "counter", body?: object) => {
    try {
      const r = await api.post<Order>(`/orders/${o.id}/${c}`, body);
      toast("ok", `${o.composite} ${c} → ${r.status.toLowerCase()}`);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const counter = (o: Order) => setAsk({
    id: o.id, title: `Counter ${o.composite}`, label: "Fee bps", value: String(o.counterFeeBps ?? o.feeBps),
    send: (v) => cmd(o, "counter", { feeBps: parseFloat(v) }),
  });

  useKeys((e) => {
    if (!cur || ask) return;
    if (e.key === "x") cmd(cur, "cancel");
    else if (e.key === "a" && cur.status === "COUNTERED") cmd(cur, "accept");
    else if (e.key === "r") counter(cur);
    else return;
    e.preventDefault();
  }, [cur, ask]);

  const c = data?.counts;
  const bad = (n: number | undefined) => (n ? "text-down" : "");
  return (
    <div className="flex flex-col gap-4">
      <Figures items={[
        { label: "Open orders", value: c?.openOrders ?? "—" },
        { label: "Open loans", value: c?.openLoans ?? "—" },
        { label: "Unbooked", value: c?.unbooked ?? "—", tone: c?.unbooked ? "text-hot" : "" },
        { label: "Breaks", value: c?.breaks ?? "—", tone: bad(c?.breaks) },
        { label: "Settle fails", value: c?.unsettled ?? "—", tone: bad(c?.unsettled) },
        { label: "Open locates", value: c?.openLocates ?? "—" },
      ]} />

      <Panel bodyClass="overflow-auto max-h-[44vh]"
        title={<>Orders <span className="label">{orders.length}</span></>}
        right={<>
          <span className="kbd mr-2">A accept counter · R counter · X cancel</span>
          <input value={cp} onChange={(e) => setCp(e.target.value)} placeholder="Counterparty" className="input w-28 h-6" />
          <Tabs value={filter} onChange={setFilter} options={[{ id: "all", label: "All" }, { id: "open", label: "Open" }, { id: "done", label: "Done" }]} />
        </>}>
        {!orders.length ? <p className="px-4 py-6 text-faint">No orders yet. Send one from the Ticket, or ⌘K "borrow 100k AAPL @ 30".</p> : (
          <table className="w-full">
            <thead><tr>{["time", "side", "name", "type", "quantity", "fee", "counter", "filled", "counterparty", "venue", "lifecycle", "status", "by", "note", ""].map((h) =>
              <th key={h} className={`th ${["quantity", "fee", "counter", "filled"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
            <tbody>
              {orders.map((o, i) => [
                <tr key={o.id} onClick={() => setSel(i)} className={`row ${i === sel ? "row-sel" : ""}`}>
                  <td className="td text-faint">{time(o.createdAt)}</td>
                  <td className="td text-dim">{o.side === "BORROW" ? "Borrow" : "Lend"}</td>
                  <td className="td font-semibold">{o.composite}</td>
                  <td className="td text-dim">{o.type.toLowerCase().replace("_", " ")}</td>
                  <td className="td text-right">{full(o.quantity)}</td>
                  <td className="td text-right">{bps(o.feeBps)}</td>
                  <td className="td text-right text-hot">{o.counterFeeBps ? `${bps(o.counterFeeBps)}${o.counterQty ? ` × ${qty(o.counterQty)}` : ""}` : ""}</td>
                  <td className="td text-right text-dim">{o.filledQty ? qty(o.filledQty) : ""}</td>
                  <td className="td text-dim">{o.counterparty ?? "Best"}</td>
                  <td className="td text-dim" title={o.basketId ? `basket ${o.basketId}, line ${o.lineNo}: one Venue B list` : ""}>
                    {o.venue.replace("VENUE_", "")}{o.basketId && <span className="ml-1 text-faint">basket</span>}</td>
                  <td className="td"><Pipeline {...orderStage(o)} /></td>
                  <td className="td"><State s={o.status} /></td>
                  <td className="td text-faint">{actor(o.createdBy)}</td>
                  <td className="td text-faint max-w-[240px] truncate" title={o.rejectReason ?? o.routeReason}>{o.rejectReason ?? o.routeReason}</td>
                  <td className="td text-right whitespace-nowrap">
                    {o.status === "COUNTERED" && <button className="btn-ghost btn-sm" onClick={() => cmd(o, "accept")}>Accept</button>}
                    {OPEN.includes(o.status) && <>
                      <button className="btn-ghost btn-sm" onClick={() => counter(o)}>Counter</button>
                      <button className="btn-ghost btn-sm" onClick={() => cmd(o, "cancel")}>Cancel</button>
                    </>}
                  </td>
                </tr>,
                ask?.id === o.id && <AskRow key={o.id + "-ask"} ask={ask} cols={15} onClose={() => setAsk(null)} />,
              ])}
            </tbody>
          </table>
        )}
      </Panel>

      <Fills trades={data?.trades ?? []} ask={ask} setAsk={setAsk} />
    </div>
  );
}

/** A lender's recall on the borrow: owed, on its way back, or stuck. It stands until the return settles. */
function RecallTag({ t, today }: { t: Trade; today: string | undefined }) {
  const overdue = !!(t.recallDue && today && t.recallDue < today);
  const what = { RECALLED: `${qty(t.recalledQty)} recalled`, RETURNING: "returning", RETURN_FAILING: "return failing",
    RETURN_NOT_SENT: "return not instructed" }[t.recallStatus ?? "RECALLED"];
  const bad = overdue || t.recallStatus === "RETURN_FAILING" || t.recallStatus === "RETURN_NOT_SENT";
  return (
    <span className={`ml-2 ${bad ? "text-down" : "text-hot"}`}
      title={`${t.recallRef ? `ref ${t.recallRef}. ` : ""}The recall stands until the shares settle back with the lender; not back by the due date, the lender can buy them in at our cost.`}>
      {what}{t.recallDue ? `, due ${t.recallDue}` : ""}{overdue ? " · overdue" : ""}
    </span>
  );
}

function Fills({ trades, ask, setAsk }: { trades: Trade[]; ask: Ask | null; setAsk: (a: Ask | null) => void }) {
  const toast = useDesk((s) => s.toast);
  const today = useDesk((s) => s.clock?.nowLocal.slice(0, 10));  // the sim's New York date
  // Exceptions first: breaks, settle fails and recalls float to the top.
  const rows = useMemo(() => [...trades].sort((a, b) => Number(isException(b)) - Number(isException(a))), [trades]);
  const [selId, setSelId] = useState<string | null>(null);
  const cur = rows.find((t) => t.id === selId) ?? rows[0];
  useEffect(() => { if (selId && !rows.some((t) => t.id === selId)) setSelId(null); }, [rows, selId]);

  const run = async (t: Trade, c: "book" | "amend" | "settle" | "return" | "recall" | "rerate" | "mark", body?: object) => {
    try {
      const r = await api.post<Trade & { mark?: { callAmount?: number; price?: number } }>(`/trades/${t.id}/${c}`, body);
      const msg = c === "book" || c === "amend"
        ? `${c === "book" ? "booked" : "amended"}: ${r.compareStatus.toLowerCase()}${r.diffs.length ? ` (${r.diffs.map((d) => d.field).join(", ")})` : ""}`
        : c === "mark" ? `marked at ${r.mark?.price}: call ${money(r.mark?.callAmount ?? 0)} ${t.ccy}`
        : c === "rerate" ? (r.pendingRerateBps !== null ? `re-rate to ${bps(r.pendingRerateBps)} asked of the counterparty` : `re-rated to ${bps(r.feeBps)}`)
        : c === "settle" ? `settlement ${(r.settleStatus ?? "").toLowerCase()}`
        : `${c}ed: ${qty(r.openQty)} still open`;
      toast(r.compareStatus === "UNMATCHED" ? "warn" : "ok", `${t.composite} ${msg}`);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const close = (t: Trade) => {
    const verb = t.side === "BORROW" ? "return" : "recall";
    setAsk({ id: t.id, title: `${verb === "return" ? "Return" : "Recall"} ${t.composite}`, label: "Shares", value: String(t.openQty),
      hint: () => `${qty(t.openQty)} open`,
      send: (v) => run(t, verb, { quantity: parseInt(v.replace(/[,_]/g, ""), 10) }) });
  };
  const rerate = (t: Trade) => setAsk({
    id: t.id, title: `Re-rate ${t.composite}`, label: `${bps(t.feeBps)} →`, value: String(t.feeBps),
    hint: (v) => {
      const d = (parseFloat(v) - t.feeBps) / 1e4 * t.notional / 360;
      return Number.isFinite(d) && d ? `${d > 0 ? "+" : "−"}$${Math.abs(Math.round(d)).toLocaleString()} / day` : "";
    },
    send: (v) => run(t, "rerate", { feeBps: parseFloat(v) }),
  });
  const live = (t?: Trade) => !!t && !!t.contractId && t.loanStatus === "OPEN";

  useKeys((e) => {
    if (!e.shiftKey || !live(cur) || ask) return;  // shift: the fills panel (plain keys belong to the orders above)
    if (e.key === "R") rerate(cur);
    else if (e.key === "C") close(cur);
    else if (e.key === "M") run(cur, "mark");
    else return;
    e.preventDefault();
  }, [cur, ask]);

  const runMarks = async () => {
    try {
      const r = await api.post<{ marked: number; marginCalls: number; ours: number }>("/marks/run");
      toast("ok", `marked ${r.marked} contracts: ${r.marginCalls} margin calls (${r.ours} on our fills)`);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };

  return (
    <div className="flex flex-wrap gap-4 items-start">
      <Panel className="flex-[999_1_900px] min-w-0" bodyClass="overflow-auto max-h-[56vh]"
        title={<>Fills and loans <span className="label">{trades.length}</span></>}
        right={<>
          <span className="kbd mr-2">⇧R re-rate · ⇧C return / recall · ⇧M mark</span>
          <button className="btn btn-sm" onClick={runMarks}>Run marks</button>
        </>}>
        {!trades.length ? <Empty>Fills from both venues land here, book to the processor and settle at the custodian.</Empty> : (
          <table className="w-full">
            <thead><tr>{["executed", "side", "name", "venue", "quantity", "open", "fee", "notional", "counterparty", "lifecycle", "status", ""].map((h) =>
              <th key={h} className={`th ${["quantity", "open", "fee", "notional"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
            <tbody>
              {rows.map((t) => [
                <tr key={t.id} onClick={() => setSelId(t.id)} className={`row ${t.id === cur?.id ? "row-sel" : ""}`}>
                  <td className="td text-faint">{time(t.executedAt)}</td>
                  <td className="td text-dim">{t.side === "BORROW" ? "Borrow" : "Lend"}</td>
                  <td className="td font-semibold">{t.composite}</td>
                  <td className="td text-dim">{t.venue.replace("VENUE_", "")}</td>
                  <td className="td text-right">{full(t.quantity)}</td>
                  <td className="td text-right text-dim">{t.openQty !== t.quantity ? qty(t.openQty) : ""}</td>
                  <td className="td text-right">{bps(t.feeBps)}{t.pendingRerateBps !== null && <span className="text-hot"> → {bps(t.pendingRerateBps)}?</span>}</td>
                  <td className="td text-right text-dim">{t.ccy === "USD" ? `$${money(t.notional)}` : `${money(t.notional)} ${t.ccy}`}</td>
                  <td className="td text-dim">{t.counterparty}</td>
                  <td className="td"><Pipeline {...tradeStage(t)} /></td>
                  <td className="td max-w-[240px] truncate"><Note t={t} today={today} /></td>
                  <td className="td text-right whitespace-nowrap">
                    {(t.compareStatus === "UNBOOKED" || t.compareStatus === "FAILED") && <button className="btn-accent btn-sm" onClick={() => run(t, "book")}>Book</button>}
                    {t.compareStatus === "UNMATCHED" && <button className="btn btn-sm text-down" onClick={() => run(t, "amend")}>Fix break</button>}
                    {t.compareStatus === "MATCHED" && (!t.settleRef) && <button className="btn btn-sm" onClick={() => run(t, "settle")}>Settle</button>}
                    {live(t) && <>
                      <button className="btn-ghost btn-sm" onClick={() => close(t)}>{t.side === "BORROW" ? "Return" : "Recall"}</button>
                      <button className="btn-ghost btn-sm" onClick={() => rerate(t)}>Re-rate</button>
                    </>}
                  </td>
                </tr>,
                ask?.id === t.id && <AskRow key={t.id + "-ask"} ask={ask} cols={12} onClose={() => setAsk(null)} />,
              ])}
            </tbody>
          </table>
        )}
      </Panel>
      {cur && <Story t={cur} onFix={() => run(cur, "amend")} />}
    </div>
  );
}

const isException = (t: Trade) => t.compareStatus === "UNMATCHED" || t.compareStatus === "FAILED"
  || t.settleStatus === "FAILING" || t.settleStatus === "FAILED" || !!t.recallStatus || !!t.boughtIn;

/** The one status that matters most for the row, worst first. */
function Note({ t, today }: { t: Trade; today: string | undefined }) {
  if (t.compareStatus === "UNMATCHED")
    return <span className="text-down"><span className="dot bg-down mr-1.5" />Break · {t.diffs.map((d) => `${d.field} ${String(d.ours)} vs ${String(d.processor)}`).join(", ")}</span>;
  if (t.boughtIn) return <span className="text-down" title="The lender bought these in on a recall we didn't return in time: off the loan, at our cost (see Short list)">
    <span className="dot bg-down mr-1.5" />{qty(t.boughtIn)} bought in</span>;
  if (t.recallStatus) return <span><State s="RECALLED" /><RecallTag t={t} today={today} /></span>;
  if (t.settleStatus === "FAILING" || t.settleStatus === "FAILED") return <State s="FAILING" />;
  if (t.compareStatus !== "MATCHED") return <State s={t.compareStatus} />;
  if (t.loanStatus === "CLOSED") return <State s="CLOSED" />;
  return <State s={t.settleStatus === "SETTLED" ? "SETTLED" : "MATCHED"} />;
}

/** The trade's story: every step from the venue to the custodian, who did it, and the agent's fix for a break. */
function Story({ t, onFix }: { t: Trade; onFix: () => void }) {
  return (
    <aside aria-label="Trade story" className="panel flex-[1_1_300px] max-w-[360px] min-w-0">
      <div className="flex items-center h-10 px-4 border-b border-line gap-2">
        <span className="text-xs font-medium flex-1 truncate">{t.composite} · {t.contractId ?? t.venueTradeId}</span>
        <State s={t.loanStatus} />
      </div>
      <div className="grid grid-cols-3 border-b border-line">
        {[["Quantity", full(t.quantity)], ["Fee", bps(t.feeBps)], ["Notional", `${money(t.notional)}`]].map(([k, v], i) => (
          <div key={k} className={`flex flex-col gap-1.5 px-4 py-3 ${i ? "border-l border-line" : ""}`}>
            <span className="label">{k}</span><span className="text-[16px] font-medium">{v}</span>
          </div>
        ))}
      </div>
      <div className="grid grid-cols-[44px_1fr_auto] gap-x-2.5 px-4 py-2 border-b border-rule">
        <span className="label">{time(t.executedAt)}</span>
        <span className="flex flex-col"><span>Filled</span><span className="text-2xs text-dim">{full(t.quantity)} at {bps(t.feeBps)} with {t.counterparty}</span></span>
        <span className="label">{t.venue.replace("VENUE_", "Venue ")}</span>
      </div>
      {t.lifecycle.map((e, i) => (
        <div key={i} className="grid grid-cols-[44px_1fr_auto] gap-x-2.5 px-4 py-2 border-b border-rule">
          <span className="label">{time(e.ts)}</span>
          <span className="flex flex-col min-w-0">
            <span className={e.error ? "text-down" : ""}>{e.event[0] + e.event.slice(1).toLowerCase().replace(/_/g, " ")}</span>
            <span className="text-2xs text-dim truncate">
              {[e.quantity != null && full(e.quantity), e.fromBps != null && `${bps(e.fromBps)} → ${bps(e.toBps)}`,
                e.feeBps != null && `${bps(e.feeBps)} bps`, e.callAmount != null && `call ${money(e.callAmount)}`, e.error]
                .filter(Boolean).join(" · ")}
            </span>
          </span>
          <span className="label">{actor(e.by)}</span>
        </div>
      ))}
      {!!t.diffs.length && (
        <div className="px-4 py-3.5 flex flex-col gap-2.5">
          <span className="label">Processor says</span>
          {t.diffs.map((d) => (
            <div key={d.field} className="flex items-baseline gap-2.5">
              <span className="flex-1 text-dim">{d.field}</span>
              <span className="text-faint line-through">{String(d.ours)}</span><span className="text-faint">→</span>
              <span className="text-[16px] font-medium">{String(d.processor)}</span>
            </div>
          ))}
          <div><button className="btn-accent btn-sm" onClick={onFix}>Fix break</button></div>
        </div>
      )}
    </aside>
  );
}
