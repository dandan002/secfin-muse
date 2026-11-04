import { useEffect, useMemo, useState } from "react";
import { bestFor } from "../lib/allocate";
import { api } from "../lib/api";
import { bps, full, parseQty } from "../lib/format";
import { useDesk } from "../lib/store";
import type { LenderLadder, OrderBatchResult } from "../lib/types";
import { Kbd, State } from "./ui";

interface Line {
  instrumentId: string;
  composite: string;
  why?: string;
  qty: string;
  lender: string; // our code; "" = best available on the venue
  fee: string;
  auto: boolean; // lender and fee still as filled in from the ladder
}

/** Borrow several names on one confirm. Each line is filled in from Venue A's ladder (the cheapest lender who can
 *  fill it, at their indicative fee); the trader can change any of it. Sent as bids naming each lender. */
export function BulkBorrow() {
  const start = useDesk((s) => s.bulkBorrow);
  const close = () => useDesk.getState().openBulkBorrow(null);
  const toast = useDesk((s) => s.toast);
  const [ladders, setLadders] = useState<Record<string, LenderLadder>>({});
  const [lines, setLines] = useState<Line[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<OrderBatchResult | null>(null);
  const [outcome, setOutcome] = useState<Record<number, { status?: string; error?: string }>>({});

  useEffect(() => {
    if (!start) return;
    setResult(null);
    setOutcome({});
    setConfirming(false);
    setLines(start.map((l) => ({ instrumentId: l.instrumentId, composite: l.composite, why: l.why, qty: String(l.quantity),
      lender: "", fee: "", auto: true })));
    const ids = [...new Set(start.map((l) => l.instrumentId))];
    api.get<Record<string, LenderLadder>>(`/lenders?ids=${ids.slice(0, 50).join(",")}`)
      .then((r) => {
        setLadders(r);
        setLines((ls) => ls.map((l) => fill(l, r[l.instrumentId])));
      })
      .catch((e) => toast("warn", `lender ladder unavailable: ${(e as Error).message}`));
  }, [start]);

  const update = (i: number, patch: Partial<Line>) => setLines((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const ok = (l: Line) => (parseQty(l.qty) ?? 0) > 0 && l.fee !== "" && Number.isFinite(parseFloat(l.fee));
  const valid = lines.filter(ok);
  const total = useMemo(() => valid.reduce((n, l) => n + (parseQty(l.qty) ?? 0), 0), [valid]);

  const send = async () => {
    setBusy(true);
    try {
      const r = await api.post<OrderBatchResult>("/orders/batch", { orders: valid.map((l) => ({
        side: "BORROW", instrument: l.composite, quantity: parseQty(l.qty), feeBps: parseFloat(l.fee),
        counterparty: l.lender || null, venue: "VENUE_A", type: l.lender ? "BID" : "AUTO",
      })) });
      setResult(r);
      // Results come back in the order sent, the blocked ones by position: put each against its line.
      const sentIdx = lines.map((l, i) => (ok(l) ? i : -1)).filter((i) => i >= 0);
      const placed = [...r.placed];
      setOutcome(Object.fromEntries(sentIdx.map((lineIdx, k) => {
        const f = r.failed.find((x) => x.line === k);
        return [lineIdx, f ? { error: f.error } : { status: placed.shift()?.status }];
      })));
      toast(r.failed.length ? "warn" : "ok", `sent ${r.placed.length} order${r.placed.length === 1 ? "" : "s"}${r.failed.length ? `, ${r.failed.length} blocked` : ""}`);
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  useEffect(() => {
    if (!start) return;
    const fn = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      else if (e.key === "Enter" && confirming && !busy) { e.preventDefault(); send(); }
    };
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  });

  if (!start) return null;
  return (
    <div className="fixed inset-0 z-40 bg-black/30 flex justify-end" onClick={close}>
      <aside aria-label="Bulk borrow" className="h-full w-[760px] max-w-full bg-panel border-l border-line shadow-pop flex flex-col"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 h-[52px] px-6 border-b border-line shrink-0">
          <span className="text-md font-semibold flex-1">Borrow {lines.length} name{lines.length === 1 ? "" : "s"}</span>
          <span className="label">Venue A · bids naming each lender</span>
          <button className="btn-ghost btn-sm" onClick={close} aria-label="Close">✕</button>
        </div>
        <div className="flex-1 overflow-auto">
          <table className="w-full">
            <thead><tr>{["name", "quantity", "lender", "shows", "fee bps", ""].map((h) =>
              <th key={h} className={`th ${["quantity", "shows", "fee bps"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
            <tbody>
              {lines.map((l, i) => {
                const ladder = ladders[l.instrumentId];
                const quote = ladder?.lenders.find((x) => x.counterparty === l.lender);
                const out = outcome[i];
                return (
                  <tr key={i} className="row">
                    <td className="td">
                      <div className="font-semibold">{l.composite}</div>
                      {l.why && <div className="label">{l.why}</div>}
                    </td>
                    <td className="td text-right">
                      <input value={l.qty} onChange={(e) => update(i, { qty: e.target.value })} aria-label={`${l.composite} quantity`}
                        className="input w-24 text-right" />
                    </td>
                    <td className="td">
                      {!ladder ? <span className="text-faint">Loading…</span> : !ladder.ok
                        ? <span className="text-hot" title={ladder.error ?? ""}>Venue A didn't answer: best available</span>
                        : (
                          <select value={l.lender} aria-label={`${l.composite} lender`} className="input w-56"
                            onChange={(e) => {
                              const q = ladder.lenders.find((x) => x.counterparty === e.target.value);
                              update(i, { lender: e.target.value, fee: q ? String(q.feeBps) : l.fee, auto: false });
                            }}>
                            <option value="">Best available (venue picks)</option>
                            {ladder.lenders.map((x) => (
                              <option key={x.counterparty} value={x.counterparty}>
                                {x.counterparty} · {bps(x.feeBps)} · {full(x.quantity)}
                              </option>
                            ))}
                          </select>
                        )}
                    </td>
                    <td className={`td text-right ${quote && quote.quantity < (parseQty(l.qty) ?? 0) ? "text-hot" : "text-dim"}`}>
                      {quote ? full(quote.quantity) : "—"}
                    </td>
                    <td className="td text-right">
                      <span className="inline-flex items-center gap-2">
                        {l.auto && l.fee !== "" && <span className="label" title="Filled in from the lender's indicative fee">Auto</span>}
                        <input value={l.fee} onChange={(e) => update(i, { fee: e.target.value, auto: false })}
                          aria-label={`${l.composite} fee`} placeholder="bps" className="input w-20 text-right" />
                      </span>
                    </td>
                    <td className="td text-right">
                      {out?.status ? <State s={out.status} /> : out?.error ? <span className="text-down" title={out.error}>Blocked</span>
                        : <button className="btn-ghost btn-sm" onClick={() => setLines((ls) => ls.filter((_, j) => j !== i))}
                            aria-label={`Remove ${l.composite}`}>✕</button>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!!result?.failed.length && (
            <ul className="px-6 py-3 flex flex-col gap-1 text-down">
              {result.failed.map((f) => <li key={f.line}>{f.instrument}{f.counterparty ? ` · ${f.counterparty}` : ""}: {f.error}</li>)}
            </ul>
          )}
        </div>
        <div className="flex items-center gap-3 px-6 py-4 border-t border-line shrink-0">
          {result ? (
            <>
              <span className="flex-1">{result.placed.length} sent{result.failed.length ? `, ${result.failed.length} blocked by checks` : ""}. Fills land in the Blotter.</span>
              <button className="btn" onClick={() => { close(); useDesk.getState().setView("blotter"); }}>Open Blotter</button>
              <button className="btn-ghost" onClick={close}>Close</button>
            </>
          ) : confirming ? (
            <>
              <span className="flex-1">Send {valid.length} bid{valid.length === 1 ? "" : "s"} for {full(total)} shares on Venue A?
                {valid.length < lines.length && <span className="text-hot"> {lines.length - valid.length} line(s) without a size or fee stay here.</span>}</span>
              <button className="btn-accent" disabled={busy} onClick={send}>Send {valid.length} <Kbd>↵</Kbd></button>
              <button className="btn-ghost" onClick={() => setConfirming(false)}>Back</button>
            </>
          ) : (
            <>
              <span className="flex-1 text-dim">{full(total)} shares across {valid.length} line{valid.length === 1 ? "" : "s"}. Pre-trade checks run on each; any that fail are reported and the rest go.</span>
              <button className="btn-accent" disabled={!valid.length} onClick={() => setConfirming(true)}>Review and send</button>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

/** Fill a line from its ladder: the cheapest lender who can take the whole size, at their indicative fee. */
function fill(l: Line, ladder: LenderLadder | undefined): Line {
  if (!l.auto || !ladder?.ok || !ladder.lenders.length) return l;
  const best = bestFor(parseQty(l.qty) ?? 0, ladder.lenders);
  return best ? { ...l, lender: best.counterparty, fee: String(best.feeBps) } : l;
}
