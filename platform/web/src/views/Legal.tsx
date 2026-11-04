import type { ReactNode } from "react";
import { navigate, ROUTES } from "../lib/router";

const UPDATED = "4 November 2026";
const EMAIL = import.meta.env.VITE_CONTACT_EMAIL as string | undefined; // set it for a private channel
const ISSUES = "https://github.com/dandan002/secfin-muse/issues";

function Page({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-h-full bg-bg text-ink">
      <div className="max-w-2xl mx-auto px-6 py-10 flex flex-col gap-6">
        <a href={ROUTES.landing} className="text-[16px] font-semibold tracking-[-0.02em]"
          onClick={(e) => { e.preventDefault(); navigate(ROUTES.landing); }}>LoanDesk</a>
        <header className="flex flex-col gap-1">
          <h1 className="title m-0">{title}</h1>
          <span className="label">Last updated {UPDATED}</span>
        </header>
        <div className="flex flex-col gap-5 text-xs text-dim leading-relaxed [&_h2]:text-ink [&_h2]:font-medium [&_h2]:text-sm [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:flex [&_ul]:flex-col [&_ul]:gap-1">
          {children}
        </div>
        <footer className="pt-6 border-t border-line text-2xs text-faint flex gap-4">
          <LegalLinks />
        </footer>
      </div>
    </div>
  );
}

const Contact = () => EMAIL
  ? <a className="text-accent hover:underline" href={`mailto:${EMAIL}`}>{EMAIL}</a>
  : <a className="text-accent hover:underline" href={ISSUES} target="_blank" rel="noreferrer">the project's issue tracker</a>;

/** Links to both pages, for any footer. */
export function LegalLinks() {
  const go = (to: string) => (e: React.MouseEvent) => { e.preventDefault(); navigate(to); };
  return (
    <>
      <a href={ROUTES.terms} onClick={go(ROUTES.terms)} className="hover:text-ink">Terms</a>
      <a href={ROUTES.privacy} onClick={go(ROUTES.privacy)} className="hover:text-ink">Privacy</a>
    </>
  );
}

export function Terms() {
  return (
    <Page title="Terms of use">
      <p>LoanDesk is a demonstration of a securities-lending desk. By signing in you agree to these terms.</p>
      <section>
        <h2>A simulation, not a service</h2>
        <p>Every venue, processor, custodian, counterparty and chat on LoanDesk is a simulator, and all data is
          synthetic. Nothing here is a real trade, locate, loan or settlement, and nothing is investment, legal or
          regulatory advice. LoanDesk is not a broker-dealer and is not affiliated with or endorsed by EquiLend,
          Wematch, FIS, Bloomberg or Meta; their names are trademarks of their owners.</p>
      </section>
      <section>
        <h2>Access</h2>
        <ul>
          <li>Access is by invitation. Keep your sign-in to yourself; what you do is recorded under your name.</li>
          <li>Don't enter real personal, client or market data.</li>
          <li>Don't try to reach other people's accounts, get around the desk's controls, or overload the service.</li>
          <li>We may suspend or remove access at any time.</li>
        </ul>
      </section>
      <section>
        <h2>The agent</h2>
        <p>Some proposals on the desk are drafted by AI models. They can be wrong. Review what you approve.</p>
      </section>
      <section>
        <h2>No warranty</h2>
        <p>LoanDesk is provided as is, without warranties of any kind, and may change, pause or shut down at any time.
          To the extent the law allows, we aren't liable for any loss arising from its use.</p>
      </section>
      <section>
        <h2>Changes and contact</h2>
        <p>We may update these terms; the date above says when. Questions: <Contact />.</p>
      </section>
    </Page>
  );
}

export function Privacy() {
  return (
    <Page title="Privacy policy">
      <p>What LoanDesk collects about the people who use it, and what it does with it. Desk data itself (instruments,
        counterparties, trades, chat) is synthetic.</p>
      <section>
        <h2>What we collect</h2>
        <ul>
          <li><span className="text-ink">Your account:</span> email address, name or username and sign-in records,
            held by Clerk, our sign-in provider. Clerk also processes your IP address and device details to keep
            sign-in secure.</li>
          <li><span className="text-ink">Your actions:</span> approvals, edits, rejections and settings changes, each
            recorded with your name and the time in the desk's audit log.</li>
          <li><span className="text-ink">In your browser:</span> Clerk's session cookies, plus your theme, last screen,
            watchlist and trader name in local storage. No analytics, advertising or tracking cookies.</li>
        </ul>
      </section>
      <section>
        <h2>Who processes it</h2>
        <ul>
          <li>Clerk: sign-in and sessions.</li>
          <li>Amazon Web Services (US, Ohio region): hosting, the database and its backups.</li>
          <li>Cloudflare: DNS, TLS and routing to the service.</li>
          <li>Meta's model API, when the agent is on: the synthetic desk data the agent reviews.</li>
          <li>Langfuse, when tracing is on: records of the agent's model calls, with counterparty text removed.</li>
        </ul>
        <p>We don't sell personal information or use it for advertising.</p>
      </section>
      <section>
        <h2>How long we keep it</h2>
        <p>Your account lasts until you or we remove it. The audit log is append-only by design and lasts as long as
          the deployment it belongs to; backups are overwritten as the service is restarted.</p>
      </section>
      <section>
        <h2>Your choices</h2>
        <p>Ask us to see, correct or delete your account information: <Contact />. LoanDesk is not meant for anyone
          under 18.</p>
      </section>
      <section>
        <h2>Changes</h2>
        <p>We may update this policy; the date above says when.</p>
      </section>
    </Page>
  );
}
