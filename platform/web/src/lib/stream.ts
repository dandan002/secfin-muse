import { useEffect } from "react";
import { API_URL } from "./api";
import { useDesk } from "./store";

/** One WebSocket to the desk's /stream; pushes bump topic versions so views refetch. Reconnects with backoff. */
export function useStream() {
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let closed = false;
    const { setClock, setConnected, bump } = useDesk.getState();

    const connect = () => {
      ws = new WebSocket(API_URL.replace(/^http/, "ws") + "/stream");
      ws.onopen = () => {
        retry = 0;
        setConnected(true);
        ["locate", "order", "trade", "break", "action", "market"].forEach(bump);
      };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.topic === "clock") setClock(msg.data);
        else bump(msg.topic);
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) timer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, []);
}
