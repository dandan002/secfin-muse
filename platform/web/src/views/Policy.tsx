import { ReactNode, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { actor, full, money, parseQty, qty, time } from "../lib/format";
import { useQuery } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Autopilot, Counterparty, DeskPolicy, Limits, Priority } from "../lib/types";
import { Empty, Panel } from "../components/ui";

const words = (s: string) => s.replace(/_/g, " ");

/** What the desk lets the autopilot do, and the order counterparties are worked in. Every change is audited. */
export function Policy() {
  const { data, reload } = useQuery<DeskPolicy>("/policy", ["policy"]);
  const { data: ap } = useQuery<Autopilot>("/autopilot", ["autopilot"], 15000);
  const { data: cps } = useQuery<Counterparty[]>("/counterparties");
  if (!data) return <Empty>Loading the desk's policy…</Empty>;
  return (
    <div className="h-full overflow-auto flex flex-col gap-4 pb-4">
      <div className="flex flex-wrap gap-4 items-start shrink-0">
        {(["autopilot", "danger"] as const).map((m) => (
          <LimitsPanel key={m} mode={m} setting={data[m]} kinds={data.kinds} live={ap?.mode === m ? ap : null} onSaved={reload} />
        ))}
        <BreakerPanel setting={data.breaker} onSaved={reload} />
      </div>
      <PriorityPanel setting={data.priority} cps={cps ?? []} onSaved={reload} />
    </div>
  );
}

function Who({ s }: { s: { set: boolean; by?: string; at?: string } }) {
  return <span className="label">{s.set ? `Set by ${actor(s.by)} · ${time(s.at)}` : "Settings default"}</span>;
}

function useSave(key: string, onSaved: () => void) {
  const toast = useDesk((s) => s.toast);
  const [busy, setBusy] = useState(false);
  const run = async (body: unknown | null) => {
    setBusy(true);
    try {
      await (body === null ? api.del(`/policy/${key}`) : api.put(`/policy/${key}`, body));
      toast("ok", body === null ? `${key}: back to the default` : `${key} saved`);
      onSaved();
    } catch (e) {
      toast("err", `${key}: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return { busy, save: (b: unknown) => run(b), reset: () => run(null) };
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="grid grid-cols-[140px_1fr] items-center gap-3 px-4 py-2 border-b border-rule" title={hint}>
      <span className="text-dim">{label}</span>
      {children}
    </label>
  );
}

/** A dollar or share figure typed the desk's way ("5m", "250k", "1.5b"). */
function Amount({ value, onChange, prefix = "$" }: { value: number | null; onChange: (n: number | null) => void; prefix?: string }) {
  const [text, setText] = useState(value === null ? "" : qty(value));
  useEffect(() => setText(value === null ? "" : qty(value)), [value]);
  const bad = text.trim() !== "" && parseQty(text) === null;
  return (
    <span className="flex items-center gap-1.5">
      {prefix && <span className="text-faint">{prefix}</span>}
      <input className={`input w-32 ${bad ? "border-down" : ""}`} value={text}
        onChange={(e) => { setText(e.target.value); const n = parseQty(e.target.value); onChange(e.target.value.trim() === "" ? null : n); }} />
      {value !== null && value >= 1e4 && <span className="label">{full(value)}</span>}
    </span>
  );
}

function LimitsPanel({ mode, setting, kinds, live, onSaved }: {
  mode: "autopilot" | "danger";
  setting: DeskPolicy["autopilot"];
  kinds: string[];
  live: Autopilot | null;
  onSaved: () => void;
}) {
  const [d, setD] = useState<Limits>(setting.value);
  const src = JSON.stringify(setting.value);
  useEffect(() => setD(JSON.parse(src)), [src]);
  const { busy, save, reset } = useSave(mode, onSaved);
  const dirty = JSON.stringify(d) !== src;
  const ok = [d.maxCardUsd, d.dayUsd, d.dayCount].every((n) => Number.isFinite(n) && n >= 0);
  const put = (patch: Partial<Limits>) => setD({ ...d, ...patch });
  const toggle = (k: string) => {
    const have = d.kinds ?? kinds;
    put({ kinds: have.includes(k) ? have.filter((x) => x !== k) : [...have, k] });
  };
  const danger = mode === "danger";

  return (
    <Panel className={`flex-[1_1_380px] min-w-0 ${danger ? "border-hot/40" : ""}`}
      title={<>{danger ? <span className="text-hot">Danger</span> : "Autopilot"} limits {live && <span className="label">· on now</span>}</>}
      right={<Who s={setting} />}>
      {live?.today && (
        <div className="px-4 py-2.5 border-b border-line text-2xs text-dim">
          Today: {live.today.count} of {full(d.dayCount)} cards, ${qty(live.today.notionalUsd)} of ${qty(d.dayUsd)}
        </div>
      )}
      <Field label="Per card" hint="The most one card may put at stake: shares × price, or the cash on a margin call">
        <Amount value={d.maxCardUsd} onChange={(n) => put({ maxCardUsd: n ?? NaN })} />
      </Field>
      <Field label="Per day" hint="Notional the mode may approve in a New York day">
        <Amount value={d.dayUsd} onChange={(n) => put({ dayUsd: n ?? NaN })} />
      </Field>
      <Field label="Cards a day">
        <Amount prefix="" value={d.dayCount} onChange={(n) => put({ dayCount: n ?? NaN })} />
      </Field>
      {!danger && (
        <Field label="Min confidence" hint="The agent's own confidence on its card, 0 to 1">
          <input type="number" min={0} max={1} step={0.05} className="input w-24" value={d.minConfidence}
            onChange={(e) => put({ minConfidence: Math.min(1, Math.max(0, Number(e.target.value))) })} />
        </Field>
      )}
      {danger && (
        <Field label="Take counters within" hint="Accept a lender's counter when it's this close to our bid">
          <span className="flex items-center gap-1.5">
            <input type="number" min={0} max={100} step={1} className="input w-20" value={d.counterTolerancePct}
              onChange={(e) => put({ counterTolerancePct: Math.min(100, Math.max(0, Number(e.target.value))) })} />
            <span className="text-faint">% of our bid</span>
          </span>
        </Field>
      )}
      <div className="px-4 py-3 border-b border-rule flex flex-col gap-2">
        <span className="flex items-center gap-3">
          <span className="text-dim flex-1">Card kinds it may approve</span>
          <label className="flex items-center gap-1.5 text-2xs">
            <input type="checkbox" checked={d.kinds === null} onChange={() => put({ kinds: d.kinds === null ? [...kinds] : null })} />
            Every kind
          </label>
        </span>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1">
          {kinds.map((k) => (
            <label key={k} className={`flex items-center gap-2 ${d.kinds === null ? "text-faint" : ""}`}>
              <input type="checkbox" disabled={d.kinds === null} checked={d.kinds === null || d.kinds.includes(k)} onChange={() => toggle(k)} />
              <span className="first-letter:uppercase">{words(k)}</span>
            </label>
          ))}
        </div>
      </div>
      <div className="px-4 py-2.5 text-2xs text-faint">
        {danger ? "Hot names and cards with warnings may go too." : "Never Hot names, never a card with any flag."}{" "}
        A failed hard check, or a margin payment past the collateral budget, always waits for a trader.
      </div>
      <footer className="flex items-center gap-2 px-4 py-3 border-t border-line">
        <button className="btn-accent btn-sm" disabled={!dirty || !ok || busy} onClick={() => save(d)}>Save</button>
        {dirty && <button className="btn-ghost btn-sm" onClick={() => setD(JSON.parse(src))}>Discard</button>}
        <span className="flex-1" />
        {setting.set && <button className="btn-ghost btn-sm" disabled={busy} onClick={reset}
          title={`Per card $${qty(setting.default.maxCardUsd)}, per day $${qty(setting.default.dayUsd)}`}>Back to default</button>}
      </footer>
    </Panel>
  );
}

function BreakerPanel({ setting, onSaved }: { setting: DeskPolicy["breaker"]; onSaved: () => void }) {
  const [n, setN] = useState(setting.value.tripAfter);
  useEffect(() => setN(setting.value.tripAfter), [setting.value.tripAfter]);
  const { busy, save, reset } = useSave("breaker", onSaved);
  return (
    <Panel className="flex-[0_1_260px] min-w-0" title="Breaker" right={<Who s={setting} />}>
      <div className="px-4 py-3 flex flex-col gap-3">
        <span className="text-dim">Turn autopilot off after this many auto-approved cards fail in a row.</span>
        <span className="flex items-center gap-2">
          <input type="number" min={1} max={100} className="input w-20" value={n}
            onChange={(e) => setN(Math.max(1, Math.round(Number(e.target.value) || 1)))} />
          <span className="text-faint">failures</span>
        </span>
      </div>
      <footer className="flex items-center gap-2 px-4 py-3 border-t border-line">
        <button className="btn-accent btn-sm" disabled={n === setting.value.tripAfter || busy} onClick={() => save({ tripAfter: n })}>Save</button>
        <span className="flex-1" />
        {setting.set && <button className="btn-ghost btn-sm" disabled={busy} onClick={reset}>Back to default</button>}
      </footer>
    </Panel>
  );
}

const LISTS: { id: "returns" | "recalls" | "margin"; title: string; hint: string; after: string }[] = [
  { id: "returns", title: "Returns", hint: "When the box is over the shorts, give back borrows from these lenders first.",
    after: "Everyone else after, dearest fee first." },
  { id: "recalls", title: "Recalls", hint: "To cover a short with shares we lent, recall from these borrowers first.",
    after: "Everyone else after, cheapest loan first. A loan earning more than the borrow would cost is kept out." },
  { id: "margin", title: "Margin calls", hint: "After a mark, meet these counterparties' calls first.",
    after: "Everyone else after, largest first. Payments past the day's budget wait for a trader." },
];

function PriorityPanel({ setting, cps, onSaved }: { setting: DeskPolicy["priority"]; cps: Counterparty[]; onSaved: () => void }) {
  const [d, setD] = useState<Priority>(setting.value);
  const src = JSON.stringify(setting.value);
  useEffect(() => setD(JSON.parse(src)), [src]);
  const { busy, save, reset } = useSave("priority", onSaved);
  const dirty = JSON.stringify(d) !== src;
  const name = useMemo(() => Object.fromEntries(cps.map((c) => [c.code, c])), [cps]);

  const move = (k: keyof Omit<Priority, "marginBudgetUsd">, i: number, by: number) => {
    const xs = [...d[k]];
    const j = i + by;
    if (j < 0 || j >= xs.length) return;
    [xs[i], xs[j]] = [xs[j], xs[i]];
    setD({ ...d, [k]: xs });
  };

  return (
    <Panel className="shrink-0" title="Counterparty priority" right={<Who s={setting} />}>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))]">
        {LISTS.map((l, li) => (
          <div key={l.id} className={`flex flex-col min-w-0 ${li ? "border-l border-line" : ""}`}>
            <div className="px-4 pt-3 pb-2 flex flex-col gap-1">
              <span className="font-medium">{l.title}</span>
              <span className="text-2xs text-dim">{l.hint}</span>
            </div>
            <ol className="flex flex-col">
              {d[l.id].map((code, i) => (
                <li key={code} className="grid grid-cols-[22px_1fr_auto] items-center gap-2 px-4 h-8 border-t border-rule">
                  <span className="label">{i + 1}</span>
                  <span className="truncate" title={name[code]?.name}>{code} <span className="text-faint">{name[code]?.name}</span></span>
                  <span className="flex gap-0.5">
                    <button className="btn-ghost btn-sm px-1.5" aria-label={`Move ${code} up`} disabled={!i} onClick={() => move(l.id, i, -1)}>↑</button>
                    <button className="btn-ghost btn-sm px-1.5" aria-label={`Move ${code} down`} disabled={i === d[l.id].length - 1} onClick={() => move(l.id, i, 1)}>↓</button>
                    <button className="btn-ghost btn-sm px-1.5" aria-label={`Take ${code} off`} onClick={() => setD({ ...d, [l.id]: d[l.id].filter((x) => x !== code) })}>×</button>
                  </span>
                </li>
              ))}
            </ol>
            <div className="px-4 py-2 border-t border-rule">
              <select className="input w-full" value="" aria-label={`Add to ${l.title.toLowerCase()}`}
                onChange={(e) => e.target.value && setD({ ...d, [l.id]: [...d[l.id], e.target.value] })}>
                <option value="">Add a counterparty…</option>
                {cps.filter((c) => !d[l.id].includes(c.code)).map((c) => (
                  <option key={c.code} value={c.code}>{c.code} · {c.name}</option>
                ))}
              </select>
            </div>
            <span className="px-4 pb-3 text-2xs text-faint">{l.after}</span>
            {l.id === "margin" && (
              <div className="mt-auto px-4 py-3 border-t border-line flex flex-col gap-1.5">
                <span className="text-dim">Collateral budget a day</span>
                <Amount value={d.marginBudgetUsd} onChange={(n) => setD({ ...d, marginBudgetUsd: n })} />
                <span className="text-2xs text-faint">{d.marginBudgetUsd === null ? "Blank: no budget, every call drafted as usual."
                  : `Paid out in the order above until $${money(d.marginBudgetUsd)} is committed.`}</span>
              </div>
            )}
          </div>
        ))}
      </div>
      <footer className="flex items-center gap-2 px-4 py-3 border-t border-line">
        <button className="btn-accent btn-sm" disabled={!dirty || busy} onClick={() => save(d)}>Save priority</button>
        {dirty && <button className="btn-ghost btn-sm" onClick={() => setD(JSON.parse(src))}>Discard</button>}
        <span className="flex-1" />
        {setting.set && <button className="btn-ghost btn-sm" disabled={busy} onClick={reset}>Clear all</button>}
      </footer>
    </Panel>
  );
}
