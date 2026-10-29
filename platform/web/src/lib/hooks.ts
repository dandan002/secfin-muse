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
      if (useDesk.getState().paletteOpen) return;
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
