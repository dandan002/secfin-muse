import { SignIn } from "@clerk/react";
import { navigate, ROUTES } from "../lib/router";

/** Clerk's sign-in, styled to the desk (dark graphite, sage accent). Sign-up is off: traders are invited. */
const appearance = {
  variables: {
    colorPrimary: "rgb(150 202 186)",
    colorPrimaryForeground: "rgb(14 15 18)",
    colorBackground: "rgb(19 20 24)",
    colorInput: "rgb(27 29 34)",
    colorInputForeground: "rgb(221 223 228)",
    colorForeground: "rgb(221 223 228)",
    colorMutedForeground: "rgb(139 144 155)",
    colorNeutral: "rgb(221 223 228)",
    colorDanger: "rgb(228 136 126)",
    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
    fontSize: "13px",
    borderRadius: "10px",
  },
  elements: { cardBox: "shadow-none border border-line/60", footer: "bg-panel" },
};

export function SignInPage() {
  return (
    <div className="h-full overflow-auto">
      <div className="min-h-full max-w-md mx-auto px-4 flex flex-col">
        <header className="h-14 flex items-center justify-between">
          <a
            href={ROUTES.landing}
            onClick={(e) => {
              e.preventDefault();
              navigate(ROUTES.landing);
            }}
            className="flex items-baseline gap-2"
          >
            <span className="text-[16px] font-semibold tracking-[-0.02em]">LoanDesk</span>
            <span className="text-2xs text-faint">sec lending desk</span>
          </a>
        </header>

        <main className="flex-1 flex flex-col justify-center py-10">
          <div className="label mb-2">Desk access</div>
          <p className="text-xs text-dim mb-6 leading-relaxed">
            Sign in with the account your desk invited. Every action you take is recorded under your name.
          </p>
          <SignIn
            routing="path"
            path={ROUTES.signIn}
            forceRedirectUrl={ROUTES.desk}
            withSignUp={false}
            appearance={appearance}
          />
        </main>

        <footer className="py-6 text-2xs text-faint">All data is synthetic.</footer>
      </div>
    </div>
  );
}
