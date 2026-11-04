import type { LenderQuote } from "./types";

export interface Allocation {
  counterparty: string;
  quantity: number;
  feeBps: number;
}

/** Split a size across the cheapest lenders first, each up to what it shows, in round lots of 100. Whatever the
 *  ladder can't cover is left over: the caller says so rather than bidding for shares nobody has. */
export function allocate(total: number, lenders: LenderQuote[], maxFeeBps?: number | null): { lines: Allocation[]; short: number } {
  const lines: Allocation[] = [];
  let left = total;
  for (const l of [...lenders].sort((a, b) => a.feeBps - b.feeBps || b.quantity - a.quantity)) {
    if (left <= 0) break;
    if (maxFeeBps != null && l.feeBps > maxFeeBps) continue;
    const take = Math.min(left, Math.floor(l.quantity / 100) * 100);
    if (take <= 0) continue;
    lines.push({ counterparty: l.counterparty, quantity: take, feeBps: l.feeBps });
    left -= take;
  }
  return { lines, short: Math.max(0, left) };
}

/** The lender to send a whole line to: the cheapest that can fill it alone, else the cheapest with any size. */
export function bestFor(qty: number, lenders: LenderQuote[]): LenderQuote | undefined {
  const sorted = [...lenders].sort((a, b) => a.feeBps - b.feeBps || b.quantity - a.quantity);
  return sorted.find((l) => l.quantity >= qty) ?? sorted[0];
}
