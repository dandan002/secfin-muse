import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Room } from "../lib/api";
import { useStore } from "../lib/store";

/** Message box with @mention autocomplete, typing pings, and drafts pushed by the agent (bot /compose). */
export function Composer({ room }: { room: Room }) {
  const me = useStore((s) => s.me);
  const draft = useStore((s) => s.composeDraft);
  const ingest = useStore((s) => s.ingest);
  const [text, setText] = useState("");
  const [fromAgent, setFromAgent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mentionAt, setMentionAt] = useState<number | null>(null);
  const [sel, setSel] = useState(0);
  const lastPing = useRef(0);
  const box = useRef<HTMLTextAreaElement>(null);

  // Per-room unsent drafts survive tab switches.
  const drafts = useRef<Record<string, string>>({});
  useEffect(() => {
    setText(drafts.current[room.id] ?? "");
    setFromAgent(false);
    box.current?.focus();
  }, [room.id]);

  useEffect(() => {
    if (draft && draft.roomId === room.id) {
      setText(draft.text);
      setFromAgent(true);
      box.current?.focus();
    }
  }, [draft, room.id]);

  const query = mentionAt !== null ? text.slice(mentionAt + 1).split(/\s/)[0].toLowerCase() : "";
  const candidates = useMemo(
    () =>
      mentionAt === null
        ? []
        : room.members
            .filter((m) => m.handle !== me)
            .filter((m) => m.handle.toLowerCase().includes(query) || m.displayName.toLowerCase().includes(query))
            .slice(0, 6),
    [mentionAt, query, room.members, me],
  );

  const update = (v: string, caret: number) => {
    setText(v);
    drafts.current[room.id] = v;
    const before = v.slice(0, caret);
    const m = before.match(/(^|\s)@([a-z0-9._-]*)$/i);
    setMentionAt(m ? caret - m[2].length - 1 : null);
    setSel(0);
    const now = Date.now();
    if (v && now - lastPing.current > 2500) {
      lastPing.current = now;
      void api.typing(me, room.id).catch(() => undefined);
    }
  };

  const pick = (handle: string) => {
    if (mentionAt === null) return;
    const rest = text.slice(mentionAt + 1 + query.length);
    const v = `${text.slice(0, mentionAt)}@${handle} ${rest.replace(/^\s*/, "")}`;
    setText(v);
    drafts.current[room.id] = v;
    setMentionAt(null);
    box.current?.focus();
  };

  const send = async () => {
    const body = text.trim();
    if (!body) return;
    setError(null);
    try {
      const m = await api.send(me, room.id, body, `ui-${me}-${Date.now()}`);
      ingest(m);
      setText("");
      drafts.current[room.id] = "";
      setFromAgent(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="relative border-t border-line bg-panel px-4 py-3">
      {fromAgent && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-accent/40 bg-accent/10 px-2 py-1 text-[12px] text-accent">
          Draft from LoanDesk Agent — review before sending
          <span className="flex-1" />
          <button
            onClick={() => {
              setText("");
              setFromAgent(false);
            }}
            className="text-muted hover:text-ink"
          >
            Discard
          </button>
        </div>
      )}
      {candidates.length > 0 && (
        <ul className="absolute bottom-full left-4 mb-1 w-72 overflow-hidden rounded-md border border-line bg-panel shadow-xl">
          {candidates.map((c, i) => (
            <li key={c.uuid}>
              <button
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(c.handle);
                }}
                className={`flex w-full gap-2 px-2 py-1.5 text-left ${i === sel ? "bg-raised" : ""}`}
              >
                <span className="font-medium">@{c.handle}</span>
                <span className="truncate text-muted">
                  {c.displayName} · {c.firm}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-end gap-2">
        <textarea
          ref={box}
          value={text}
          rows={Math.min(6, Math.max(1, text.split("\n").length))}
          onChange={(e) => update(e.target.value, e.target.selectionStart)}
          onKeyDown={(e) => {
            if (candidates.length) {
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                setSel((s) => (s + (e.key === "ArrowDown" ? 1 : candidates.length - 1)) % candidates.length);
                return;
              }
              if (e.key === "Enter" || e.key === "Tab") {
                e.preventDefault();
                pick(candidates[sel].handle);
                return;
              }
              if (e.key === "Escape") {
                setMentionAt(null);
                return;
              }
            }
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder={`Message ${room.name}  ·  Enter to send, Shift+Enter for a new line, @ to mention`}
          className="scroll-thin min-h-[38px] flex-1 resize-none rounded-md border border-line bg-raised px-3 py-2 outline-none placeholder:text-muted focus:border-accent"
        />
        <button
          onClick={() => void send()}
          disabled={!text.trim()}
          className="h-[38px] rounded-md bg-accent px-4 font-medium text-bg disabled:opacity-40"
        >
          Send
        </button>
      </div>
      {error && <p className="mt-1 text-[12px] text-hot">{error}</p>}
    </div>
  );
}
