import { SignInButton } from "@clerk/react";
import { authEnabled } from "../lib/auth";

const STEPS = [
  ["chat", "A borrow agreed in Desk Chat is read in context and drafted as an order card."],
  ["route", "GC to firm bids, specials negotiated, Venue B lists when Venue A is down."],
  ["fill", "Fills from either venue book themselves to the processor and compare field by field."],
  ["break", "A break comes back as a drafted fix: the trader approves, edits or rejects."],
  ["settle", "Matched contracts instruct the custodian; returns, recalls and re-rates follow the loan."],
];

const FEATURES = [
  { title: "Locates", tone: "text-gc", body: "ETB and GC names clear on a standing policy with a Reg SHO 203(b) basis. Warm names get a reviewed decision; hot names wait for you." },
  { title: "Trading", tone: "text-warm", body: "One ticket for two venues, with live pre-trade checks: limits, restricted list, concentration, fat finger and the lending fee floor." },
  { title: "Booking", tone: "text-hot", body: "CDM trades to the processor, Rule 10c-1a fields captured at booking, settlement on ISO 20022 sese.023, and daily marks." },
];

/** Signed-out front door. ``onDevEnter``: with sign-in not configured, local dev can still open the desk. */
export function Landing({ onDevEnter }: { onDevEnter?: () => void }) {
  const cta = authEnabled ? (
    <SignInButton mode="modal">
      <button className="btn-accent h-9 px-4 text-sm">Sign in to the desk</button>
    </SignInButton>
  ) : onDevEnter ? (
    <button className="btn-accent h-9 px-4 text-sm" onClick={onDevEnter}>Open the desk (dev, no sign-in)</button>
  ) : null;

  return (
    <div className="h-full overflow-auto">
      <div className="max-w-5xl mx-auto px-4 sm:px-8">
        <header className="h-14 flex items-center justify-between">
          <div className="flex items-baseline gap-2">
            <span className="text-sm font-medium tracking-[0.22em]">LOANDESK</span>
            <span className="text-2xs text-faint">sec lending desk</span>
          </div>
          {authEnabled && (
            <SignInButton mode="modal"><button className="btn">Sign in</button></SignInButton>
          )}
        </header>

        <section className="pt-16 pb-14 sm:pt-24">
          <div className="label mb-4">Securities lending · agent drafts, trader approves</div>
          <h1 className="text-3xl sm:text-5xl leading-tight font-medium text-ink max-w-3xl">
            Chat to venue to books, without retyping a single line.
          </h1>
          <p className="mt-5 text-base text-dim max-w-2xl">
            LoanDesk reads the desk's chat, routes borrows and loans across two venues, books every fill and
            settles it. Every outgoing action waits for a trader's approval, and every step is in the audit trail.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            {cta}
            {!authEnabled && (
              <span className="text-xs text-warm">
                Sign-in isn't configured: set VITE_CLERK_PUBLISHABLE_KEY{onDevEnter ? "" : " to open the desk"}.
              </span>
            )}
          </div>
        </section>

        <section className="grid gap-3 sm:grid-cols-3">
          {FEATURES.map((f) => (
            <div key={f.title} className="panel p-4">
              <div className={`label ${f.tone}`}>{f.title}</div>
              <p className="mt-2 text-xs text-dim leading-relaxed">{f.body}</p>
            </div>
          ))}
        </section>

        <section className="panel mt-3 p-4">
          <div className="label mb-3">One trade, end to end</div>
          <ol className="grid gap-3 sm:grid-cols-5">
            {STEPS.map(([k, v], i) => (
              <li key={k} className="text-xs">
                <div className="text-accent tabular-nums">{String(i + 1).padStart(2, "0")} · {k}</div>
                <p className="mt-1 text-dim leading-relaxed">{v}</p>
              </li>
            ))}
          </ol>
        </section>

        <footer className="py-10 text-2xs text-faint leading-relaxed max-w-3xl">
          All data is synthetic. Not affiliated with or endorsed by EquiLend, Wematch, FIS or Bloomberg; all names
          are trademarks of their owners. Venues, processor, custodian and chat are simulators.
        </footer>
      </div>
    </div>
  );
}
