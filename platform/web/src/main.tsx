import { ClerkProvider, useAuth } from "@clerk/react";
import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { CLERK_KEY, setTokenGetter } from "./lib/auth";
import { isUnder, navigate, Redirect, ROUTES, usePath } from "./lib/router";
import { Landing } from "./views/Landing";
import { SignInPage } from "./views/SignInPage";
import "./index.css";

/** The desk, once every API call can carry the session token (set before the desk's first fetch). */
function SignedInDesk() {
  const { getToken } = useAuth();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    setTokenGetter(() => getToken());
    setReady(true);
    return () => setTokenGetter(null);
  }, [getToken]);
  return ready ? <App /> : null;
}

/** ``/`` landing, ``/sign-in`` Clerk, ``/desk`` only with a live session. Nothing routes until Clerk has loaded, so
 *  a signed-in trader isn't bounced to sign-in on a cold load, and a signed-out visitor never sees the desk mount. */
function Routes() {
  const path = usePath();
  const { isLoaded, isSignedIn } = useAuth();
  if (!isLoaded) return null;
  if (isUnder(path, ROUTES.desk)) return isSignedIn ? <SignedInDesk /> : <Redirect to={ROUTES.signIn} />;
  if (isUnder(path, ROUTES.signIn)) return isSignedIn ? <Redirect to={ROUTES.desk} /> : <SignInPage />;
  if (path === ROUTES.landing) return <Landing signedIn={isSignedIn} />;
  return <Redirect to={ROUTES.landing} />;
}

/** No Clerk key: the landing page; local dev (``npm run dev``) can still open the desk (the API is open then too).
 *  A production build without a key never mounts the desk. */
function Unconfigured() {
  const path = usePath();
  const dev = import.meta.env.DEV;
  if (dev && isUnder(path, ROUTES.desk)) return <App />;
  if (path !== ROUTES.landing) return <Redirect to={ROUTES.landing} />;
  return <Landing onDevEnter={dev ? () => navigate(ROUTES.desk) : undefined} />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {CLERK_KEY ? (
      <ClerkProvider
        publishableKey={CLERK_KEY}
        signInUrl={ROUTES.signIn}
        signInForceRedirectUrl={ROUTES.desk}
        afterSignOutUrl={ROUTES.landing}
        routerPush={(to) => navigate(to)}
        routerReplace={(to) => navigate(to, { replace: true })}
      >
        <Routes />
      </ClerkProvider>
    ) : <Unconfigured />}
  </React.StrictMode>,
);
