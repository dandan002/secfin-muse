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
                    <td className="td text-right text-dim">{bps(l.instrument?.feeBps)}</td>
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
  const toast = useDesk((s) => s.toast);
  useEffect(() => {
    setQ(l?.suggestedQty ? String(l.suggestedQty) : "");
    setFee(l?.instrument ? String(Math.round(l.instrument.feeBps)) : "");
    setWhy("");
  }, [l?.id, l?.suggestedQty, l?.instrument]);
  if (!l) return <Panel title="detail"><Empty>Select a line.</Empty></Panel>;
  const n = parseQty(q) ?? undefined;
  const extra = { feeBps: fee ? parseFloat(fee) : undefined, rationale: why || undefined };
  const askAgent = async () => {
    try {
      await api.post(`/agent/locates/${l.id}/review`);
      toast("ok", "agent proposal queued for approval");
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
        <Stat label="fee bps" value={bps(l.instrument?.feeBps)} />
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
        <label className="flex flex-col gap-1"><span className="label">ind. fee bps</span>
          <input value={fee} onChange={(e) => setFee(e.target.value)} className="input text-right" /></label>
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
    const head = "id,counterparty,instrument,requested,approved,status,decidedBy,decidedAt,regShoBasis";
    const body = rows.map((l) => [l.id, l.counterparty, l.instrument?.isin ?? l.identifier, l.qtyRequested, l.qtyApproved,
      l.status, l.decidedBy, l.decidedAt, `"${l.regShoBasis ?? ""}"`].join(","));
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
          <thead><tr>{["decided", "cp", "name", "tier", "req", "approved", "status", "by", "reply", "reg sho basis", ""].map((h) =>
            <th key={h} className={`th ${["req", "approved"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((l) => (
              <tr key={l.id} className="row">
                <td className="td text-faint">{time(l.decidedAt ?? l.receivedAt)}</td>
                <td className="td text-dim">{l.counterparty}</td>
                <td className="td">{l.instrument?.composite ?? l.identifier}</td>
                <td className="td"><TierBadge tier={l.tier} /></td>
                <td className="td text-right text-dim">{qty(l.qtyRequested)}</td>
                <td className="td text-right">{qty(l.qtyApproved)}</td>
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
