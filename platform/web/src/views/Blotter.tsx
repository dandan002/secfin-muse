import { useMemo, useState } from "react";
import { api } from "../lib/api";
import { actor, bps, money, qty, time } from "../lib/format";
import { useKeys, useQuery, useSelection } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Order, Trade } from "../lib/types";
import { Empty, Kbd, Panel, State, Tabs } from "../components/ui";

interface Blotter {
  orders: Order[];
  trades: Trade[];
  counts: Record<string, number>;
}

const OPEN = ["NEW", "SENT", "PENDING", "COUNTERED"];

export function BlotterView() {
  const { data } = useQuery<Blotter>("/blotter", ["order", "trade", "break"]);
  const [filter, setFilter] = useState<"all" | "open" | "done">("all");
  const [cp, setCp] = useState("");
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
  const rerate = (o: Order) => {
    const v = prompt(`Counter ${o.composite} at fee (bps)`, String(o.counterFeeBps ?? o.feeBps));
    if (v) cmd(o, "counter", { feeBps: parseFloat(v) });
  };

  useKeys((e) => {
    if (!cur) return;
    if (e.key === "x") cmd(cur, "cancel");
    else if (e.key === "a" && cur.status === "COUNTERED") cmd(cur, "accept");
    else if (e.key === "r") rerate(cur);
    else return;
    e.preventDefault();
  }, [cur]);

  const c = data?.counts;
  return (
    <div className="h-full flex flex-col gap-3 min-h-0">
      <div className="grid grid-cols-6 gap-3">
        {[["open orders", c?.openOrders], ["open loans", c?.openLoans], ["unbooked", c?.unbooked], ["breaks", c?.breaks],
          ["settle fails", c?.unsettled], ["open locates", c?.openLocates]].map(([k, v]) => (
          <div key={k as string} className="panel px-3 py-2">
            <div className="label">{k}</div>
            <div className={`text-lg ${(k === "breaks" || k === "settle fails") && (v as number) > 0 ? "text-hot" : ""}`}>{v ?? "—"}</div>
          </div>
        ))}
      </div>

      <Panel className="flex-[3]" bodyClass="overflow-auto"
        title={<>orders <span className="text-dim normal-case tracking-normal">{orders.length}</span></>}
        right={<>
          <span className="text-2xs text-faint flex gap-3 mr-2"><span><Kbd>a</Kbd> accept counter</span><span><Kbd>r</Kbd> re-rate</span><span><Kbd>x</Kbd> cancel</span></span>
          <input value={cp} onChange={(e) => setCp(e.target.value)} placeholder="counterparty" className="input w-28 h-6" />
          <Tabs value={filter} onChange={setFilter} options={[{ id: "all", label: "all" }, { id: "open", label: "open" }, { id: "done", label: "done" }]} />
        </>}>
        {!orders.length ? <Empty>No orders yet. Send one from the Trade ticket (3) or ⌘K "borrow 100k AAPL @ 30".</Empty> : (
          <table className="w-full text-xs">
            <thead><tr>{["time", "side", "name", "type", "qty", "fee", "counter", "filled", "cp", "venue", "status", "by", "note"].map((h) =>
              <th key={h} className={`th ${["qty", "fee", "counter", "filled"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
            <tbody>
              {orders.map((o, i) => (
                <tr key={o.id} onClick={() => setSel(i)} className={`row ${i === sel ? "row-sel" : ""}`}>
                  <td className="td text-faint">{time(o.createdAt)}</td>
                  <td className={`td ${o.side === "BORROW" ? "text-info" : "text-accent"}`}>{o.side.toLowerCase()}</td>
                  <td className="td">{o.composite}</td>
                  <td className="td text-dim">{o.type.toLowerCase().replace("_", " ")}</td>
                  <td className="td text-right">{qty(o.quantity)}</td>
                  <td className="td text-right">{bps(o.feeBps)}</td>
                  <td className="td text-right text-warm">{o.counterFeeBps ? `${bps(o.counterFeeBps)}${o.counterQty ? ` × ${qty(o.counterQty)}` : ""}` : ""}</td>
                  <td className="td text-right text-dim">{o.filledQty ? qty(o.filledQty) : ""}</td>
                  <td className="td text-dim">{o.counterparty ?? "best"}</td>
                  <td className="td text-faint">{o.venue.replace("VENUE_", "")}</td>
                  <td className="td"><State s={o.status} /></td>
                  <td className="td text-faint">{actor(o.createdBy)}</td>
                  <td className="td text-faint max-w-[260px] truncate" title={o.rejectReason ?? o.routeReason}>{o.rejectReason ?? o.routeReason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Fills trades={data?.trades ?? []} />
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
    <span className={`ml-2 ${bad ? "text-hot" : "text-warm"}`}
      title={`${t.recallRef ? `ref ${t.recallRef}. ` : ""}The recall stands until the shares settle back with the lender; not back by the due date, the lender can buy them in at our cost.`}>
      {what}{t.recallDue ? `, due ${t.recallDue}` : ""}{overdue ? " · overdue" : ""}
    </span>
  );
}

function Fills({ trades }: { trades: Trade[] }) {
  const toast = useDesk((s) => s.toast);
  const today = useDesk((s) => s.clock?.nowLocal.slice(0, 10));  // the sim's New York date
  const [sel, setSel] = useState(0);
  const cur = trades[sel];
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
    const v = prompt(`${verb} ${t.composite}: shares (open ${qty(t.openQty)})`, String(t.openQty));
    if (v) run(t, verb, { quantity: parseInt(v.replace(/[,_]/g, ""), 10) });
  };
  const rerate = (t: Trade) => {
    const v = prompt(`Re-rate ${t.composite} (now ${bps(t.feeBps)} bps)`, String(t.feeBps));
    if (v) run(t, "rerate", { feeBps: parseFloat(v) });
  };
  const live = (t?: Trade) => !!t && !!t.contractId && t.loanStatus === "OPEN";

  useKeys((e) => {
    if (!e.shiftKey || !live(cur)) return;  // shift: the fills panel (plain keys belong to the orders above)
    if (e.key === "R") rerate(cur);
    else if (e.key === "C") close(cur);
    else if (e.key === "M") run(cur, "mark");
    else return;
    e.preventDefault();
  }, [cur]);

  const runMarks = async () => {
    try {
      const r = await api.post<{ marked: number; marginCalls: number; ours: number }>("/marks/run");
      toast("ok", `marked ${r.marked} contracts: ${r.marginCalls} margin calls (${r.ours} on our fills)`);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };

  return (
    <Panel className="flex-[2]" bodyClass="overflow-auto" title={<>fills & loans <span className="text-dim normal-case tracking-normal">{trades.length}</span></>}
      right={<>
        <span className="text-2xs text-faint flex gap-3 mr-2"><span><Kbd>⇧C</Kbd> return / recall</span><span><Kbd>⇧R</Kbd> re-rate</span><span><Kbd>⇧M</Kbd> mark</span></span>
        <button className="btn h-6" onClick={runMarks}>run marks</button>
      </>}>
      {!trades.length ? <Empty>Fills from both venues land here, book to the processor and settle at the custodian.</Empty> : (
        <table className="w-full text-xs">
          <thead><tr>{["executed", "side", "name", "venue", "qty", "open", "fee", "notional", "cp", "contract", "compare", "settle", "loan", ""].map((h) =>
            <th key={h} className={`th ${["qty", "open", "fee", "notional"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
          <tbody>
            {trades.map((t, i) => {
              const last = t.lifecycle[t.lifecycle.length - 1];
              return (
                <tr key={t.id} onClick={() => setSel(i)} className={`row ${i === sel ? "row-sel" : ""}`}>
                  <td className="td text-faint">{time(t.executedAt)}</td>
                  <td className={`td ${t.side === "BORROW" ? "text-info" : "text-accent"}`}>{t.side.toLowerCase()}</td>
                  <td className="td">{t.composite}</td>
                  <td className="td text-faint">{t.venue.replace("VENUE_", "")}</td>
                  <td className="td text-right">{qty(t.quantity)}</td>
                  <td className="td text-right text-dim">{t.openQty !== t.quantity ? qty(t.openQty) : ""}</td>
                  <td className="td text-right">{bps(t.feeBps)}{t.pendingRerateBps !== null && <span className="text-warm"> → {bps(t.pendingRerateBps)}?</span>}</td>
                  <td className="td text-right text-dim">{money(t.notional)} {t.ccy}</td>
                  <td className="td text-dim">{t.counterparty}</td>
                  <td className="td text-faint" title={t.uti ? `UTI ${t.uti}` : ""}>{t.contractId ?? ""}</td>
                  <td className="td">
                    <State s={t.compareStatus} />
                    {!!t.diffs.length && <span className="ml-2 text-faint">{t.diffs.map((d) => `${d.field} ${String(d.ours)}≠${String(d.processor)}`).join(" · ")}</span>}
                  </td>
                  <td className="td" title={t.settleRef ?? ""}><State s={t.settleStatus} /></td>
                  <td className="td" title={last ? `${last.event.toLowerCase()} ${time(last.ts)} by ${actor(last.by)}${last.error ? `: ${last.error}` : ""}` : ""}>
                    <State s={t.loanStatus} />{last && <span className="ml-2 text-faint">{last.event.toLowerCase().replace("_", " ")}</span>}
                    {t.recallStatus && <RecallTag t={t} today={today} />}
                    {!!t.boughtIn && <span className="ml-2 text-down"
                      title="The lender bought these in on a recall we didn't return in time: off the loan, at our cost (see Shorts)">
                      {qty(t.boughtIn)} bought in</span>}
                  </td>
                  <td className="td text-right whitespace-nowrap space-x-1">
                    {(t.compareStatus === "UNBOOKED" || t.compareStatus === "FAILED") && <button className="btn-accent h-6" onClick={() => run(t, "book")}>book</button>}
                    {t.compareStatus === "UNMATCHED" && <button className="btn h-6 text-warm" onClick={() => run(t, "amend")}>fix break</button>}
                    {t.compareStatus === "MATCHED" && (!t.settleRef) && <button className="btn h-6" onClick={() => run(t, "settle")}>settle</button>}
                    {live(t) && <>
                      <button className="btn h-6" onClick={() => close(t)}>{t.side === "BORROW" ? "return" : "recall"}</button>
                      <button className="btn h-6" onClick={() => rerate(t)}>re-rate</button>
                    </>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
