import { useEffect } from "react";
import { api, wsUrl, type Message } from "./api";
import { useStore } from "./store";

type Envelope =
  | { type: "message"; data: Message }
  | { type: "typing"; data: { streamId: string; user: string } }
  | { type: "compose"; data: { streamId: string; text: string } }
  | { type: "read"; data: { streamId: string; seq: number } }
  | { type: "room_created"; data: unknown }
  | { type: "snapshot_end" | "error"; data?: unknown };

/** Keeps one WebSocket open for the current user, reconnecting with backoff. */
export function useLiveConnection(): void {
  const me = useStore((s) => s.me);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 0;
    let timer: number | undefined;

    const connect = () => {
      ws = new WebSocket(wsUrl(me));
      ws.onopen = () => {
        retry = 0;
        useStore.getState().set({ connected: true });
      };
      ws.onmessage = (ev) => {
        const env = JSON.parse(ev.data) as Envelope;
        const s = useStore.getState();
        switch (env.type) {
          case "message":
            s.ingest(env.data);
            break;
          case "typing": {
            const u = s.users.find((x) => x.uuid === env.data.user);
            if (u && u.handle !== s.me) s.setTyping(env.data.streamId, env.data.user);
            break;
          }
          case "compose":
            s.setCompose(env.data.streamId, env.data.text);
            s.openRoom(env.data.streamId);
            break;
          case "room_created":
            void s.loadRooms();
            break;
        }
      };
      ws.onclose = () => {
        useStore.getState().set({ connected: false });
        if (closed) return;
        retry = Math.min(retry + 1, 6);
        timer = window.setTimeout(() => {
          void useStore.getState().loadRooms(); // catch up on anything missed
          connect();
        }, 250 * 2 ** retry);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [me]);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const h = await api.health();
        if (!stop) useStore.getState().set({ simTime: h.simTime });
      } catch {
        /* offline; the banner shows it */
      }
    };
    void tick();
    const id = window.setInterval(tick, 1000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, []);
}
