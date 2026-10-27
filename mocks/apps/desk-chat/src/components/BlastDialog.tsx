import { useState } from "react";
import { api } from "../lib/api";
import { useStore } from "../lib/store";

/** Send one message to many rooms at once. */
export function BlastDialog() {
  const { rooms, me, set, ingest } = useStore();
  const [picked, setPicked] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const close = () => set({ blastOpen: false });

  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  const send = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await api.blast(me, picked, text.trim());
      out.messages.forEach(ingest);
      close();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/50 p-4" onClick={close}>
      <div
        role="dialog"
        aria-label="Blast"
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-lg rounded-lg border border-line bg-panel p-4 shadow-2xl"
      >
        <h2 className="text-[15px] font-semibold">Blast</h2>
        <p className="mb-3 text-[12px] text-muted">Send the same message to several rooms.</p>
        <div className="scroll-thin mb-3 grid max-h-56 grid-cols-2 gap-1 overflow-y-auto">
          {rooms.map((r) => (
            <label key={r.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-raised">
              <input type="checkbox" checked={picked.includes(r.id)} onChange={() => toggle(r.id)} />
              <span className="truncate">{r.name}</span>
            </label>
          ))}
        </div>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={4}
          placeholder="e.g. GC rates unchanged today; specials list attached on request"
          className="w-full rounded-md border border-line bg-raised px-3 py-2 outline-none focus:border-accent"
        />
        {error && <p className="mt-1 text-[12px] text-hot">{error}</p>}
        <div className="mt-3 flex justify-end gap-2">
          <button onClick={close} className="rounded-md px-3 py-1.5 text-muted hover:text-ink">
            Cancel
          </button>
          <button
            onClick={() => void send()}
            disabled={busy || !picked.length || !text.trim()}
            className="rounded-md bg-accent px-3 py-1.5 font-medium text-bg disabled:opacity-40"
          >
            Send to {picked.length || "…"} room{picked.length === 1 ? "" : "s"}
          </button>
        </div>
      </div>
    </div>
  );
}
