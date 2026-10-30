import { api } from "../lib/api";
import { actor, bps, full, money, qty, when } from "../lib/format";
import { useKeys, useQuery, useSelection } from "../lib/hooks";
import { useDesk } from "../lib/store";
import type { BuyIn, DeliveryFail, ShortList, ShortListLine } from "../lib/types";
import { Empty, Kbd, Panel, PreBorrowTag, Stat, State, TierBadge } from "../components/ui";

const TOPICS = ["short_list", "order", "trade", "action", "market"];

/** What the desk must borrow today: clients' shorts settling next, less the box, less covers already working;
 *  and what it holds that nobody needs any more, to return. */
export function ShortListView() {
  const { data, error, reload } = useQuery<ShortList>("/short-list", TOPICS);
  const { data: fails, reload: reloadFails } = useQuery<DeliveryFail[]>("/fails", TOPICS);
  const { data: buyIns } = useQuery<BuyIn[]>("/buy-ins", TOPICS);
  const toast = useDesk((s) => s.toast);
  const openTicket = useDesk((s) => s.openTicket);
  const rows = data?.lines ?? [];
  const [sel, setSel] = useSelection(rows.length);
  const cur = rows[sel];
  const sum = data?.summary ?? {};

  const draft = async () => {
    try {
      const r = await api.post<{ drafted: string[]; notes: string[] }>("/short-list/draft");
      toast(r.drafted.length ? "ok" : "info", r.drafted.length
        ? `drafted ${r.drafted.length} card${r.drafted.length > 1 ? "s" : ""}: approve them in Agent`
        : r.notes[0] ?? "nothing left to cover or return");
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  const refresh = async () => {
    try {
      await api.post("/short-list/refresh");
      reload();
    } catch (e) {
      toast("warn", (e as Error).message);
    }
  };
  const borrow = (l: ShortListLine) =>
    openTicket({ instrument: l.composite, side: "BORROW", quantity: Math.ceil(l.uncovered / 100) * 100 });

  useKeys((e) => {
    if (e.key === "d") draft();
    else if (e.key === "b" && cur?.uncovered) borrow(cur);
    else return;
    e.preventDefault();
  }, [cur]);

  return (
    <div className="h-full flex flex-col gap-3 min-h-0">
      <div className="grid grid-cols-3 md:grid-cols-8 gap-3">
        <Stat label="stock record" value={data?.tradeDate ?? "—"} />
        <Stat label="settles" value={data?.settlementDate ?? "—"} />
        <Stat label="names short" value={full(sum.short)} tone={sum.short ? "text-hot" : ""} />
        <Stat label="working" value={full(sum.working)} tone={sum.working ? "text-warm" : ""} />
        <Stat label="covered" value={full(sum.covered)} tone="text-up" />
        <Stat label="to borrow" value={money(sum.uncoveredNotional)} tone={sum.uncoveredNotional ? "text-hot" : ""} />
        <Stat label="excess · $/day" value={sum.excess ? `${money(sum.excessNotional)} · ${money(sum.excessCostUsd)}` : "—"}
          tone={sum.excess ? "text-warm" : ""} />
        <Stat label="rule 204 · pre-borrow" value={sum.cnsFails ? `${full(sum.closeOut)} · ${full(sum.preBorrow)}` : "—"}
          tone={sum.preBorrow ? "text-down" : sum.closeOut ? "text-hot" : ""} />
      </div>
      {!!fails?.length && <FailsPanel fails={fails} onDone={() => { reload(); reloadFails(); }} />}
      {!!buyIns?.length && <BuyInsPanel rows={buyIns} />}
      <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px] gap-3">
        <Panel
          title={<>short list <span className="text-dim normal-case tracking-normal">{rows.length}</span></>}
          right={<div className="flex items-center gap-2 text-2xs text-faint">
            <span><Kbd>d</Kbd> draft covers &amp; returns</span><span><Kbd>b</Kbd> borrow</span>
            <button className="btn h-6" onClick={refresh}>refresh</button>
            <button className="btn-accent h-6" onClick={draft}>draft</button>
          </div>}
          bodyClass="overflow-auto"
        >
          {error && <Empty>{error}</Empty>}
          {!error && !data?.tradeDate && <Empty>No stock record yet. It lands after the close: clients' short positions settling the next business day, and how much of each locate they used.</Empty>}
          {!error && data?.tradeDate && !rows.length && <Empty>No client shorts at the {data.tradeDate} close.</Empty>}
          {!!rows.length && (
            <table className="w-full text-xs">
              <thead>
                <tr>
                  {["name", "tier", "clients", "client short", "sold", "box", "fail in", "fail out", "recalled", "short by", "working", "to borrow", "excess", "status"].map((h) => (
                    <th key={h} className={`th ${["name", "tier", "status"].includes(h) ? "" : "text-right"}`}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((l, i) => (
                  <tr key={l.instrumentId} onClick={() => setSel(i)} className={`row cursor-default ${i === sel ? "row-sel" : ""}`}>
                    <td className="td">
                      <span className="text-ink">{l.composite}</span>
                      {l.preBorrow && <span className="ml-2"><PreBorrowTag /></span>}
                      <span className="text-faint ml-2 hidden 2xl:inline">{l.name?.slice(0, 22)}</span>
                    </td>
                    <td className="td"><TierBadge tier={l.tier} /></td>
                    <td className="td text-right text-dim" title={l.clients.map((c) => `${c.counterparty} ${full(c.short)}`).join("\n")}>
                      {l.clients.length}</td>
                    <td className="td text-right">{qty(l.clientShort)}</td>
                    <td className="td text-right text-dim">{l.soldToday ? qty(l.soldToday) : "—"}</td>
                    <td className="td text-right text-dim" title={l.unbooked ? `incl. ${full(l.unbooked)} not booked at the processor yet` : ""}>
                      {qty(Math.max(l.box, 0) + l.unbooked)}</td>
                    <td className={`td text-right ${l.fails ? "text-hot" : "text-faint"}`} title="our borrows failing to arrive">{l.fails ? qty(l.fails) : "—"}</td>
                    <td className={`td text-right ${l.closeOutBy ? "text-hot" : l.cnsFails ? "text-warm" : "text-faint"}`}
                      title={l.closeOutBy ? `failed at CNS: Rule 204 close-out by ${when(l.closeOutBy)} New York` : l.cnsFails ? "closed out: delivers once the shares settle" : ""}>
                      {l.cnsFails ? qty(l.cnsFails) : "—"}</td>
                    <td className={`td text-right ${l.recalled ? "text-warm" : "text-faint"}`}>{l.recalled ? qty(l.recalled) : "—"}</td>
                    <td className="td text-right">{l.shortfall ? qty(l.shortfall) : "—"}</td>
                    <td className="td text-right text-warm">{l.working || l.drafted.length ? qty(l.shortfall - l.uncovered) : "—"}</td>
                    <td className={`td text-right ${l.uncovered ? "text-hot" : "text-faint"}`}>{l.uncovered ? qty(l.uncovered) : "—"}</td>
                    <td className={`td text-right ${l.excess ? "text-warm" : "text-faint"}`}
                      title={l.returning.length ? `${full(l.excess - l.toReturn)} at the gate to return` : ""}>
                      {l.excess ? qty(l.excess) : "—"}</td>
                    <td className="td"><State s={l.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        <LineDetail l={cur} onBorrow={borrow} />
      </div>
    </div>
  );
}

function LineDetail({ l, onBorrow }: { l: ShortListLine | undefined; onBorrow: (l: ShortListLine) => void }) {
  const setView = useDesk((s) => s.setView);
  if (!l) return <Panel title="detail"><Empty>Select a name.</Empty></Panel>;
  return (
    <Panel title="detail" right={<State s={l.status} />} bodyClass="p-3 flex flex-col gap-4 overflow-auto">
      <div>
        <div className="flex items-center gap-2">
          <span className="text-sm">{l.composite}</span>
          <TierBadge tier={l.tier} />
          {l.etb && <span className="text-2xs text-gc border border-line rounded px-1">ETB</span>}
        </div>
        <div className="text-xs text-faint truncate">{l.name}</div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <Stat label="client short" value={full(l.clientShort)} />
        <Stat label="box" value={full(l.box)} />
        <Stat label="unbooked" value={full(l.unbooked)} />
        <Stat label="failing in" value={full(l.fails)} tone={l.fails ? "text-hot" : ""} />
        <Stat label="failed at CNS" value={full(l.cnsFails)} tone={l.closeOutBy ? "text-hot" : l.cnsFails ? "text-warm" : ""} />
        <Stat label="recalled" value={full(l.recalled)} tone={l.recalled ? "text-warm" : ""} />
        <Stat label="short by" value={full(l.shortfall)} tone={l.shortfall ? "text-hot" : "text-up"} />
        <Stat label="market bps" value={bps(l.feeBps)} />
      </div>
      <div className="border-t rule pt-3">
        <div className="label mb-1.5">clients</div>
        <table className="w-full text-xs">
          <thead><tr>{["client", "short", "sold", "bought back"].map((h) =>
            <th key={h} className={`th ${h === "client" ? "" : "text-right"}`}>{h}</th>)}</tr></thead>
          <tbody>
            {l.clients.map((c) => (
              <tr key={c.counterparty} className="row">
                <td className="td text-dim">{c.counterparty}</td>
                <td className="td text-right">{full(c.short)}</td>
                <td className="td text-right text-dim">{c.sold ? full(c.sold) : "—"}</td>
                <td className="td text-right text-dim">{c.covered ? full(c.covered) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(!!l.cnsFails || l.preBorrow) && (
        <div className="border-t rule pt-3">
          <div className="label mb-1.5">rule 204</div>
          {l.closeOutBy && <div className="text-xs text-hot">· {full(l.cnsFails)} failed to deliver at CNS: close out by {when(l.closeOutBy)} New York, by borrowing or buying the shares</div>}
          {!l.closeOutBy && !!l.cnsFails && <div className="text-xs text-dim">· {full(l.cnsFails)} closed out; delivered against the fail once the shares settle</div>}
          {l.preBorrow && <div className="text-xs text-down">· a close-out was missed: pre-borrow (Rule 204(b)) until the fail delivers. A locate in the name needs shares we hold</div>}
        </div>
      )}
      <div className="border-t rule pt-3">
        <div className="label mb-1.5">covers</div>
        {!l.drafted.length && !l.working && <div className="text-xs text-faint">{l.shortfall ? "none working" : "none needed"}</div>}
        {!!l.working && <div className="text-xs text-dim">· {full(l.working)} on open borrow orders</div>}
        <ul className="text-xs text-dim space-y-1">
          {l.drafted.map((a) => <li key={a.actionId}>· {a.title} <span className="text-faint">({actor(a.kind)}, at the gate)</span></li>)}
        </ul>
      </div>
      {(!!l.excess || !!l.returnedSinceBox) && (
        <div className="border-t rule pt-3">
          <div className="label mb-1.5">returns</div>
          {!!l.excess && <div className="text-xs text-dim">· {full(l.excess)} more than the clients need, about ${full(Math.round(l.excessCostUsd))} a day at {bps(l.feeBps)} bps</div>}
          {!!l.returnedSinceBox && <div className="text-xs text-dim">· {full(l.returnedSinceBox)} returned since the box was read</div>}
          <ul className="text-xs text-dim space-y-1">
            {l.returning.map((a) => <li key={a.actionId}>· {a.title} <span className="text-faint">(at the gate)</span></li>)}
          </ul>
          {!!l.toReturn && <div className="text-xs text-warm mt-1">{full(l.toReturn)} not drafted yet: press <Kbd>d</Kbd></div>}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {!!(l.drafted.length || l.returning.length) && <button className="btn-accent" onClick={() => setView("agent")}>review cards</button>}
        {!!l.uncovered && <button className="btn" onClick={() => onBorrow(l)}>borrow {qty(l.uncovered)} by hand</button>}
      </div>
    </Panel>
  );
}

/** Time to a deadline in the sim's clock: "2h 10m", or "missed". */
function left(deadline: string, now: string | undefined): string {
  if (!now) return "";
  const m = Math.round((Date.parse(deadline) - Date.parse(now)) / 60_000);
  if (m <= 0) return "missed";
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

/** Client shorts that failed to deliver at CNS: Rule 204 wants each closed out by the next open. */
function FailsPanel({ fails, onDone }: { fails: DeliveryFail[]; onDone: () => void }) {
  const toast = useDesk((s) => s.toast);
  const now = useDesk((s) => s.clock?.now);
  const deliver = async (f: DeliveryFail) => {
    try {
      await api.post(`/fails/${f.id}/deliver`, {});
      toast("ok", `delivered ${qty(f.openQty)} ${f.composite} against ${f.id}`);
      onDone();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  return (
    <Panel title={<>failed at CNS · rule 204 <span className="text-dim normal-case tracking-normal">{fails.length}</span></>}
      right={<span className="text-2xs text-faint">closed out by borrowing or buying before the open; delivered once the shares settle</span>}
      bodyClass="overflow-auto max-h-40">
      <table className="w-full text-xs">
        <thead><tr>{["fail", "client", "name", "failed", "open", "close out by", "left", "status", ""].map((h) =>
          <th key={h} className={`th ${["open"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
        <tbody>
          {fails.map((f) => {
            const due = f.status === "OPEN" ? left(f.deadline, now) : "";
            return (
              <tr key={f.id} className="row">
                <td className="td text-faint">{f.id}</td>
                <td className="td text-dim">{f.counterparty}</td>
                <td className="td">{f.composite ?? f.instrumentId}{f.preBorrow && <span className="ml-2"><PreBorrowTag /></span>}</td>
                <td className="td text-dim">{f.settlementDate}</td>
                <td className="td text-right">{full(f.openQty)}</td>
                <td className="td text-dim">{when(f.deadline)}</td>
                <td className={`td ${f.missed ? "text-down" : due ? "text-hot" : "text-faint"}`}>{f.missed ? "missed" : due || "—"}</td>
                <td className="td"><State s={f.status} /></td>
                <td className="td text-right"><button className="btn h-6" onClick={() => deliver(f)}
                  title="Deliver shares we hold against the fail (the CNS-DELIVER policy does this once they settle)">deliver</button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Panel>
  );
}

/** Shares bought in against us, newest first: what each cost, and who bears it. */
function BuyInsPanel({ rows }: { rows: BuyIn[] }) {
  const setView = useDesk((s) => s.setView);
  const open = rows.filter((b) => ["TO_PAY", "TO_RECEIVE", "INSTRUCTED"].includes(b.cashStatus ?? ""))
    .reduce((a, b) => a + b.amountDueUsd, 0);
  const toCharge = rows.filter((b) => b.chargeStatus === "TO_CHARGE").reduce((a, b) => a + b.amountDueUsd, 0);
  const cards = rows.some((b) => b.cashStatus === "TO_PAY" || b.cashStatus === "TO_RECEIVE" || b.chargeStatus === "TO_CHARGE");
  return (
    <Panel title={<>bought in <span className="text-dim normal-case tracking-normal">{rows.length} · {money(open)} to settle · {money(toCharge)} to charge clients</span></>}
      right={<div className="flex items-center gap-2 text-2xs text-faint">
        <span>a lender's on a recall not returned in time; the buyer's at CNS on a client fail, charged to the client</span>
        {cards && <button className="btn-accent h-6" onClick={() => setView("agent")}>review cards</button>}
      </div>}
      bodyClass="overflow-auto max-h-32">
      <table className="w-full text-xs">
        <thead><tr>{["id", "kind", "from", "name", "qty", "price", "due", "cash", "client", "when"].map((h) =>
          <th key={h} className={`th ${["qty", "price", "due"].includes(h) ? "text-right" : ""}`}>{h}</th>)}</tr></thead>
        <tbody>
          {rows.map((b) => (
            <tr key={b.id} className="row">
              <td className="td text-faint">{b.id}</td>
              <td className="td text-dim">{b.kind === "CNS" ? "CNS fail" : `recall · ${b.origin === "venue_b" ? "Venue B" : "processor"}`}</td>
              <td className="td text-dim" title={b.kind === "CNS" ? "the client it's charged to (Rule 204(d))" : "the lender"}>{b.counterparty}</td>
              <td className="td">{b.composite ?? b.instrumentId}</td>
              <td className="td text-right">{full(b.quantity)}</td>
              <td className="td text-right text-dim">{b.price.toFixed(2)}</td>
              <td className={`td text-right ${b.amountDue > 0 ? "text-down" : "text-up"}`}>{money(b.amountDueUsd)}</td>
              <td className="td" title={b.cashStatus === "CNS" ? "NSCC's money settlement takes it" : b.cashRef ?? ""}><State s={b.cashStatus} /></td>
              <td className="td" title={b.chargeRef ?? ""}>{b.kind === "CNS" ? <State s={b.chargeStatus} /> : <span className="text-faint">—</span>}</td>
              <td className="td text-dim">{when(b.boughtInAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}
