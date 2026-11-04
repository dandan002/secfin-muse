import { useEffect } from "react";
import { API_URL } from "./api";
import { sessionToken } from "./auth";
import { useDesk } from "./store";

type Listener = (data: any) => void;  // eslint-disable-line @typescript-eslint/no-explicit-any
const listeners = new Map<string, Set<Listener>>();

/** Hear each push on ``topic`` as it arrives, payload and all: for views that fold changes into what they hold
 *  instead of refetching. ``"open"`` fires on every (re)connect, when anything missed must be read afresh. */
export function onPush(topic: string, fn: Listener): () => void {
  const set = listeners.get(topic) ?? new Set();
  listeners.set(topic, set.add(fn));
  return () => set.delete(fn);
}

const emit = (topic: string, data: unknown) => listeners.get(topic)?.forEach((fn) => fn(data));

/** One WebSocket to the desk's /stream; pushes bump topic versions so views refetch, and go to any listeners.
 *  Reconnects with backoff. */
export function useStream() {
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let closed = false;
    const { setClock, setConnected, bump } = useDesk.getState();
    // The API closes a socket whose sign-in has lapsed: renew it as Clerk refreshes the session token.
    const renew = setInterval(async () => {
      const token = await sessionToken();
      if (token && ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "auth", token }));
    }, 20_000);

    const connect = async () => {
      const token = await sessionToken();
      if (closed) return;
      ws = new WebSocket(API_URL.replace(/^http/, "ws") + "/stream" + (token ? `?token=${encodeURIComponent(token)}` : ""));
      ws.onopen = () => {
        retry = 0;
        setConnected(true);
        ["locate", "order", "trade", "break", "action", "market", "agent_run", "chat", "short_list"].forEach(bump);
        emit("open", null);
      };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.topic === "clock") setClock(msg.data);
        else {
          bump(msg.topic);
          emit(msg.topic, msg.data);
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) timer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
      };
    };
    connect();
    return () => {
      closed = true;
      clearInterval(renew);
      clearTimeout(timer);
      ws?.close();
    };
  }, []);
}
