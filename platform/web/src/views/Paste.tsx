import { useState } from "react";
import { api } from "../lib/api";
import { bps, full } from "../lib/format";
import { useQuery } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Counterparty, PastePreview } from "../lib/types";
import { Empty, Panel, Tabs } from "../components/ui";

const SAMPLE = `Security\tPX_LAST\tEQY_SH_OUT\tVOLUME_AVG_20D\tQty
AAPL US Equity\t344.43\t#N/A Requesting Data...\t51200000\t250000
MSFT US Equity\t#N/A N/A\t7430000000\t21000000\t100000
NVDA US Equity\t217.28\t24300000000\t#N/A N/A\t500000`;

type Target = "locates" | "interest_list" | "watchlist";

/** Paste anything: sheet ranges, Bloomberg output, chat shorthand. Preview first; nothing is created until you apply. */
export function Paste() {
  const [text, setText] = useState("");
  const [prev, setPrev] = useState<PastePreview | null>(null);
  const [target, setTarget] = useState<Target>("locates");
  const [cp, setCp] = useState("PBALPHA");
  const [skip, setSkip] = useState<Set<number>>(new Set());
  const { data: cps } = useQuery<Counterparty[]>("/counterparties");
  const toast = useDesk((s) => s.toast);
  const addWatch = useDesk((s) => s.addWatch);
  const setView = useDesk((s) => s.setView);

  const parse = async (t = text) => {
    if (!t.trim()) return;
    try {
      setPrev(await api.post<PastePreview>("/ingest/paste", { text: t }));
      setSkip(new Set());
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const apply = async () => {
    if (!prev) return;
    const rows = prev.rows.filter((r) => r.instrument && !skip.has(r.rowNo)).map((r) => r.rowNo);
    try {
      const r = await api.post<{ target: string; created?: unknown[]; instrumentIds?: string[]; optimiser?: { matchesProposed: number } }>(
        `/ingest/paste/${prev.id}/apply`, { target, counterparty: cp, rows });
      if (r.instrumentIds) {
        addWatch(r.instrumentIds);
        toast("ok", `pinned ${r.instrumentIds.length} names`);
      } else if (r.created) {
        toast("ok", `created ${r.created.length} locates`);
        setView("locates");
      } else {
        toast("ok", `interest list sent to venue B · ${r.optimiser?.matchesProposed ?? 0} matches proposed`);
      }
      setPrev(null);
      setText("");
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };

  const shade = (c: number | undefined) =>
    c === undefined ? "" : c >= 0.95 ? "" : c >= 0.7 ? "bg-warm/[0.07]" : c > 0 ? "bg-hot/[0.09]" : "bg-down/[0.12]";

  return (
    <div className="h-full grid grid-rows-[auto_1fr] gap-3 min-h-0">
      <Panel title="paste anything" right={<>
        <button className="btn" onClick={() => { setText(SAMPLE); parse(SAMPLE); }}>sample sheet</button>
        <button className="btn-accent" onClick={() => parse()}>preview</button>
      </>} bodyClass="p-3">
        <textarea value={text} onChange={(e) => setText(e.target.value)}
          onPaste={(e) => { const t = e.clipboardData.getData("text"); setTimeout(() => parse(t), 0); }}
          rows={6} placeholder={"Paste a sheet range, Bloomberg output, a PB locate file, or chat lines like\n250k AAPL US, 75k MSFT @ 25bps"}
          className="input w-full h-auto py-2 leading-relaxed resize-y" />
      </Panel>

      <Panel bodyClass="overflow-auto"
        title={prev ? <>preview · {prev.kind.toLowerCase()} <span className="text-dim normal-case tracking-normal">{prev.resolved}/{prev.total} resolved</span></> : "preview"}
        right={prev && <>
          <Tabs value={target} onChange={setTarget} options={[{ id: "locates", label: "as locates" },
            { id: "interest_list", label: "as venue B list" }, { id: "watchlist", label: "as watchlist" }]} />
          {target === "locates" && (
            <select value={cp} onChange={(e) => setCp(e.target.value)} className="input h-6 w-32">
              {cps?.filter((c) => c.kind !== "AGENT_LENDER").map((c) => <option key={c.code}>{c.code}</option>)}
            </select>
          )}
          <button className="btn-accent" onClick={apply}>apply</button>
        </>}>
        {!prev ? <Empty>Deterministic parsers run first and shade each cell by confidence. Lines they can't read are listed for the parser model.</Empty> : (
          <>
            <table className="w-full text-xs">
              <thead><tr>{["", "#", "identifier", "resolved", "qty", "fee", "cp", "extra", "issue"].map((h) =>
                <th key={h} className={`th ${["qty", "fee"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
              <tbody>
                {prev.rows.map((r) => (
                  <tr key={r.rowNo} className={`row ${skip.has(r.rowNo) ? "opacity-40" : ""}`}>
                    <td className="td w-6"><input type="checkbox" disabled={!r.instrument} checked={!!r.instrument && !skip.has(r.rowNo)}
                      onChange={() => setSkip((s) => { const n = new Set(s); if (n.has(r.rowNo)) n.delete(r.rowNo); else n.add(r.rowNo); return n; })} /></td>
                    <td className="td text-faint">{r.rowNo}</td>
                    <td className={`td ${shade(r.confidence.identifier)}`}>{r.identifier}</td>
                    <td className="td">{r.instrument ? <><span>{r.instrument.composite}</span><span className="text-faint ml-2">{r.instrument.name.slice(0, 24)}</span></> : <span className="text-down">—</span>}</td>
                    <td className={`td text-right ${shade(r.confidence.quantity)}`}>{full(r.quantity)}</td>
                    <td className={`td text-right ${shade(r.confidence.feeBps)}`}>{bps(r.feeBps)}</td>
                    <td className="td text-dim">{r.counterparty}</td>
                    <td className="td text-faint max-w-[260px] truncate">{Object.entries(r.extra).map(([k, v]) => `${k}=${v}`).join(" ")}</td>
                    <td className="td text-down">{r.error}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!!prev.unparsed.length && (
              <div className="m-3 p-3 border border-dashed border-line rounded-soft text-xs">
                <div className="label mb-1.5">not understood · for the parser model</div>
                {prev.unparsed.map((u, i) => <div key={i} className="text-faint">{u}</div>)}
              </div>
            )}
          </>
        )}
      </Panel>
    </div>
  );
}
