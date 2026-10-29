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
      <div className="grid grid-cols-5 gap-3">
        {[["open orders", c?.openOrders], ["fills", c?.fills], ["unbooked", c?.unbooked], ["breaks", c?.breaks],
          ["open locates", c?.openLocates]].map(([k, v]) => (
          <div key={k as string} className="panel px-3 py-2">
            <div className="label">{k}</div>
            <div className={`text-lg ${k === "breaks" && (v as number) > 0 ? "text-hot" : ""}`}>{v ?? "—"}</div>
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

function Fills({ trades }: { trades: Trade[] }) {
  const toast = useDesk((s) => s.toast);
  const run = async (t: Trade, c: "book" | "amend") => {
    try {
      const r = await api.post<Trade>(`/trades/${t.id}/${c}`);
      toast(r.compareStatus === "MATCHED" ? "ok" : r.compareStatus === "UNMATCHED" ? "warn" : "info",
        `${t.composite} ${c === "book" ? "booked" : "amended"}: ${r.compareStatus.toLowerCase()}${r.diffs.length ? ` (${r.diffs.map((d) => d.field).join(", ")})` : ""}`);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  return (
    <Panel className="flex-[2]" bodyClass="overflow-auto" title={<>fills & bookings <span className="text-dim normal-case tracking-normal">{trades.length}</span></>}>
      {!trades.length ? <Empty>Fills from the venues land here, ready to book to the processor.</Empty> : (
        <table className="w-full text-xs">
          <thead><tr>{["executed", "side", "name", "qty", "fee", "notional", "cp", "uti", "contract", "compare", ""].map((h) =>
            <th key={h} className={`th ${["qty", "fee", "notional"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.id} className="row">
                <td className="td text-faint">{time(t.executedAt)}</td>
                <td className="td text-dim">{t.side.toLowerCase()}</td>
                <td className="td">{t.composite}</td>
                <td className="td text-right">{qty(t.quantity)}</td>
                <td className="td text-right">{bps(t.feeBps)}</td>
                <td className="td text-right text-dim">{money(t.notional)} {t.ccy}</td>
                <td className="td text-dim">{t.counterparty}</td>
                <td className="td text-faint max-w-[120px] truncate" title={t.uti ?? ""}>{t.uti?.slice(-12)}</td>
                <td className="td text-faint">{t.contractId ?? ""}</td>
                <td className="td">
                  <State s={t.compareStatus} />
                  {!!t.diffs.length && <span className="ml-2 text-faint">{t.diffs.map((d) => `${d.field} ${String(d.ours)}≠${String(d.processor)}`).join(" · ")}</span>}
                </td>
                <td className="td text-right">
                  {(t.compareStatus === "UNBOOKED" || t.compareStatus === "FAILED") && <button className="btn-accent h-6" onClick={() => run(t, "book")}>book</button>}
                  {t.compareStatus === "UNMATCHED" && <button className="btn h-6 text-warm" onClick={() => run(t, "amend")}>fix break</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
