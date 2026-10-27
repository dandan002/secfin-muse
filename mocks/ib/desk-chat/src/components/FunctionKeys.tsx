import { useEffect } from "react";
import { useStore } from "../lib/store";
import { COMMANDS } from "./CommandBar";

type Key = { key: string; label: string; run: () => void };

/** Bottom function-key strip with real key bindings, plus the F1 help overlay. */
export function FunctionKeys() {
  const { set, cycleTheme, helpOpen } = useStore();

  const keys: Key[] = [
    { key: "F1", label: "HELP", run: () => set({ helpOpen: !useStore.getState().helpOpen }) },
    { key: "F2", label: "CHATS", run: () => document.getElementById("room-filter")?.focus() },
    { key: "F3", label: "CMD", run: () => document.getElementById("command-line")?.focus() },
    { key: "F8", label: "BLAST", run: () => set({ blastOpen: true }) },
    { key: "F9", label: "SRCH", run: () => set({ searchOpen: !useStore.getState().searchOpen, searchSeed: "" }) },
    { key: "F10", label: "THEME", run: cycleTheme },
    { key: "ESC", label: "CANCEL", run: () => set({ helpOpen: false, blastOpen: false, searchOpen: false }) },
  ];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = e.key === "Escape" ? "ESC" : e.key;
      const hit = keys.find((x) => x.key === k);
      const st = useStore.getState();
      if (hit && (k !== "ESC" || st.helpOpen || st.blastOpen || st.searchOpen)) {
        e.preventDefault();
        hit.run();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <>
      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-t border-line bg-black px-2 py-1 text-[11px]">
        {keys.map((k) => (
          <button key={k.key} onClick={k.run} className="flex items-center gap-1 text-ink hover:text-accent">
            <span className="fkey">{k.key}</span>
            {k.label}
          </button>
        ))}
      </div>
      {helpOpen && (
        <div className="fixed inset-0 z-40 grid place-items-center bg-black/70 p-4" onClick={() => set({ helpOpen: false })}>
          <div onClick={(e) => e.stopPropagation()} className="w-full max-w-xl border border-accent bg-black font-mono text-[12px]">
            <div className="bg-accent px-2 py-0.5 font-semibold text-black">HELP · COMMANDS AND KEYS</div>
            <table className="m-3 text-left">
              <tbody>
                {COMMANDS.map(([c, d]) => (
                  <tr key={c}>
                    <td className="pr-4 text-accent">{c} &lt;GO&gt;</td>
                    <td className="text-ink">{d}</td>
                  </tr>
                ))}
                {keys.map((k) => (
                  <tr key={k.key}>
                    <td className="pr-4 pt-1">
                      <span className="fkey">{k.key}</span>
                    </td>
                    <td className="pt-1 text-ink">{k.label}</td>
                  </tr>
                ))}
                <tr>
                  <td className="pr-4 pt-1 text-accent">CTRL+F / CTRL+B</td>
                  <td className="pt-1 text-ink">search / blast</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
