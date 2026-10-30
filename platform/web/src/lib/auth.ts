/** Sign-in through Clerk. With a publishable key the desk needs a Clerk session; every API call and the stream
 *  carry its token, and the API verifies it (LOANDESK_CLERK_ISSUER). Without a key, local dev only. */
export const CLERK_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined;
export const authEnabled = !!CLERK_KEY;

let getter: (() => Promise<string | null>) | null = null;

export function setTokenGetter(fn: (() => Promise<string | null>) | null) {
  getter = fn;
}

/** A fresh session token (Clerk caches and refreshes it), or null with sign-in off. */
export async function sessionToken(): Promise<string | null> {
  return getter ? getter() : null;
}
