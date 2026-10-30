import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { actor, bps, full, parseQty, pct, qty, time } from "../lib/format";
import { useKeys, useQuery, useSelection } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Counterparty, Locate } from "../lib/types";
import { Empty, Kbd, Panel, Stat, State, Tabs, TierBadge } from "../components/ui";

type Tab = "inbox" | "actioned";

export function Locates() {
  const [tab, setTab] = useState<Tab>("inbox");
  return (
    <div className="h-full flex flex-col gap-3 min-h-0">
      <div className="flex items-center gap-3">
        <Tabs value={tab} onChange={setTab} options={[{ id: "inbox", label: "Inbox" }, { id: "actioned", label: "Actioned" }]} />
        <NewLocate />
      </div>
      {tab === "inbox" ? <Inbox /> : <Actioned />}
    </div>
  );
}

function NewLocate() {
  const { data: cps } = useQuery<Counterparty[]>("/counterparties");
  const toast = useDesk((s) => s.toast);
  const [cp, setCp] = useState("PBALPHA");
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
      <span className="label mr-1">new locate</span>
      <select value={cp} onChange={(e) => setCp(e.target.value)} className="input w-32">
        {cps?.filter((c) => c.kind !== "AGENT_LENDER").map((c) => <option key={c.code}>{c.code}</option>)}
      </select>
      <input value={id} onChange={(e) => setId(e.target.value)} placeholder="VOD LN / ISIN" className="input w-36" />
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="250k" className="input w-20 text-right" />
      <button className="btn">add</button>
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
  const [sel, setSel] = useSelection(rows.length);
  const cur = rows[sel];

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
    if (e.key === "A" && e.shiftKey) approveEtb();
    else if (e.key === "a") decide(cur, "APPROVED");
    else if (e.key === "p") decide(cur, "PARTIAL", cur.suggestedQty);
    else if (e.key === "J" || e.key === "x") decide(cur, "REJECTED");
    else if (e.key === "b") openTicket({ instrument: cur.instrument?.composite ?? cur.identifier, quantity: cur.qtyRequested });
    else return;
    e.preventDefault();
  }, [cur, rows]);

  let lastCp = "";
  return (
    <div className="flex-1 min-h-0 grid grid-cols-[1fr_340px] gap-3">
      <Panel
        title={<>incoming <span className="text-dim normal-case tracking-normal">{rows.length}</span></>}
        right={<div className="flex items-center gap-3 text-2xs text-faint">
          <span><Kbd>a</Kbd> approve</span><span><Kbd>p</Kbd> partial</span><span><Kbd>x</Kbd> reject</span>
          <span><Kbd>⇧A</Kbd> all ETB</span><span><Kbd>b</Kbd> borrow</span>
        </div>}
        bodyClass="overflow-auto"
      >
        {error && <Empty>{error}</Empty>}
        {!error && !rows.length && <Empty>Inbox clear. Locates arrive from the gateway, chat, paste or the form above; ETB names under the cap clear themselves.</Empty>}
        {!!rows.length && (
          <table className="w-full text-xs">
            <thead>
              <tr>
                {["time", "name", "tier", "req", "avail", "suggest", "fee", "util", "route", "why", "via"].map((h) => (
                  <th key={h} className={`th ${["req", "avail", "suggest", "fee", "util"].includes(h) ? "text-right" : ""}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((l, i) => {
                const head = l.counterparty !== lastCp;
                lastCp = l.counterparty;
                return [
                  head && (
                    <tr key={l.counterparty + "-h"}>
                      <td colSpan={11} className="td pt-3 pb-1 label text-dim">{l.counterparty}</td>
                    </tr>
                  ),
                  <tr key={l.id} onClick={() => setSel(i)} className={`row cursor-default ${i === sel ? "row-sel" : ""}`}>
                    <td className="td text-faint">{time(l.receivedAt)}</td>
                    <td className="td">
                      <span className="text-ink">{l.instrument?.composite ?? l.identifier}</span>
                      <span className="text-faint ml-2 hidden xl:inline">{l.instrument?.name.slice(0, 22)}</span>
                    </td>
                    <td className="td"><TierBadge tier={l.instrument?.tier ?? l.tier} /></td>
                    <td className="td text-right">{qty(l.qtyRequested)}</td>
                    <td className={`td text-right ${(l.available ?? 0) < l.qtyRequested ? "text-down" : "text-dim"}`}>{qty(l.available)}</td>
                    <td className="td text-right text-accent">{qty(l.suggestedQty)}</td>
                    <td className={`td text-right ${l.feeLimitBps != null && (l.feeBps ?? 0) > l.feeLimitBps ? "text-warm" : "text-dim"}`}
                      title={`market ${bps(l.instrument?.feeBps)}, floor ${bps(l.feeFloorBps)}${l.feeLimitBps != null ? `, client's limit ${bps(l.feeLimitBps)}` : ""}`}>
                      {bps(l.feeBps)}</td>
                    <td className="td text-right text-dim">{pct(l.instrument?.utilPct, 0)}</td>
                    <td className="td"><State s={l.route} /></td>
                    <td className="td text-faint max-w-[220px] truncate">{l.routeReasons.join(" · ")}</td>
                    <td className="td text-faint">{l.channel.toLowerCase()}</td>
                  </tr>,
                ];
              })}
            </tbody>
          </table>
        )}
      </Panel>
      <LocateDetail l={cur} onDecide={decide} />
    </div>
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
  if (!l) return <Panel title="detail"><Empty>Select a line.</Empty></Panel>;
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
    <Panel title="detail" right={<span className="text-2xs text-faint">{l.id}</span>} bodyClass="p-3 flex flex-col gap-4 overflow-auto">
      <div>
        <div className="flex items-center gap-2">
          <span className="text-sm">{l.instrument?.composite ?? l.identifier}</span>
          <TierBadge tier={l.instrument?.tier} />
          {l.instrument?.etb && <span className="text-2xs text-gc border border-line rounded px-1">ETB</span>}
        </div>
        <div className="text-xs text-faint truncate">{l.instrument?.name} · {l.instrument?.isin}</div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <Stat label="requested" value={full(l.qtyRequested)} />
        <Stat label="available" value={qty(l.available)} tone={(l.available ?? 0) < l.qtyRequested ? "text-down" : ""} />
        <Stat label="from" value={l.counterparty} />
        <Stat label="internal" value={qty(l.availableBy?.internal)} tone={l.availableBy?.internal ? "text-accent" : ""} />
        <Stat label="street" value={qty(l.availableBy?.street)} />
        <Stat label="market bps" value={bps(l.instrument?.feeBps)} />
        <Stat label="floor bps" value={bps(l.feeFloorBps)} />
        <Stat label="client limit" value={l.feeLimitBps != null ? bps(l.feeLimitBps) : "none"} tone={overLimit ? "text-warm" : ""} />
        <Stat label="cp room" value={room ? qty(room.maxQty) : "—"} tone={room && room.maxQty < l.qtyRequested ? "text-down" : ""} />
        <Stat label="util" value={pct(l.instrument?.utilPct)} />
        <Stat label="via" value={l.channel.toLowerCase()} />
      </div>
      <div className="border-t rule pt-3">
        <div className="label mb-1.5">why it's here</div>
        <ul className="text-xs text-dim space-y-1">
          {l.routeReasons.map((r) => <li key={r}>· {r}</li>)}
        </ul>
      </div>
      <div className="border-t rule pt-3 grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1"><span className="label">quantity</span>
          <input value={q} onChange={(e) => setQ(e.target.value)} className="input text-right" /></label>
        <label className="flex flex-col gap-1"><span className="label">fee bps</span>
          <input value={fee} onChange={(e) => setFee(e.target.value)} className="input text-right" /></label>
        <label className="col-span-2 flex flex-col gap-1"><span className="label">good until (New York)</span>
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
        <label className="col-span-2 flex flex-col gap-1"><span className="label">rationale</span>
          <input value={why} onChange={(e) => setWhy(e.target.value)} placeholder="optional" className="input" /></label>
      </div>
      <div className="flex flex-wrap gap-2">
        <button className="btn-accent" onClick={() => onDecide(l, "APPROVED", n, extra)}>approve {qty(n ?? l.qtyRequested)}</button>
        <button className="btn" onClick={() => onDecide(l, "REJECTED", undefined, extra)}>reject</button>
        <button className="btn ml-auto" onClick={askAgent} title="Ask the reviewer model (needs LOANDESK_AGENT_MODE)">ask agent</button>
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
    <Panel title={<>today's decisions <span className="text-dim normal-case tracking-normal">{rows.length}</span></>}
      right={<button className="btn" onClick={exportCsv}>export csv</button>} className="flex-1" bodyClass="overflow-auto">
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
                    <button className="btn h-6" onClick={() => release(l)}>release</button>}
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
