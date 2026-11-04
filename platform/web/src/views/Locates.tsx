import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { actor, bps, full, parseQty, pct, qty, time } from "../lib/format";
import { useKeys, useMulti, useQuery, useSelection } from "../lib/hooks";
import { type BulkLine, useDesk } from "../lib/store";
import { onPush } from "../lib/stream";
import type { Counterparty, Locate } from "../lib/types";
import { Empty, Figures, Kbd, Panel, PreBorrowTag, Stat, State, Tabs, TierBadge } from "../components/ui";

type Tab = "inbox" | "actioned";

export function Locates() {
  const [tab, setTab] = useState<Tab>("inbox");
  return (
    <div className="h-full flex flex-col gap-4 min-h-0">
      <Burst />
      <div className="flex items-center gap-3">
        <Tabs value={tab} onChange={setTab} options={[{ id: "inbox", label: "Waiting" }, { id: "actioned", label: "Decided" }]} />
        <NewLocate />
      </div>
      {tab === "inbox" ? <Inbox /> : <Actioned />}
    </div>
  );
}

/** The morning burst at a glance: how many came in, how many the policy cleared, how many sit with the agent or the
 *  trader, and a bar of how much is handled. */
function Burst() {
  const { data: sm } = useQuery<{ received: number; waiting: number; withAgent: number; forYou: number; decided: number;
    autoCleared: number }>("/locates/summary", ["locate"]);
  const received = sm?.received ?? 0;
  const handled = received ? (sm?.decided ?? 0) / received : 0;
  const withAgent = received ? (sm?.withAgent ?? 0) / received : 0;
  return (
    <div className="shrink-0">
      <Figures items={[
        { label: "Received", value: sm ? received : "—" },
        { label: "Auto-cleared", value: sm?.autoCleared ?? "—" },
        { label: "Decided by hand or agent", value: sm ? sm.decided - sm.autoCleared : "—" },
        { label: "With agent", value: sm?.withAgent ?? "—" },
        { label: "For you", value: sm?.forYou ?? "—", tone: sm?.forYou ? "text-hot" : "" },
      ]} />
      <span role="img" aria-label={`${sm?.decided ?? 0} of ${received} handled`} className="flex h-0.5 -mt-0.5 mx-px bg-line rounded-b overflow-hidden">
        <span className="block bg-ink" style={{ width: `${handled * 100}%` }} />
        <span className="block bg-accent" style={{ width: `${withAgent * 100}%` }} />
      </span>
    </div>
  );
}

/** Every waiting line, kept live from the stream rather than refetched. One full read to start (and on reconnect,
 *  and every 30 s to pick up market moves in the enrichment); after that each locate push is folded in: a decided
 *  line drops out at once, a new or changed one is fetched by id, a few hundred milliseconds' worth at a time. */
function useWaiting() {
  const [byId, setById] = useState<Map<string, Locate> | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A full read can be overtaken by pushes while it's in flight. A line never goes back to waiting once it's
  // left, so lines seen leaving stay out of any read that lands later, and lines streamed in after a read began
  // stay in even if that read didn't have them.
  const gone = useRef(new Set<string>());
  const arrived = useRef(new Map<string, number>());
  const load = useCallback(async () => {
    const t0 = Date.now();
    try {
      const list = await api.get<Locate[]>("/locates?status=RECEIVED&limit=5000");
      setById((old) => {
        const n = new Map(list.filter((l) => !gone.current.has(l.id)).map((l) => [l.id, l]));
        old?.forEach((l, id) => {
          if (!n.has(id) && (arrived.current.get(id) ?? 0) >= t0) n.set(id, l);
        });
        return n;
      });
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    const off = onPush("open", load);
    return () => { clearInterval(t); off(); };
  }, [load]);
  useEffect(() => {
    let pending = new Set<string>();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const flush = async () => {
      const ids = [...pending];
      pending = new Set();
      timer = undefined;
      for (let k = 0; k < ids.length; k += 150) {
        const chunk = ids.slice(k, k + 150);
        try {
          const got = await api.get<Locate[]>(`/locates?limit=1000&ids=${chunk.join(",")}`);
          setById((m) => {
            if (!m) return m;
            const n = new Map(m);
            chunk.forEach((id) => n.delete(id));
            for (const l of got) {
              if (l.status !== "RECEIVED") gone.current.add(l.id);
              else if (!gone.current.has(l.id)) {
                n.set(l.id, l);
                arrived.current.set(l.id, Date.now());
              }
            }
            return n;
          });
        } catch { /* the 30 s read catches up */ }
      }
    };
    const off = onPush("locate", (d: { id?: string; data?: { status?: string } }) => {
      if (!d?.id) return;
      if (d.data?.status && d.data.status !== "RECEIVED") {
        pending.delete(d.id);
        gone.current.add(d.id);
        setById((m) => {
          if (!m?.has(d.id!)) return m;
          const n = new Map(m);
          n.delete(d.id!);
          return n;
        });
        return;
      }
      pending.add(d.id);
      timer ??= setTimeout(flush, 400);
    });
    return () => { off(); clearTimeout(timer); };
  }, []);
  const list = useMemo(() => (byId ? [...byId.values()] : null), [byId]);
  return { data: list, error };
}

type Item = { kind: "head"; cp: string } | { kind: "row"; l: Locate; i: number };
const HEAD = 32;  // the sticky column headers above the rows

function NewLocate() {
  const { data: cps } = useQuery<Counterparty[]>("/counterparties");
  const toast = useDesk((s) => s.toast);
  const [cp, setCp] = useState("KESTRELHF");
  const [id, setId] = useState("");
  const [q, setQ] = useState("");
  const submit = async () => {
    const n = parseQty(q);
    if (!id || !n) return toast("warn", "identifier and quantity, e.g. VOD LN 250k");
    try {
      const [l] = await api.post<Locate[]>("/locates", { counterparty: cp, lines: [{ identifier: id, quantity: n }] });
      toast(l.status === "INVALID" ? "err" : "ok", `${l.identifier}: ${l.status.toLowerCase()}${l.error ? ` (${l.error})` : ""}`);
      setId("");
      setQ("");
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  return (
    <form className="ml-auto flex items-center gap-1.5" onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <span className="label mr-1">New locate</span>
      <select value={cp} onChange={(e) => setCp(e.target.value)} className="input w-32">
        {cps?.filter((c) => c.kind === "HEDGE_FUND").map((c) => <option key={c.code}>{c.code}</option>)}
      </select>
      <input value={id} onChange={(e) => setId(e.target.value)} placeholder="VOD LN / ISIN" className="input w-36" />
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="250k" className="input w-20 text-right" />
      <button className="btn">Add</button>
    </form>
  );
}

function Inbox() {
  const { data, error } = useWaiting();
  const toast = useDesk((s) => s.toast);
  const openTicket = useDesk((s) => s.openTicket);
  // Group by requester, oldest request first within each.
  const rows = useMemo(() => {
    const list = [...(data ?? [])].sort((a, b) =>
      a.counterparty.localeCompare(b.counterparty) || a.receivedAt.localeCompare(b.receivedAt) || a.lineNo - b.lineNo);
    return list;
  }, [data]);
  const [sel, setSel] = useSelection(rows.length);
  const cur = rows[sel];
  const multi = useMulti(rows.map((r) => r.id), cur?.id);
  const picked = rows.filter((r) => multi.picked.has(r.id));

  // All lines are held, but only those in view (plus a margin) are drawn: thousands scroll like dozens.
  const items = useMemo(() => {
    const out: Item[] = [];
    rows.forEach((l, i) => {
      if (l.counterparty !== (out.length ? rows[i - 1]?.counterparty : undefined)) out.push({ kind: "head", cp: l.counterparty });
      out.push({ kind: "row", l, i });
    });
    return out;
  }, [rows]);
  const itemOf = useMemo(() => new Map(items.flatMap((it, k) => (it.kind === "row" ? [[it.i, k] as const] : []))), [items]);
  const scroller = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ top: 0, height: 800 });
  const [rowH, setRowH] = useState(36);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setView((v) => ({ ...v, height: el.clientHeight })));
    ro.observe(el);
    return () => ro.disconnect();
  }, [rows.length > 0]);
  useLayoutEffect(() => {  // rows are one height; read it off a drawn one so the spacers line up exactly
    const h = scroller.current?.querySelector("tr[data-row]")?.getBoundingClientRect().height;
    if (h && Math.abs(h - rowH) > 0.5) setRowH(h);
  });
  const first = Math.max(0, Math.floor((view.top - HEAD) / rowH) - 15);
  const last = Math.min(items.length, Math.ceil((view.top + view.height) / rowH) + 15);
  useEffect(() => {  // keep the line in focus on screen as J/K or the arrows move it
    const el = scroller.current;
    const k = itemOf.get(sel);
    if (!el || k == null) return;
    const y = HEAD + k * rowH;
    if (y - HEAD < el.scrollTop) el.scrollTop = y - HEAD;
    else if (y + rowH > el.scrollTop + el.clientHeight) el.scrollTop = y + rowH - el.clientHeight;
  }, [sel, itemOf, rowH]);
  const [confirm, setConfirm] = useState<"approve" | "reject" | null>(null);
  useEffect(() => { if (!picked.length) setConfirm(null); }, [picked.length]);

  // Each ticked line at its own suggested size (in full where we can cover it, partial where we can't) and its
  // own quoted fee. A line with nothing to give is left for the trader rather than approved at zero.
  const givable = picked.filter((l) => (l.suggestedQty ?? 0) > 0);
  const bulkDecide = async (decision: "APPROVED" | "REJECTED") => {
    const lines = decision === "APPROVED"
      ? givable.map((l) => ({ id: l.id, quantity: l.suggestedQty, feeBps: l.feeBps ?? undefined }))
      : picked.map((l) => ({ id: l.id }));
    setConfirm(null);
    if (!lines.length) return toast("info", "nothing to give on the ticked lines");
    try {
      const r = await api.post<{ decided: string[]; failed: { id: string; error: string }[] }>("/locates/decisions",
        { decision, lines });
      toast(r.failed.length ? "warn" : "ok", `${decision === "APPROVED" ? "approved" : "rejected"} ${r.decided.length}`
        + (r.failed.length ? `; ${r.failed.length} not: ${r.failed[0].error}${r.failed.length > 1 ? " …" : ""}` : ""));
      multi.clear();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  // What the ticked lines still need from the street: requested less what we hold, per name, in round lots.
  const borrowShortfall = () => {
    const need = new Map<string, BulkLine>();
    for (const l of picked) {
      const short = l.qtyRequested - (l.availableBy?.internal ?? 0);
      if (short <= 0 || !l.instrumentId) continue;
      const cur = need.get(l.instrumentId);
      need.set(l.instrumentId, { instrumentId: l.instrumentId, composite: l.instrument?.composite ?? l.identifier,
        quantity: (cur?.quantity ?? 0) + Math.ceil(short / 100) * 100,
        why: [cur?.why, `${l.counterparty} ${qty(l.qtyRequested)}`].filter(Boolean).join(" · ") });
    }
    if (!need.size) return toast("info", "the ticked lines are covered from our own books");
    useDesk.getState().openBulkBorrow([...need.values()]);
  };

  const decide = async (l: Locate, decision: "APPROVED" | "PARTIAL" | "REJECTED", quantity?: number, extra = {}) => {
    try {
      const r = await api.post<Locate>(`/locates/${l.id}/decision`, { decision, quantity, ...extra });
      toast(r.status === "REJECTED" ? "info" : "ok",
        `${l.instrument?.composite ?? l.identifier} ${r.status.toLowerCase()} ${qty(r.qtyApproved)} for ${l.counterparty}`);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const approveEtb = async () => {
    const ids = rows.filter((r) => r.instrument?.etb).map((r) => r.id);
    if (!ids.length) return toast("info", "no ETB names in view");
    const r = await api.post<{ decided: string[]; failed: unknown[] }>("/locates/decisions", { ids, decision: "APPROVED" });
    toast("ok", `approved ${r.decided.length} ETB lines${r.failed.length ? `, ${r.failed.length} skipped` : ""}`);
  };

  useKeys((e) => {
    if (!cur) return;
    if (e.key === "A" && e.shiftKey) (picked.length ? setConfirm("approve") : approveEtb());
    else if (e.key === "a") decide(cur, "APPROVED");
    else if (e.key === "p") decide(cur, "PARTIAL", cur.suggestedQty);
    else if (e.key === "J" || e.key === "x") decide(cur, "REJECTED");
    else if (e.key === "b") openTicket({ instrument: cur.instrument?.composite ?? cur.identifier, quantity: cur.qtyRequested });
    else return;
    e.preventDefault();
  }, [cur, rows, picked]);

  const full_ = givable.filter((l) => l.suggestedQty === l.qtyRequested).length;
  const BulkBar = () => confirm ? (
    <span className="flex items-center gap-2">
      <span>{confirm === "approve"
        ? !givable.length ? "None of the ticked lines has anything to give (limits or supply)."
        : `Approve ${givable.length}: ${full_} in full, ${givable.length - full_} partial${picked.length > givable.length ? `, ${picked.length - givable.length} with nothing to give stay` : ""}?`
        : `Reject ${picked.length}?`}</span>
      <button className="btn-accent btn-sm" disabled={confirm === "approve" && !givable.length}
        onClick={() => bulkDecide(confirm === "approve" ? "APPROVED" : "REJECTED")}>
        {confirm === "approve" ? "Approve" : "Reject"}</button>
      <button className="btn-ghost btn-sm" onClick={() => setConfirm(null)}>Back</button>
    </span>
  ) : (
    <span className="flex items-center gap-2">
      <span className="font-medium">{picked.length} ticked</span>
      <span className="label">{qty(picked.reduce((n, l) => n + l.qtyRequested, 0))} requested</span>
      <button className="btn-accent btn-sm" onClick={() => setConfirm("approve")}>Approve at suggested <Kbd>⇧A</Kbd></button>
      <button className="btn btn-sm" onClick={borrowShortfall}>Borrow shortfall…</button>
      <button className="btn-ghost btn-sm" onClick={() => setConfirm("reject")}>Reject</button>
      <button className="btn-ghost btn-sm" onClick={multi.clear}>Clear <Kbd>Esc</Kbd></button>
    </span>
  );

  return (
    <div className="flex-1 min-h-[420px] grid grid-cols-[minmax(0,1fr)_340px] gap-4">
      <Panel
        title={<>Waiting <span className="label">{rows.length}</span></>}
        right={picked.length ? <BulkBar /> : <span className="kbd">Space tick · A approve · P partial · X reject · ⇧A all ETB · B borrow</span>}
        bodyClass="flex flex-col"
      >
        {error && !data && <Empty>{error}</Empty>}
        {!data && !error && <Empty>Loading the waiting lines…</Empty>}
        {data && !rows.length && <Empty>Inbox clear. Locates arrive from the gateway, chat, paste or the form above; ETB names under the cap clear themselves.</Empty>}
        {!!rows.length && (
          <div ref={scroller} className="flex-1 min-h-0 overflow-auto"
            onScroll={(e) => { const top = e.currentTarget.scrollTop; setView((v) => (v.top === top ? v : { ...v, top })); }}>
            <table className="w-full text-xs">
              <thead>
                <tr>
                  <th className="th w-8"><input type="checkbox" aria-label="Tick all" checked={multi.all}
                    onChange={(e) => multi.setAll(e.target.checked)} /></th>
                  {["time", "name", "tier", "requested", "internal", "street", "cover", "suggest", "fee", "util.", "with", "why"].map((h) => (
                    <th key={h} className={`th ${["requested", "internal", "street", "suggest", "fee", "util."].includes(h) ? "text-right" : ""}`}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {first > 0 && <tr aria-hidden style={{ height: first * rowH }} />}
                {items.slice(first, last).map((it) => it.kind === "head" ? (
                  <tr key={it.cp + "-h"} data-row className="row hover:bg-transparent">
                    <td colSpan={13} className="td text-xs font-medium text-dim">
                      <button className="hover:text-ink" title="Tick this client's lines"
                        onClick={() => rows.forEach((r) => r.counterparty === it.cp && !multi.picked.has(r.id) && multi.toggle(r.id))}>
                        {it.cp}</button></td>
                  </tr>
                ) : (
                  <LocateRow key={it.l.id} l={it.l} on={it.i === sel} ticked={multi.picked.has(it.l.id)}
                    onFocus={() => setSel(it.i)} onTick={(range) => multi.toggle(it.l.id, range)} />
                ))}
                {last < items.length && <tr aria-hidden style={{ height: (items.length - last) * rowH }} />}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      <LocateDetail l={cur} onDecide={decide} />
    </div>
  );
}

function LocateRow({ l, on, ticked, onFocus, onTick }: {
  l: Locate;
  on: boolean;
  ticked: boolean;
  onFocus: () => void;
  onTick: (range: boolean) => void;
}) {
  return (
    <tr data-row onClick={onFocus} className={`row cursor-default ${on ? "row-sel" : ticked ? "bg-raised" : ""}`}>
      <td className="td w-8" onClick={(e) => { e.stopPropagation(); onTick(e.shiftKey); }}>
        <input type="checkbox" readOnly checked={ticked} aria-label={`Tick ${l.instrument?.composite ?? l.identifier}`} />
      </td>
      <td className="td text-faint">{time(l.receivedAt)}</td>
      <td className="td">
        <span className="text-ink">{l.instrument?.composite ?? l.identifier}</span>
        {l.preBorrow && <span className="ml-2"><PreBorrowTag /></span>}
        <span className="text-faint ml-2 hidden xl:inline">{l.instrument?.name.slice(0, 22)}</span>
      </td>
      <td className="td"><TierBadge tier={l.instrument?.tier ?? l.tier} /></td>
      <td className="td text-right">{qty(l.qtyRequested)}</td>
      <td className="td text-right text-dim">{qty(l.availableBy?.internal)}</td>
      <td className="td text-right text-dim">{qty(l.availableBy?.street)}</td>
      <td className="td"><Cover l={l} /></td>
      <td className="td text-right font-medium">{qty(l.suggestedQty)}</td>
      <td className={`td text-right ${l.feeLimitBps != null && (l.feeBps ?? 0) > l.feeLimitBps ? "text-hot" : ""}`}
        title={`market ${bps(l.instrument?.feeBps)}, floor ${bps(l.feeFloorBps)}${l.feeLimitBps != null ? `, client's limit ${bps(l.feeLimitBps)}` : ""}`}>
        {bps(l.feeBps)}</td>
      <td className="td text-right text-dim">{pct(l.instrument?.utilPct, 0)}</td>
      <td className={`td ${l.route === "REVIEW" ? "text-dim" : "text-hot"}`}>{l.route === "REVIEW" ? "◇ Agent" : l.route === "HOLD" ? "You · hold" : "You"}</td>
      <td className="td text-faint max-w-[220px] truncate" title={`${l.routeReasons.join(" · ")} · via ${l.channel.toLowerCase()}`}>{l.routeReasons.join(" · ")}</td>
    </tr>
  );
}

/** Requested vs. what we can find (ours plus the street): a bar that turns green when it covers in full. */
function Cover({ l }: { l: Locate }) {
  const avail = l.available ?? (l.availableBy ? l.availableBy.internal + l.availableBy.street : 0);
  const cov = l.qtyRequested ? Math.min(1, avail / l.qtyRequested) : 0;
  return (
    <span className="flex items-center gap-2" title={`${qty(avail)} available of ${qty(l.qtyRequested)} requested`}>
      <span className="relative block w-24 h-0.5 bg-line">
        <span className={`absolute inset-y-0 left-0 ${cov >= 1 ? "bg-up" : "bg-ink"}`} style={{ width: `${cov * 100}%` }} />
      </span>
      <span className="label w-8">{Math.round(cov * 100)}%</span>
    </span>
  );
}

function LocateDetail({ l, onDecide }: {
  l: Locate | undefined;
  onDecide: (l: Locate, d: "APPROVED" | "PARTIAL" | "REJECTED", q?: number, extra?: object) => Promise<void>;
}) {
  const [q, setQ] = useState("");
  const [fee, setFee] = useState("");
  const [why, setWhy] = useState("");
  const [override, setOverride] = useState(false);
  const [expiry, setExpiry] = useState("");
  const toast = useDesk((s) => s.toast);
  useEffect(() => {
    setQ(l?.suggestedQty ? String(l.suggestedQty) : "");
    setFee(l?.feeBps != null ? String(l.feeBps) : "");  // the desk's quote: a bit above market
    setWhy("");
    setOverride(false);
    setExpiry("");
  }, [l?.id, l?.suggestedQty, l?.feeBps]);
  if (!l) return <Panel title="Detail"><Empty>Select a line.</Empty></Panel>;
  const n = parseQty(q) ?? undefined;
  const feeN = fee ? parseFloat(fee) : undefined;
  const underFloor = feeN != null && l.feeFloorBps != null && feeN < l.feeFloorBps;
  const room = l.counterpartyRoom;
  const overLimit = feeN != null && l.feeLimitBps != null && feeN > l.feeLimitBps;
  // A locate is good until the close unless the trader sets a time (New York) on the next business day or so.
  const expiresAt = expiry ? nyToIso(expiry) : undefined;
  const extra = { feeBps: feeN, rationale: why || undefined, overrideFeeFloor: underFloor && override, expiresAt };
  const askAgent = async () => {
    try {
      await api.post(`/agent/locates/${l.id}/review`);
      toast("ok", "agent is reviewing: its card will appear in the Agent queue");
    } catch (e) {
      toast("warn", (e as Error).message);
    }
  };
  return (
    <Panel title="Detail" right={<span className="label">{l.id}</span>} bodyClass="p-4 flex flex-col gap-4 overflow-auto">
      <div>
        <div className="flex items-center gap-2">
          <span className="text-md font-semibold">{l.instrument?.composite ?? l.identifier}</span>
          <TierBadge tier={l.instrument?.tier} />
          {l.instrument?.etb && <span className="text-2xs text-dim">ETB</span>}
          {l.preBorrow && <PreBorrowTag />}
        </div>
        <div className="text-xs text-faint truncate">{l.instrument?.name} · {l.instrument?.isin}</div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <Stat label="requested" value={full(l.qtyRequested)} />
        <Stat label="available" value={qty(l.available)} tone={(l.available ?? 0) < l.qtyRequested ? "text-down" : ""} />
        <Stat label="from" value={l.counterparty} />
        <Stat label="internal" value={qty(l.availableBy?.internal)} tone="" />
        <Stat label="street" value={qty(l.availableBy?.street)} />
        <Stat label="market bps" value={bps(l.instrument?.feeBps)} />
        <Stat label="floor bps" value={bps(l.feeFloorBps)} />
        <Stat label="client limit" value={l.feeLimitBps != null ? bps(l.feeLimitBps) : "none"} tone={overLimit ? "text-warm" : ""} />
        <Stat label="cp room" value={room ? qty(room.maxQty) : "—"} tone={room && room.maxQty < l.qtyRequested ? "text-down" : ""} />
        <Stat label="util" value={pct(l.instrument?.utilPct)} />
        <Stat label="via" value={l.channel.toLowerCase()} />
      </div>
      <div className="border-t border-line pt-3">
        <div className="label mb-1.5">Why it's here</div>
        <ul className="text-xs text-dim space-y-1">
          {l.routeReasons.map((r) => <li key={r}>· {r}</li>)}
        </ul>
      </div>
      <div className="border-t border-line pt-3 grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1"><span className="label">Quantity</span>
          <input value={q} onChange={(e) => setQ(e.target.value)} className="input text-right" /></label>
        <label className="flex flex-col gap-1"><span className="label">Fee bps</span>
          <input value={fee} onChange={(e) => setFee(e.target.value)} className="input text-right" /></label>
        <label className="col-span-2 flex flex-col gap-1"><span className="label">Good until (New York)</span>
          <input type="datetime-local" value={expiry} onChange={(e) => setExpiry(e.target.value)} className="input"
            title="Leave empty: today's close (16:00), or the next business day's after it" /></label>
        {overLimit && (
          <div className="col-span-2 text-2xs text-warm">over the client's limit of {bps(l.feeLimitBps)}: it may go elsewhere</div>
        )}
        {underFloor && (
          <label className="col-span-2 flex items-center gap-2 text-2xs text-warm">
            <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
            under the desk's floor of {bps(l.feeFloorBps)}: override (recorded on the locate)
          </label>
        )}
        {!!room?.failing.length && (
          <ul className="col-span-2 text-2xs text-down space-y-0.5">{room.failing.map((f) => <li key={f}>· {f}</li>)}</ul>
        )}
        <label className="col-span-2 flex flex-col gap-1"><span className="label">Rationale</span>
          <input value={why} onChange={(e) => setWhy(e.target.value)} placeholder="optional" className="input" /></label>
      </div>
      <div className="flex flex-wrap gap-2">
        <button className="btn-accent" onClick={() => onDecide(l, "APPROVED", n, extra)}>Approve {qty(n ?? l.qtyRequested)} <Kbd>A</Kbd></button>
        <button className="btn-ghost" onClick={() => onDecide(l, "REJECTED", undefined, extra)}>Reject <Kbd>X</Kbd></button>
        <button className="btn ml-auto" onClick={askAgent} title="Ask the reviewer model (needs LOANDESK_AGENT_MODE)">Ask agent</button>
      </div>
    </Panel>
  );
}

function Actioned() {
  const { data } = useQuery<Locate[]>("/locates?status=APPROVED,PARTIAL,REJECTED,EXPIRED,RELEASED,USED,INVALID", ["locate"]);
  const toast = useDesk((s) => s.toast);
  const rows = data ?? [];
  const release = async (l: Locate) => {
    try {
      await api.post(`/locates/${l.id}/release`);
      toast("info", `released ${qty(l.qtyApproved)} ${l.instrument?.composite}`);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const exportCsv = () => {
    const head = "id,counterparty,instrument,requested,approved,feeBps,enteredAt,expiresAt,used,status,decidedBy,regShoBasis";
    const body = rows.map((l) => [l.id, l.counterparty, l.instrument?.isin ?? l.identifier, l.qtyRequested, l.qtyApproved,
      l.feeBps ?? "", l.enteredAt ?? "", l.expiresAt ?? "", l.qtyUsed, l.status, l.decidedBy,
      `"${l.regShoBasis ?? ""}"`].join(","));
    const url = URL.createObjectURL(new Blob([[head, ...body].join("\n")], { type: "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "locates-actioned.csv" });
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <Panel title={<>Today's decisions <span className="label">{rows.length}</span></>}
      right={<button className="btn btn-sm" onClick={exportCsv}>Export CSV</button>} className="flex-1" bodyClass="overflow-auto">
      {!rows.length ? <Empty>No decisions yet.</Empty> : (
        <table className="w-full text-xs">
          <thead><tr>{["entered", "cp", "name", "tier", "req", "approved", "fee", "expires", "used", "status", "by", "reply", "reg sho basis", ""].map((h) =>
            <th key={h} className={`th ${["req", "approved", "fee", "used"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((l) => (
              <tr key={l.id} className="row">
                <td className="td text-faint" title={`requested ${time(l.receivedAt)}`}>{time(l.enteredAt ?? l.decidedAt ?? l.receivedAt)}</td>
                <td className="td text-dim">{l.counterparty}</td>
                <td className="td">{l.instrument?.composite ?? l.identifier}</td>
                <td className="td"><TierBadge tier={l.tier} /></td>
                <td className="td text-right text-dim">{qty(l.qtyRequested)}</td>
                <td className="td text-right">{qty(l.qtyApproved)}</td>
                <td className="td text-right text-dim">{l.qtyApproved ? bps(l.feeBps) : "—"}</td>
                <td className="td text-faint">{l.qtyApproved ? time(l.expiresAt) : "—"}</td>
                <td className="td text-right">{l.status === "USED" || l.status === "EXPIRED" ? qty(l.qtyUsed) : "—"}</td>
                <td className="td"><State s={l.status} /></td>
                <td className="td text-dim">{actor(l.decidedBy)}</td>
                <td className="td text-faint">{l.replyStatus?.toLowerCase()}</td>
                <td className="td text-faint max-w-[280px] truncate" title={l.regShoBasis ?? l.error ?? ""}>{l.regShoBasis ?? l.error}</td>
                <td className="td text-right">
                  {(l.status === "APPROVED" || l.status === "PARTIAL") &&
                    <button className="btn btn-sm" onClick={() => release(l)}>Release</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

/** A datetime-local value read as New York time ("2026-10-28T12:00") to an ISO instant. */
function nyToIso(local: string): string {
  const guess = new Date(`${local}:00Z`);
  const ny = new Date(guess.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const utc = new Date(guess.toLocaleString("en-US", { timeZone: "UTC" }));
  return new Date(guess.getTime() + (utc.getTime() - ny.getTime())).toISOString();
}
