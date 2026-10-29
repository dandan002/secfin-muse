/** Desk shorthand: 250k, 1.25m. */
export function qty(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return trim(n / 1e9) + "b";
  if (a >= 1e6) return trim(n / 1e6) + "m";
  if (a >= 1e4) return trim(n / 1e3) + "k";
  return n.toLocaleString("en-US");
}

function trim(x: number): string {
  const s = x.toFixed(x >= 100 ? 0 : x >= 10 ? 1 : 2);
  return s.includes(".") ? s.replace(/\.?0+$/, "") : s; // 1.50 -> 1.5, but 500 stays 500
}

export const full = (n: number | null | undefined) => (n === null || n === undefined ? "—" : n.toLocaleString("en-US"));

export function bps(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n >= 100 ? n.toFixed(0) : n.toFixed(1);
}

export function pct(n: number | null | undefined, d = 1): string {
  return n === null || n === undefined ? "—" : n.toFixed(d) + "%";
}

export function px(n: number | null | undefined, ccy?: string): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: ccy === "JPY" ? 0 : 2, maximumFractionDigits: 2 });
}

export function money(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return qty(Math.round(n));
}

const NY = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** Stored in UTC, shown in exchange-local (New York) time. */
export function time(iso: string | null | undefined): string {
  return iso ? NY.format(new Date(iso)) : "—";
}

/** "250k" | "1.2m" | "50,000" → shares. */
export function parseQty(s: string): number | null {
  const m = s.trim().replace(/,/g, "").match(/^(\d+(?:\.\d+)?)\s*(k|m|mm)?$/i);
  if (!m) return null;
  const mult = { k: 1e3, m: 1e6, mm: 1e6 }[(m[2] ?? "").toLowerCase() as "k" | "m" | "mm"] ?? 1;
  return Math.round(parseFloat(m[1]) * mult);
}

export const actor = (a: string | null | undefined) => (a ?? "—").replace(/^trader:/, "").replace(/^policy:/, "⚙ ");
