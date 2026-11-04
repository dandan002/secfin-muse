import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { actor, bps, full, parseQty, pct, qty, time } from "../lib/format";
import { useKeys, useMulti, useQuery, useSelection } from "../lib/hooks";
import { type BulkLine, useDesk } from "../lib/store";
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
  const { data: open } = useQuery<Locate[]>("/locates?status=RECEIVED&limit=5000", ["locate"]);
  const { data: done } = useQuery<Locate[]>("/locates?status=APPROVED,PARTIAL,REJECTED,EXPIRED,RELEASED,USED,INVALID", ["locate"]);
  const waiting = open ?? [];
  const decided = done ?? [];
  const received = waiting.length + decided.length;
  const auto = decided.filter((l) => (l.decidedBy ?? "").startsWith("policy")).length;
  const agent = waiting.filter((l) => l.route === "REVIEW").length;
  const you = waiting.filter((l) => l.route !== "REVIEW").length;
  const handled = received ? decided.length / received : 0;
  const withAgent = received ? agent / received : 0;
  return (
    <div className="shrink-0">
      <Figures items={[
        { label: "Received", value: received },
        { label: "Auto-cleared", value: auto },
        { label: "Decided by hand or agent", value: decided.length - auto },
        { label: "With agent", value: agent },
        { label: "For you", value: you, tone: you ? "text-hot" : "" },
      ]} />
      <span role="img" aria-label={`${decided.length} of ${received} handled`} className="flex h-0.5 -mt-0.5 mx-px bg-line rounded-b overflow-hidden">
        <span className="block bg-ink" style={{ width: `${handled * 100}%` }} />
        <span className="block bg-accent" style={{ width: `${withAgent * 100}%` }} />
      </span>
    </div>
  );
}

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
  const { data, error } = useQuery<Locate[]>("/locates?status=RECEIVED&limit=5000", ["locate", "market"]);
  const toast = useDesk((s) => s.toast);
  const openTicket = useDesk((s) => s.openTicket);
  // Group by requester, oldest request first within each.
  const rows = useMemo(() => {
    const list = [...(data ?? [])].sort((a, b) =>
      a.counterparty.localeCompare(b.counterparty) || a.receivedAt.localeCompare(b.receivedAt) || a.lineNo - b.lineNo);
    return list;
  }, [data]);
  // A burst can leave thousands waiting: draw them a page at a time so live updates stay quick.
  const [limit, setLimit] = useState(400);
  const shown = useMemo(() => rows.slice(0, limit), [rows, limit]);
  const [sel, setSel] = useSelection(shown.length);
  const cur = shown[sel];
  const multi = useMulti(shown.map((r) => r.id), cur?.id);
  const picked = shown.filter((r) => multi.picked.has(r.id));
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

  let lastCp = "";
  return (
    <div className="flex-1 min-h-[420px] grid grid-cols-[minmax(0,1fr)_340px] gap-4">
      <Panel
        title={<>Waiting <span className="label">{rows.length}</span></>}
        right={picked.length ? <BulkBar /> : <span className="kbd">Space tick · A approve · P partial · X reject · ⇧A all ETB · B borrow</span>}
        bodyClass="overflow-auto"
      >
        {error && <Empty>{error}</Empty>}
        {!error && !rows.length && <Empty>Inbox clear. Locates arrive from the gateway, chat, paste or the form above; ETB names under the cap clear themselves.</Empty>}
        {!!rows.length && (
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
              {shown.map((l, i) => {
                const head = l.counterparty !== lastCp;
                lastCp = l.counterparty;
                return [
                  head && (
                    <tr key={l.counterparty + "-h"}>
                      <td colSpan={13} className="td pt-4 pb-1.5 text-xs font-medium text-dim">
                        <button className="hover:text-ink" title="Tick this client's lines"
                          onClick={() => shown.forEach((r) => r.counterparty === l.counterparty && !multi.picked.has(r.id) && multi.toggle(r.id))}>
                          {l.counterparty}</button></td>
                    </tr>
                  ),
                  <tr key={l.id} onClick={() => setSel(i)}
                    className={`row cursor-default ${i === sel ? "row-sel" : multi.picked.has(l.id) ? "bg-raised" : ""}`}>
                    <td className="td w-8" onClick={(e) => { e.stopPropagation(); multi.toggle(l.id, e.shiftKey); }}>
                      <input type="checkbox" readOnly checked={multi.picked.has(l.id)} aria-label={`Tick ${l.instrument?.composite ?? l.identifier}`} />
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
                  </tr>,
                ];
              })}
            </tbody>
          </table>
        )}
        {rows.length > shown.length && (
          <div className="flex items-center gap-3 px-4 py-3 border-t border-line">
            <span className="text-dim flex-1">Showing {shown.length} of {rows.length}, oldest first per client.</span>
            <button className="btn btn-sm" onClick={() => setLimit((n) => n + 400)}>Show 400 more</button>
          </div>
        )}
      </Panel>
      <LocateDetail l={cur} onDecide={decide} />
    </div>
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
