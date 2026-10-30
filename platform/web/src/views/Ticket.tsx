import { useEffect, useState } from "react";
import { api, qs } from "../lib/api";
import { bps, full, money, parseQty, pct, px, qty, time } from "../lib/format";
import { useDebounced, useQuery } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { Counterparty, Instrument, MarketRow, Order, Preview } from "../lib/types";
import { Empty, Kbd, Panel, Sparkline, Stat, Tabs, TierBadge } from "../components/ui";

export function Ticket() {
  const prefill = useDesk((s) => s.ticket);
  const toast = useDesk((s) => s.toast);
  const setView = useDesk((s) => s.setView);
  const { data: cps } = useQuery<Counterparty[]>("/counterparties");
  const [side, setSide] = useState<"BORROW" | "LEND">("BORROW");
  const [ident, setIdent] = useState("");
  const [q, setQ] = useState("");
  const [fee, setFee] = useState("");
  const [cp, setCp] = useState("");
  const [type, setType] = useState("AUTO");
  const [venue, setVenue] = useState<"AUTO" | "VENUE_A" | "VENUE_B">("AUTO");
  const [override, setOverride] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewErr, setPreviewErr] = useState<string | null>(null);
  const [mkt, setMkt] = useState<MarketRow | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (prefill.instrument) setIdent(prefill.instrument);
    if (prefill.side) setSide(prefill.side);
    if (prefill.quantity) setQ(String(prefill.quantity));
    if (prefill.feeBps !== undefined) setFee(String(prefill.feeBps));
    if (prefill.counterparty) setCp(prefill.counterparty);
  }, [prefill]);

  const body = {
    side, instrument: ident, quantity: parseQty(q) ?? 0, feeBps: fee === "" ? 0 : parseFloat(fee),
    counterparty: cp || null, type: venue === "VENUE_B" ? "AUTO" : type, venue, override,
  };
  const key = useDebounced(JSON.stringify(body), 250);

  useEffect(() => {
    const b = JSON.parse(key);
    if (!b.instrument || b.quantity <= 0) {
      setPreview(null);
      setPreviewErr(null);
      return;
    }
    api.post<Preview>("/orders/preview", b)
      .then((p) => {
        setPreview(p);
        setPreviewErr(null);
        api.get<{ market: MarketRow }>(`/instruments/${p.instrument.id}/market`).then((m) => setMkt(m.market)).catch(() => {});
      })
      .catch((e) => {
        setPreview(null);
        setPreviewErr((e as Error).message);
      });
  }, [key]);

  const send = async () => {
    if (!preview) return;
    setBusy(true);
    try {
      const o = await api.post<Order>("/orders", body);
      toast(o.status === "FAILED" || o.status === "REJECTED" ? "err" : "ok",
        `${o.side.toLowerCase()} ${qty(o.quantity)} ${preview.instrument.composite} @ ${o.feeBps}bps · ${o.status.toLowerCase()}${o.rejectReason ? `: ${o.rejectReason}` : ""}`);
      if (o.status !== "FAILED" && o.status !== "REJECTED") setView("blotter");
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        send();
      }
    };
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  });

  const blocking = preview?.checks.filter((c) => !c.ok && c.blocking) ?? [];
  const warnings = preview?.checks.filter((c) => !c.ok && !c.blocking) ?? [];
  const lenders = cps?.filter((c) => (side === "BORROW" ? c.kind === "AGENT_LENDER" : c.kind !== "AGENT_LENDER" && c.kind !== "INTERNAL"));

  return (
    <div className="h-full grid grid-cols-[400px_1fr] gap-3 min-h-0">
      <Panel title="trade ticket" right={<span className="text-2xs text-faint"><Kbd>ctrl</Kbd>+<Kbd>↵</Kbd> send</span>}
        bodyClass="p-4 flex flex-col gap-4 overflow-auto">
        <Tabs value={side} onChange={setSide} options={[{ id: "BORROW", label: "borrow" }, { id: "LEND", label: "lend" }]} />
        <IdentifierField value={ident} onChange={setIdent} />
        <div className="grid grid-cols-2 gap-3">
          <Field label="quantity"><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="250k" className="input text-right" /></Field>
          <Field label={side === "BORROW" ? "fee bps (max)" : "fee bps (min)"}>
            <input value={fee} onChange={(e) => setFee(e.target.value)} placeholder={preview ? bps(preview.lastFeeBps) : "bps"} className="input text-right" />
          </Field>
          <Field label={side === "BORROW" ? "lender" : "borrower"}>
            <select value={cp} onChange={(e) => setCp(e.target.value)} className="input">
              <option value="">best available</option>
              {lenders?.map((c) => <option key={c.code} value={c.code}>{c.code}</option>)}
            </select>
          </Field>
          <Field label="venue">
            <select value={venue} onChange={(e) => setVenue(e.target.value as typeof venue)} className="input">
              <option value="AUTO">auto (fails over to B)</option>
              <option value="VENUE_A">venue A</option>
              <option value="VENUE_B">venue B · interest list</option>
            </select>
          </Field>
          <Field label="order type">
            <select value={venue === "VENUE_B" ? "INTEREST_LIST" : type} onChange={(e) => setType(e.target.value)}
              className="input" disabled={venue === "VENUE_B"}>
              {(venue === "VENUE_B" ? ["INTEREST_LIST"] : ["AUTO", "FIRM_BID", "BID", "OFFER"]).map((t) => <option key={t}>{t}</option>)}
            </select>
          </Field>
        </div>

        {preview && (
          <div className="border-t rule pt-3 space-y-3">
            <div className="text-xs">
              <span className="label">route</span>
              <div className="mt-1 text-ink">{preview.venue.replace("_", " ").toLowerCase()} · {preview.type.toLowerCase().replace("_", " ")}</div>
              <div className="text-faint">{preview.routeReason}</div>
            </div>
            <div className="grid grid-cols-3 gap-3 text-xs">
              <Stat label="notional" value={`${money(preview.notional)} ${preview.instrument.ccy}`} />
              <Stat label="settle" value={String(preview.terms.settlementDate)} />
              <Stat label="agreement" value={String(preview.terms.legalAgreement)} />
              <Stat label="collateral" value={`${preview.terms.collateralType} ${preview.terms.collateralCcy}`} />
              <Stat label="margin" value={`${preview.terms.marginPct}%`} />
              <Stat label="term" value={String(preview.terms.term)} />
            </div>
          </div>
        )}

        {preview && (
          <div className="border-t rule pt-3">
            <div className="label mb-2">pre-trade checks</div>
            <ul className="space-y-1.5 text-xs">
              {preview.checks.map((c) => (
                <li key={c.name} className="flex gap-2">
                  <span className={c.ok ? "text-up" : c.blocking ? "text-down" : "text-warm"}>{c.ok ? "✓" : c.blocking ? "✕" : "!"}</span>
                  <span className={c.ok ? "text-dim" : "text-ink"}>{c.name}</span>
                  <span className="ml-auto text-faint text-right truncate max-w-[200px]" title={c.detail}>{c.detail}</span>
                </li>
              ))}
            </ul>
            {!!warnings.length && (
              <label className="mt-3 flex items-center gap-2 text-xs text-warm">
                <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
                override {warnings.length} warning{warnings.length > 1 ? "s" : ""}
              </label>
            )}
          </div>
        )}
        {previewErr && <div className="text-xs text-down">{previewErr}</div>}

        <button className="btn-accent h-9 justify-center mt-auto" disabled={!preview || !!blocking.length || busy || (!!warnings.length && !override)} onClick={send}>
          {blocking.length ? "blocked by checks" : `send ${side.toLowerCase()} to ${preview?.venue.replace("_", " ").toLowerCase() ?? "venue"}`}
        </button>
      </Panel>

      <InstrumentCard inst={preview?.instrument} mkt={mkt} />
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="flex flex-col gap-1"><span className="label">{label}</span>{children}</label>;
}

function IdentifierField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [focus, setFocus] = useState(false);
  const [hits, setHits] = useState<Instrument[]>([]);
  const [sel, setSel] = useState(0);
  const q = useDebounced(value, 120);
  useEffect(() => {
    if (!focus || q.trim().length < 1) return setHits([]);
    api.get<Instrument[]>("/instruments/search" + qs({ q, limit: 8 })).then(setHits).catch(() => setHits([]));
  }, [q, focus]);
  const pick = (i: Instrument) => {
    onChange(i.composite);
    setHits([]);
  };
  return (
    <Field label="instrument">
      <div className="relative">
        <input
          value={value}
          onChange={(e) => { onChange(e.target.value); setSel(0); }}
          onFocus={() => setFocus(true)}
          onBlur={() => setTimeout(() => setFocus(false), 120)}
          onKeyDown={(e) => {
            if (!hits.length) return;
            if (e.key === "ArrowDown") { setSel((s) => Math.min(s + 1, hits.length - 1)); e.preventDefault(); }
            else if (e.key === "ArrowUp") { setSel((s) => Math.max(s - 1, 0)); e.preventDefault(); }
            else if (e.key === "Enter" || e.key === "Tab") { pick(hits[sel]); if (e.key === "Enter") e.preventDefault(); }
          }}
          placeholder="ticker, ISIN, SEDOL, CUSIP, FIGI"
          className="input w-full"
          autoFocus
        />
        {focus && !!hits.length && (
          <ul className="absolute z-20 mt-1 w-full panel bg-panel py-1 shadow-xl shadow-black/30">
            {hits.map((h, i) => (
              <li key={h.id}>
                <button onMouseDown={() => pick(h)} className={`w-full h-7 px-2 flex items-center gap-2 text-xs text-left ${i === sel ? "bg-raised" : ""}`}>
                  <span className="w-20 text-ink">{h.composite}</span>
                  <span className="text-faint truncate flex-1">{h.name}</span>
                  <TierBadge tier={h.tier} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Field>
  );
}

function InstrumentCard({ inst, mkt }: { inst: Instrument | undefined; mkt: MarketRow | null }) {
  if (!inst) return <Panel title="instrument"><Empty>Type a ticker or identifier. Check digits are validated as you type; typos get a "did you mean".</Empty></Panel>;
  return (
    <div className="flex flex-col gap-3 min-h-0">
      <Panel title="instrument" right={<TierBadge tier={inst.tier} />} bodyClass="p-4">
        <div className="flex items-baseline gap-3">
          <span className="text-lg">{inst.composite}</span>
          <span className="text-dim truncate">{inst.name}</span>
          {inst.etb && <span className="text-2xs text-gc border border-line rounded px-1">ETB</span>}
          {inst.thresholdFlag && <span className="text-2xs text-hot border border-hot/40 rounded px-1">THRESHOLD</span>}
          {inst.restricted && <span className="text-2xs text-down border border-down/40 rounded px-1">RESTRICTED</span>}
        </div>
        <div className="mt-1 text-2xs text-faint">{inst.isin} · {inst.sedol} · {inst.cusip ?? "—"} · {inst.figi} · {inst.sector}</div>
        <div className="mt-4 grid grid-cols-4 lg:grid-cols-8 gap-4">
          <Stat label="last px" value={`${px(inst.lastPx, inst.ccy)} ${inst.ccy}`} />
          <Stat label="fee bps" value={bps(inst.feeBps)} />
          <Stat label="util" value={pct(inst.utilPct)} />
          <Stat label="short int" value={pct(inst.siPct)} />
          <Stat label="dtc" value={inst.daysToCover.toFixed(1)} />
          <Stat label="adv 20d" value={qty(inst.adv20d)} />
          <Stat label="shs out" value={qty(inst.shsOut)} />
          <Stat label="country" value={inst.country} />
        </div>
      </Panel>
      <div className="grid grid-cols-2 gap-3 flex-1 min-h-0">
        <Panel title="availability" bodyClass="p-4 space-y-3 text-xs">
          {!mkt ? <Empty>loading…</Empty> : (
            <>
              <Row k="venue A" v={mkt.venueA ? `${qty(mkt.venueA.qty)} · ${bps(mkt.venueA.fee)} bps · ${mkt.venueA.lenders} lenders` : "no feed"} />
              <Row k="venue B" v={mkt.venueB ? `${qty(mkt.venueB.qty)} · ${bps(mkt.venueB.fee)} bps` : "interest lists only"} />
              <Row k="held by locates" v={full(mkt.held)} />
              <Row k="live available" v={<span className="text-accent">{full(mkt.available)}</span>} />
              <div className="flex items-center gap-3 pt-2"><span className="label">5d fee</span><Sparkline values={mkt.trend} w={160} h={24} /></div>
            </>
          )}
        </Panel>
        <Panel title="last prints" bodyClass="overflow-auto">
          {!mkt?.prints?.length ? <Empty>No prints seen yet.</Empty> : (
            <table className="w-full text-xs">
              <thead><tr><th className="th">time</th><th className="th">venue</th><th className="th text-right">qty</th><th className="th text-right">fee</th></tr></thead>
              <tbody>{mkt.prints.map((p, i) => (
                <tr key={i} className="row"><td className="td text-faint">{time(p.ts)}</td><td className="td text-dim">{p.venue.replace("_", " ").toLowerCase()}</td>
                  <td className="td text-right">{qty(p.qty)}</td><td className="td text-right">{bps(p.feeBps)}</td></tr>
              ))}</tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return <div className="flex justify-between gap-4"><span className="text-faint">{k}</span><span>{v}</span></div>;
}
