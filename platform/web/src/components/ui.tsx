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
        <header className="flex items-center justify-between gap-3 h-9 px-3 border-b border-line/60 shrink-0">
          <div className="label flex items-center gap-2">{title}</div>
          <div className="flex items-center gap-2">{right}</div>
        </header>
      )}
      <div className={`min-h-0 flex-1 ${bodyClass}`}>{children}</div>
    </section>
  );
}

const TIER: Record<Tier, string> = {
  GC: "text-gc border-gc/40",
  WARM: "text-warm border-warm/45",
  HOT: "text-hot border-hot/50 bg-hot/[0.06]",
};

export function TierBadge({ tier }: { tier: Tier | null | undefined }) {
  if (!tier) return <span className="text-faint">—</span>;
  return (
    <span className={`inline-flex items-center h-[18px] px-1.5 rounded border text-2xs tracking-wider ${TIER[tier]}`}>
      {tier === "WARM" ? "WARM" : tier}
    </span>
  );
}

const STATE: Record<string, string> = {
  RECEIVED: "text-info",
  PENDING: "text-info",
  SENT: "text-info",
  NEW: "text-dim",
  COUNTERED: "text-warm",
  APPROVED: "text-up",
  EXECUTED: "text-up",
  MATCHED: "text-up",
  BOOKED: "text-dim",
  PARTIAL: "text-warm",
  UNMATCHED: "text-hot",
  REJECTED: "text-down",
  FAILED: "text-down",
  INVALID: "text-down",
  CANCELLED: "text-faint",
  EXPIRED: "text-faint",
  RELEASED: "text-faint",
  UNBOOKED: "text-warm",
  SETTLED: "text-up",
  FAILING: "text-hot",
  OPEN: "text-info",
  CLOSED: "text-faint",
  TAKEN_OVER: "text-dim",
  AUTO: "text-gc",
  REVIEW: "text-warm",
  HOLD: "text-hot",
  USED: "text-up",
  SHORT: "text-hot",
  WORKING: "text-warm",
  COVERED: "text-up",
  EXCESS: "text-warm",
  CLOSED_OUT: "text-warm",
  DELIVERED: "text-up",
};

export function State({ s }: { s: string | null | undefined }) {
  if (!s) return <span className="text-faint">—</span>;
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs ${STATE[s] ?? "text-dim"}`}>
      <span className="w-1.5 h-1.5 rounded-full bg-current opacity-80" />
      {s.toLowerCase().replace("_", " ")}
    </span>
  );
}

/** Rule 204(b): a CNS fail in the name missed its close-out, so a short needs the shares borrowed first. */
export function PreBorrowTag() {
  return (
    <span className="text-2xs text-hot border border-line rounded px-1"
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
    <div className="h-full min-h-[120px] flex items-center justify-center text-xs text-faint">
      <div className="border border-dashed border-line/70 rounded-soft px-5 py-4 text-center max-w-md leading-relaxed">
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
    <svg width={w} height={h} className={up ? "text-up/80" : "text-down/80"} aria-hidden>
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
    <div className="flex flex-col gap-0.5 min-w-0">
      <span className="label">{label}</span>
      <span className={`text-sm truncate ${tone}`}>{value}</span>
    </div>
  );
}

export function Tabs<T extends string>({ value, options, onChange }: {
  value: T;
  options: { id: T; label: ReactNode }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex items-center gap-0.5 p-0.5 rounded-md border border-line/70">
      {options.map((o) => (
        <button
          key={o.id}
          onClick={() => onChange(o.id)}
          className={`h-6 px-2 rounded text-xs transition-colors ${
            value === o.id ? "bg-raised text-ink" : "text-faint hover:text-dim"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
