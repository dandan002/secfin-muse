import { useEffect, useSyncExternalStore } from "react";

/** Three pages: the landing at ``/``, sign-in at ``/sign-in`` (Clerk adds sub-steps under it), the desk at ``/desk``.
 *  A tiny history router; Clerk navigates through ``navigate`` too, so sign-in steps never reload the page. */
export const ROUTES = { landing: "/", signIn: "/sign-in", desk: "/desk" } as const;

const EVENT = "loandesk:navigate";

export function navigate(to: string, opts: { replace?: boolean } = {}) {
  const url = new URL(to, window.location.href);
  if (url.origin !== window.location.origin) {
    window.location.assign(url.href);
    return;
  }
  const next = url.pathname + url.search + url.hash;
  if (next === window.location.pathname + window.location.search + window.location.hash) return;
  window.history[opts.replace ? "replaceState" : "pushState"](null, "", next);
  window.dispatchEvent(new Event(EVENT));
}

function subscribe(cb: () => void) {
  window.addEventListener("popstate", cb);
  window.addEventListener(EVENT, cb);
  return () => {
    window.removeEventListener("popstate", cb);
    window.removeEventListener(EVENT, cb);
  };
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, () => window.location.pathname);
}

/** ``/sign-in`` and everything Clerk mounts beneath it (``/sign-in/factor-one``, ``/sign-in/sso-callback``, …). */
export const isUnder = (path: string, base: string) => path === base || path.startsWith(base + "/");

/** Render-time redirect (replace, so Back doesn't bounce into it again). */
export function Redirect({ to }: { to: string }) {
  useEffect(() => navigate(to, { replace: true }), [to]);
  return null;
}
