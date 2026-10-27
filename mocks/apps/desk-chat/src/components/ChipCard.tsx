import { useEffect, useState } from "react";
import { api, type Chip, type Instrument } from "../lib/api";
import { fmtQty } from "../lib/time";

export type TicketEvent = { instrumentId: string; isin: string; composite: string; quantity: number | null };

const TIER: Record<string, string> = { GC: "bg-gc/20 text-gc", WARM: "bg-warm/20 text-warm", HOT: "bg-hot/20 text-hot" };

/** Instrument card for a clicked chip, with a prefilled-ticket action for the host app. */
export function ChipCard({ chip, anchor, onClose }: { chip: Chip; anchor: DOMRect; onClose: () => void }) {
  const [inst, setInst] = useState<Instrument | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  useEffect(() => {
    api.instrument(chip.isin).then(setInst, (e) => setErr(String(e.message ?? e)));
  }, [chip.isin]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const openTicket = () => {
    const detail: TicketEvent = {
      instrumentId: chip.instrumentId,
      isin: chip.isin,
      composite: chip.composite,
      quantity: chip.quantity,
    };
    window.dispatchEvent(new CustomEvent<TicketEvent>("deskchat:ticket", { detail }));
    setSent(true);
  };

  const top = Math.min(anchor.bottom + 6, window.innerHeight - 280);
  const left = Math.min(anchor.left, window.innerWidth - 330);

  return (
    <>
      <div className="fixed inset-0 z-30" onClick={onClose} />
      <div
        role="dialog"
        className="fixed z-40 w-80 rounded-lg border border-line bg-panel p-3 shadow-2xl shadow-black/40"
        style={{ top, left }}
      >
        {!inst && !err && <p className="text-muted">Loading {chip.composite}…</p>}
        {err && <p className="text-hot">{err}</p>}
        {inst && (
          <>
            <div className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <div className="font-mono text-[14px] font-medium">{inst.composite}</div>
                <div className="truncate text-muted">{inst.name}</div>
              </div>
              <span className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${TIER[inst.tier]}`}>{inst.tier}</span>
            </div>
            <dl className="num mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-[12px]">
              {(
                [
                  ["ISIN", inst.isin],
                  ["SEDOL", inst.sedol],
                  ["Last", `${inst.lastPx} ${inst.ccy}`],
                  ["Fee", `${inst.feeBps} bps`],
                  ["Util", `${inst.utilPct}%`],
                  ["SI", `${inst.si_pct}%`],
                  ["ADV 20d", fmtQty(inst.adv_20d)],
                  ["DTC", `${inst.days_to_cover}d`],
                ] as const
              ).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2">
                  <dt className="text-muted">{k}</dt>
                  <dd className="truncate">{v}</dd>
                </div>
              ))}
            </dl>
            {inst.threshold_flag && <p className="mt-2 text-[11px] text-hot">On threshold list</p>}
            <button
              onClick={openTicket}
              className="mt-3 w-full rounded-md bg-accent py-1.5 font-medium text-bg hover:bg-accent/90"
            >
              {sent ? "Ticket sent to LoanDesk ✓" : `Open ticket${chip.quantity ? ` · ${fmtQty(chip.quantity)}` : ""}`}
            </button>
          </>
        )}
      </div>
    </>
  );
}
