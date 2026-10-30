import { ClerkProvider, Show, useAuth } from "@clerk/react";
import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { CLERK_KEY, setTokenGetter } from "./lib/auth";
import { Landing } from "./views/Landing";
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

/** No Clerk key: the landing page; local dev can still open the desk (the API is open then too). */
function Unconfigured() {
  const [dev, setDev] = useState(false);
  if (dev) return <App />;
  return <Landing onDevEnter={import.meta.env.DEV ? () => setDev(true) : undefined} />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {CLERK_KEY ? (
      <ClerkProvider publishableKey={CLERK_KEY} afterSignOutUrl="/">
        <Show when="signed-out"><Landing /></Show>
        <Show when="signed-in"><SignedInDesk /></Show>
      </ClerkProvider>
    ) : <Unconfigured />}
  </React.StrictMode>,
);
