import { useEffect, useMemo, useRef, useState } from "react";
import { api, qs } from "../lib/api";
import { parseQty } from "../lib/format";
import { useDebounced } from "../lib/hooks";
import { useDesk, VIEWS } from "../lib/store";
import type { Instrument, Locate } from "../lib/types";
import { Kbd, TierBadge } from "./ui";

interface Cmd {
  id: string;
  label: string;
  hint?: string;
  run: () => void | Promise<void>;
}

const LOCATE = /^loc(?:ate)?\s+([\d.,]+[km]?)\s+(.+?)\s+for\s+([\w-]+)$/i;
const TRADE = /^(borrow|lend|b|l)\s+([\d.,]+[km]?)\s+(.+?)(?:\s*@\s*([\d.]+)\s*(?:bps?)?)?(?:\s+(?:from|to)\s+([\w-]+))?$/i;
const SHOW = /^(?:show\s+)?(hot|warm|gc)(?:\s+names?)?(?:\s+over\s+(\d+)%?\s*util)?$/i;

/** Ctrl/⌘+K: "locate 500k VOD LN for KESTRELHF", "borrow 100k AAPL @ 30", "show hot names", a view, or a name. */
export function CommandPalette() {
  const open = useDesk((s) => s.paletteOpen);
  const setOpen = useDesk((s) => s.setPalette);
  const [text, setText] = useState("");
  const [sel, setSel] = useState(0);
  const [hits, setHits] = useState<Instrument[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const q = useDebounced(text, 120);

  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(!useDesk.getState().paletteOpen);
      }
    };
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  }, [setOpen]);

  useEffect(() => {
    if (open) {
      setText("");
      setSel(0);
      setTimeout(() => input.current?.focus(), 0);
    }
  }, [open]);

  useEffect(() => {
    const term = q.trim();
    if (!term || LOCATE.test(term) || TRADE.test(term) || SHOW.test(term) || term.length < 2) {
      setHits([]);
      return;
    }
    api.get<Instrument[]>("/instruments/search" + qs({ q: term, limit: 6 })).then(setHits).catch(() => setHits([]));
  }, [q]);

  const cmds = useMemo<Cmd[]>(() => {
    const d = useDesk.getState();
    const t = text.trim();
    const out: Cmd[] = [];
    const loc = t.match(LOCATE);
    if (loc) {
      const n = parseQty(loc[1]);
      const cp = loc[3].replace(/-/g, "").toUpperCase();
      out.push({
        id: "locate", label: `Create locate: ${loc[1]} ${loc[2].toUpperCase()} for ${cp}`, hint: "enter",
        run: async () => {
          const [l] = await api.post<Locate[]>("/locates", { counterparty: cp,
            lines: [{ identifier: loc[2], quantity: n }] });
          d.toast(l.status === "INVALID" ? "err" : "ok",
            `${l.identifier} ${l.status.toLowerCase()}${l.error ? `: ${l.error}` : ` · route ${l.route.toLowerCase()}`}`);
          d.setView("locates");
        },
      });
    }
    const tr = t.match(TRADE);
    if (tr) {
      const side = tr[1].toLowerCase().startsWith("l") ? "LEND" : "BORROW";
      out.push({
        id: "trade", label: `Open ticket: ${side.toLowerCase()} ${tr[2]} ${tr[3].toUpperCase()}${tr[4] ? ` @ ${tr[4]}bps` : ""}`,
        run: () => d.openTicket({ side, quantity: parseQty(tr[2]) ?? undefined, instrument: tr[3],
          feeBps: tr[4] ? parseFloat(tr[4]) : undefined, counterparty: tr[5]?.replace(/-/g, "").toUpperCase() }),
      });
    }
    const show = t.match(SHOW);
    if (show) {
      out.push({ id: "show", label: `Market: ${show[1].toUpperCase()} names`, run: () => {
        d.setMarketFilter({ tier: show[1].toUpperCase() });
        d.setView("market");
      } });
    }
    for (const h of hits) {
      out.push({ id: h.id, label: `${h.composite}  ${h.name}`, hint: h.tier,
        run: () => d.openTicket({ instrument: h.composite }) });
    }
    const lower = t.toLowerCase();
    for (const v of VIEWS) {
      if (!t || v.label.toLowerCase().startsWith(lower) || `go ${v.id}`.startsWith(lower)) {
        out.push({ id: "view-" + v.id, label: `Go to ${v.label}`, hint: v.key, run: () => d.setView(v.id) });
      }
    }
    if (!t || "theme".startsWith(lower)) out.push({ id: "theme", label: "Toggle theme", run: d.toggleTheme });
    return out;
  }, [text, hits]);

  if (!open) return null;

  const run = async (c: Cmd | undefined) => {
    if (!c) return;
    setOpen(false);
    try {
      await c.run();
    } catch (e) {
      useDesk.getState().toast("err", (e as Error).message);
    }
  };

  return (
    <div className="fixed inset-0 z-40 bg-black/40 flex items-start justify-center pt-[14vh]" onClick={() => setOpen(false)}>
      <div className="panel bg-panel w-[620px] shadow-2xl shadow-black/40" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 px-3 h-11 border-b border-line/60">
          <span className="text-accent">›</span>
          <input
            ref={input}
            value={text}
            onChange={(e) => { setText(e.target.value); setSel(0); }}
            onKeyDown={(e) => {
              if (e.key === "Escape") setOpen(false);
              else if (e.key === "ArrowDown") { setSel((s) => Math.min(s + 1, cmds.length - 1)); e.preventDefault(); }
              else if (e.key === "ArrowUp") { setSel((s) => Math.max(s - 1, 0)); e.preventDefault(); }
              else if (e.key === "Enter") run(cmds[sel]);
            }}
            placeholder="locate 500k VOD LN for KESTRELHF · borrow 100k AAPL @ 30 · show hot names"
            className="flex-1 bg-transparent text-sm focus:outline-none placeholder:text-faint"
          />
          <Kbd>esc</Kbd>
        </div>
        <ul className="max-h-80 overflow-auto py-1">
          {cmds.map((c, i) => (
            <li key={c.id}>
              <button onMouseEnter={() => setSel(i)} onClick={() => run(c)}
                className={`w-full h-8 px-3 flex items-center gap-3 text-left text-xs ${
                  i === sel ? "bg-raised text-ink" : "text-dim"}`}>
                <span className="truncate">{c.label}</span>
                <span className="ml-auto">
                  {c.hint === "GC" || c.hint === "WARM" || c.hint === "HOT" ? <TierBadge tier={c.hint} />
                    : c.hint ? <Kbd>{c.hint}</Kbd> : null}
                </span>
              </button>
            </li>
          ))}
          {!cmds.length && <li className="px-3 py-3 text-xs text-faint">No match.</li>}
        </ul>
      </div>
    </div>
  );
}
