import { ReactNode, useEffect, useRef, useState } from "react";
import type { Tier } from "../lib/types";

export function Panel({ title, right, children, className = "", bodyClass = "" }: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
}) {
  return (
    <section className={`panel flex flex-col min-h-0 ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 h-10 px-4 border-b border-line shrink-0">
          <div className="text-xs font-medium text-ink min-w-0 truncate"><span className="block first-letter:uppercase">{title}</span></div>
          <div className="flex items-center gap-2">{right}</div>
        </header>
      )}
      <div className={`min-h-0 flex-1 ${bodyClass}`}>{children}</div>
    </section>
  );
}

/** The tier is a word, not a chip. Only Hot takes colour. */
export function TierBadge({ tier }: { tier: Tier | null | undefined }) {
  if (!tier) return <span className="text-faint">—</span>;
  return <span className={tier === "HOT" ? "text-hot" : "text-dim"}>{tier === "GC" ? "GC" : tier[0] + tier.slice(1).toLowerCase()}</span>;
}

/** Statuses in four tones: done (green), broken (red, and the word goes red), needs a look (amber), and
 *  everything in flight or finished without note (grey). Colour always comes with the word. */
const GOOD = ["APPROVED", "EXECUTED", "MATCHED", "SETTLED", "USED", "COVERED", "DELIVERED", "CHARGED", "AGREED", "ACTIONED", "FILLED"];
const BAD = ["UNMATCHED", "REJECTED", "FAILED", "INVALID", "FAILING", "HOLD", "SHORT", "BOUGHT_IN", "FLAGGED"];
const WARN = ["COUNTERED", "PARTIAL", "UNBOOKED", "REVIEW", "WORKING", "EXCESS", "CLOSED_OUT", "TO_PAY", "TO_RECEIVE",
  "TO_CHARGE", "UNSURE", "PENDING"];

export function tone(s: string) {
  return GOOD.includes(s) ? "good" : BAD.includes(s) ? "bad" : WARN.includes(s) ? "warn" : "neutral";
}

const DOT = { good: "bg-up", bad: "bg-down", warn: "bg-hot", neutral: "bg-faint" };
const TEXT = { good: "text-ink", bad: "text-down", warn: "text-ink", neutral: "text-dim" };

export function State({ s }: { s: string | null | undefined }) {
  if (!s) return <span className="text-faint">—</span>;
  const t = tone(s);
  const word = s.toLowerCase().replace(/_/g, " ");
  return (
    <span className={`inline-flex items-center gap-1.5 ${TEXT[t]}`}>
      <span className={`dot ${DOT[t]}`} />
      {word[0].toUpperCase() + word.slice(1)}
    </span>
  );
}

/** Rule 204(b): a CNS fail in the name missed its close-out, so a short needs the shares borrowed first. */
export function PreBorrowTag() {
  return (
    <span className="text-2xs text-hot"
      title="Rule 204(b): a client's fail at CNS wasn't closed out by the open. Until it delivers, a locate needs shares we hold, not street supply.">
      pre-borrow
    </span>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="h-full min-h-[120px] flex items-center justify-center text-sm text-faint">
      <div className="px-5 py-4 text-center max-w-md">
        {children}
      </div>
    </div>
  );
}

export function Sparkline({ values, w = 64, h = 16 }: { values: number[]; w?: number; h?: number }) {
  if (values.length < 2) return <span className="inline-block text-faint text-2xs" style={{ width: w }}>·</span>;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - 1 - ((v - lo) / span) * (h - 2)}`);
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={w} height={h} className={up ? "text-up" : "text-down"} aria-hidden>
      <polyline points={pts.join(" ")} fill="none" stroke="currentColor" strokeWidth={1} strokeLinejoin="round" />
    </svg>
  );
}

/** A number that flashes green/red for 600 ms when it moves. */
export function Flash({ value, children }: { value: number | null | undefined; children: ReactNode }) {
  const prev = useRef(value);
  const [cls, setCls] = useState("");
  useEffect(() => {
    if (prev.current !== undefined && prev.current !== null && value !== null && value !== undefined
      && value !== prev.current) {
      setCls(value > prev.current ? "flash-up" : "flash-down");
      const t = setTimeout(() => setCls(""), 600);
      prev.current = value;
      return () => clearTimeout(t);
    }
    prev.current = value;
  }, [value]);
  return <span className={`rounded px-0.5 -mx-0.5 ${cls}`}>{children}</span>;
}

export function Stat({ label, value, tone = "" }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <span className="label block first-letter:uppercase">{label}</span>
      <span className={`text-md font-medium truncate ${tone}`}>{value}</span>
    </div>
  );
}

export function Tabs<T extends string>({ value, options, onChange }: {
  value: T;
  options: { id: T; label: ReactNode }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex items-center gap-0.5 p-0.5 rounded border border-line">
      {options.map((o) => (
        <button
          key={o.id}
          onClick={() => onChange(o.id)}
          className={`h-6 px-2 rounded-sm text-xs font-medium transition-colors first-letter:uppercase ${
            value === o.id ? "bg-raised text-ink" : "text-faint hover:text-ink"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A row of headline figures across one panel: the strip at the top of each screen. */
export function Figures({ items }: { items: { label: string; value: ReactNode; tone?: string; sub?: ReactNode }[] }) {
  return (
    <div className="panel grid shrink-0" style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))` }}>
      {items.map((f, i) => (
        <div key={f.label} className={`flex flex-col gap-1.5 px-4 py-3.5 min-w-0 ${i ? "border-l border-line" : ""}`}>
          <span className="label">{f.label}</span>
          <span className={`fig truncate ${f.tone ?? ""}`}>{f.value}</span>
          {f.sub && <span className="label truncate">{f.sub}</span>}
        </div>
      ))}
    </div>
  );
}

/** A trade's stages as six short segments: done in grey, current in ink, a failure in red. */
export const STAGES = ["Ordered", "Routed", "Filled", "Booked", "Compared", "Settled"];
export function Pipeline({ at, fail }: { at: number; fail?: number }) {
  return (
    <span className="inline-flex gap-0.5" role="img" aria-label={fail != null ? `${STAGES[fail]} failed` : STAGES[Math.min(at, 5)]}>
      {STAGES.map((name, i) => (
        <span key={name} title={name} className={`block w-3 h-[3px] ${
          i === fail ? "bg-down" : i < at ? "bg-dim" : i === at ? "bg-ink" : "bg-line"}`} />
      ))}
    </span>
  );
}
