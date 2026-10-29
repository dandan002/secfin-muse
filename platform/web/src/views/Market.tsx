import { useMemo } from "react";
import { qs } from "../lib/api";
import { bps, pct, px, qty } from "../lib/format";
import { useDebounced, useKeys, useQuery, useSelection } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { MarketRow } from "../lib/types";
import { Empty, Flash, Kbd, Panel, Sparkline, Tabs, TierBadge } from "../components/ui";

export function Market() {
  const filter = useDesk((s) => s.marketFilter);
  const setFilter = useDesk((s) => s.setMarketFilter);
  const watch = useDesk((s) => s.watchlist);
  const toggleWatch = useDesk((s) => s.toggleWatch);
  const openTicket = useDesk((s) => s.openTicket);
  const tier = filter.tier ?? "ALL";
  const q = useDebounced(filter.q ?? "", 200);
  const pinned = tier === "WATCH";
  const path = pinned
    ? (watch.length ? "/market" + qs({ ids: watch.join(","), limit: 500 }) : null)
    : "/market" + qs({ tier: tier === "ALL" ? undefined : tier, q, limit: 400 });
  const { data, error } = useQuery<MarketRow[]>(path, ["market", "locate"]);
  const rows = useMemo(() => (pinned && !watch.length ? [] : data ?? []), [data, pinned, watch]);
  const [sel, setSel] = useSelection(rows.length);
  const cur = rows[sel];

  useKeys((e) => {
    if (!cur) return;
    if (e.key === "Enter" || e.key === "b") openTicket({ instrument: cur.composite, side: "BORROW", feeBps: Math.round(cur.bestFeeBps ?? cur.feeBps) });
    else if (e.key === "l") openTicket({ instrument: cur.composite, side: "LEND" });
    else if (e.key === "w") toggleWatch(cur.instrumentId);
    else return;
    e.preventDefault();
  }, [cur]);

  return (
    <Panel className="h-full" bodyClass="overflow-auto"
      title={<>market <span className="text-dim normal-case tracking-normal">{rows.length} names</span></>}
      right={<>
        <span className="text-2xs text-faint flex gap-3 mr-2"><span><Kbd>↵</Kbd> bid</span><span><Kbd>l</Kbd> offer</span><span><Kbd>w</Kbd> pin</span></span>
        <input value={filter.q ?? ""} onChange={(e) => setFilter({ ...filter, q: e.target.value })} placeholder="filter" className="input w-32 h-6" />
        <Tabs value={tier} onChange={(t) => setFilter({ ...filter, tier: t === "ALL" ? undefined : t })}
          options={[{ id: "ALL", label: "all" }, { id: "GC", label: "gc" }, { id: "WARM", label: "warm" }, { id: "HOT", label: "hot" },
            { id: "WATCH", label: `★ ${watch.length}` }]} />
      </>}>
      {error && <Empty>{error}</Empty>}
      {!error && !rows.length && <Empty>{pinned ? "Pin names with w, or apply a paste as a watchlist." : "No availability yet. The market fills in once Venue A's feed syncs (mocks on :8030)."}</Empty>}
      {!!rows.length && (
        <table className="w-full text-xs">
          <thead><tr>{["", "name", "tier", "last px", "fee", "util", "venue A", "lenders", "best", "last print", "5d", "held", "available"].map((h) =>
            <th key={h} className={`th ${["last px", "fee", "util", "venue A", "lenders", "best", "last print", "held", "available"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.instrumentId} onClick={() => setSel(i)} onDoubleClick={() => openTicket({ instrument: r.composite })}
                className={`row ${i === sel ? "row-sel" : ""}`}>
                <td className="td w-6">
                  <button onClick={(e) => { e.stopPropagation(); toggleWatch(r.instrumentId); }}
                    className={watch.includes(r.instrumentId) ? "text-warm" : "text-faint/50 hover:text-dim"}>★</button>
                </td>
                <td className="td"><span className="text-ink">{r.composite}</span><span className="text-faint ml-2 hidden xl:inline">{r.name.slice(0, 26)}</span></td>
                <td className="td"><TierBadge tier={r.tier} /></td>
                <td className="td text-right text-dim">{px(r.lastPx, r.ccy)}</td>
                <td className="td text-right"><Flash value={r.feeBps}>{bps(r.feeBps)}</Flash></td>
                <td className="td text-right text-dim">{pct(r.utilPct, 0)}</td>
                <td className="td text-right"><Flash value={r.venueA?.qty}>{qty(r.venueA?.qty)}</Flash></td>
                <td className="td text-right text-faint">{r.venueA?.lenders ?? "—"}</td>
                <td className="td text-right"><Flash value={r.bestFeeBps}>{bps(r.bestFeeBps)}</Flash></td>
                <td className="td text-right text-dim">{bps(r.lastFeeBps)}</td>
                <td className="td"><Sparkline values={r.trend} /></td>
                <td className="td text-right text-faint">{r.held ? qty(r.held) : ""}</td>
                <td className="td text-right text-accent">{qty(r.available)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
