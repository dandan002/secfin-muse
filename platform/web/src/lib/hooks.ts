import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useDesk } from "./store";

/** GET ``path``; refetch whenever any of ``topics`` gets a stream push (throttled) or ``path`` changes. */
export function useQuery<T>(path: string | null, topics: string[] = [], pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const versions = useDesk((s) => topics.map((t) => s.versions[t] ?? 0).join(","));
  const inflight = useRef(false);
  const again = useRef(false);

  const load = useCallback(async () => {
    if (!path) return;
    if (inflight.current) {
      again.current = true;
      return;
    }
    inflight.current = true;
    setLoading(true);
    try {
      setData(await api.get<T>(path));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      inflight.current = false;
      setLoading(false);
      if (again.current) {
        again.current = false;
        setTimeout(load, 150);
      }
    }
  }, [path]);

  useEffect(() => {
    load();
  }, [load, versions]);

  useEffect(() => {
    if (!pollMs) return;
    const t = setInterval(load, pollMs);
    return () => clearInterval(t);
  }, [load, pollMs]);

  return { data, error, loading, reload: load, setData };
}

/** Global single-key shortcuts, ignored while typing in a field. */
export function useKeys(handler: (e: KeyboardEvent) => void, deps: unknown[]) {
  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) {
        return;
      }
      const d = useDesk.getState();
      if (d.paletteOpen || d.bulkBorrow) return;  // an overlay is up: the screen behind it keeps still
      handler(e);
    };
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  }, deps);
}

/** Arrow / j / k selection over a list. */
export function useSelection(count: number) {
  const [sel, setSel] = useState(0);
  useEffect(() => {
    if (sel >= count) setSel(Math.max(count - 1, 0));
  }, [count, sel]);
  useKeys(
    (e) => {
      if (e.key === "ArrowDown" || e.key === "j") {
        setSel((s) => Math.min(s + 1, count - 1));
        e.preventDefault();
      } else if (e.key === "ArrowUp" || e.key === "k") {
        setSel((s) => Math.max(s - 1, 0));
        e.preventDefault();
      }
    },
    [count],
  );
  return [sel, setSel] as const;
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** Ticked rows for bulk work: click a box to tick, shift-click to tick the run since the last, Space ticks the
 *  row in focus, Esc clears. Ticks of rows that leave the list are dropped. */
export function useMulti(ids: string[], focused: string | undefined) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const anchor = useRef<string | null>(null);
  const key = ids.join(",");
  useEffect(() => {
    setPicked((p) => {
      const keep = new Set([...p].filter((id) => ids.includes(id)));
      return keep.size === p.size ? p : keep;
    });
  }, [key]);
  const toggle = (id: string, range = false) => {
    setPicked((p) => {
      const next = new Set(p);
      if (range && anchor.current && ids.includes(anchor.current)) {
        const [a, b] = [ids.indexOf(anchor.current), ids.indexOf(id)].sort((x, y) => x - y);
        const on = !p.has(id);
        for (const x of ids.slice(a, b + 1)) (on ? next.add(x) : next.delete(x));
      } else if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    anchor.current = id;
  };
  const all = ids.length > 0 && ids.every((id) => picked.has(id));
  const setAll = (on: boolean) => setPicked(on ? new Set(ids) : new Set());
  useKeys((e) => {
    if (e.key === " " && focused && !e.shiftKey) toggle(focused);
    else if (e.key === "Escape" && picked.size) setPicked(new Set());
    else return;
    e.preventDefault();
  }, [focused, picked, key]);
  return { picked, toggle, all, setAll, clear: () => setPicked(new Set()) };
}
