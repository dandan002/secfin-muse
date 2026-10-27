import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Chip, Message, Room } from "../lib/api";
import { useStore } from "../lib/store";
import { localDay, localTime, tzLabel } from "../lib/time";
import { ChipCard } from "./ChipCard";
import { Composer } from "./Composer";
import { MessageText } from "./MessageText";

const EMPTY: Message[] = [];
const FIRM_HUES = [172, 262, 32, 200, 330, 96, 12, 230];

function hue(firm: string): number {
  let h = 0;
  for (const ch of firm) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return FIRM_HUES[h % FIRM_HUES.length];
}

function initials(name: string): string {
  return name
    .replace(/^Internal:\s*/, "")
    .split(/\s+/)
    .slice(0, 2)
    .map((p) => p[0])
    .join("")
    .toUpperCase();
}

export function RoomView({ room, highlight }: { room: Room; highlight: number | null }) {
  const messages = useStore((s) => s.messages[room.id]) ?? EMPTY;
  const hasMore = useStore((s) => s.hasMore[room.id]);
  const loadOlder = useStore((s) => s.loadOlder);
  const typing = useStore((s) => s.typing[room.id]);
  const users = useStore((s) => s.users);
  const me = useStore((s) => s.me);
  const markRead = useStore((s) => s.markRead);
  const meUuid = users.find((u) => u.handle === me)?.uuid;
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const prevHeight = useRef<number | null>(null);
  const [chip, setChip] = useState<{ chip: Chip; rect: DOMRect } | null>(null);
  const [, force] = useState(0);

  // Keep the view pinned to the bottom for new messages; preserve position when older ones load.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (prevHeight.current !== null) {
      el.scrollTop = el.scrollHeight - prevHeight.current;
      prevHeight.current = null;
    } else if (stickToBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    stickToBottom.current = true;
    markRead(room.id);
  }, [room.id, markRead]);

  useEffect(() => {
    if (highlight === null) return;
    document.getElementById(`msg-${highlight}`)?.scrollIntoView({ block: "center" });
  }, [highlight, messages]);

  // Expire typing indicators.
  useEffect(() => {
    const id = window.setInterval(() => force((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (el.scrollTop < 60 && hasMore) {
      prevHeight.current = el.scrollHeight;
      void loadOlder(room.id);
    }
  };

  const now = Date.now();
  const typers = Object.entries(typing ?? {})
    .filter(([, exp]) => exp > now)
    .map(([uid]) => users.find((u) => u.uuid === uid)?.displayName)
    .filter(Boolean);

  let lastDay = "";
  let lastSender = "";
  let lastTs = 0;

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-line px-4 py-2.5">
        <div className="min-w-0">
          <h2 className="truncate text-[14px] font-semibold">{room.name}</h2>
          <p className="truncate text-[12px] text-muted">
            {room.topic || room.members.map((m) => m.displayName).join(", ")}
          </p>
        </div>
        <div className="flex-1" />
        <div className="flex -space-x-1.5">
          {room.members.map((m) => (
            <span
              key={m.uuid}
              title={`${m.displayName} · ${m.firm}${m.kind === "bot" ? " (bot)" : ""}`}
              className="grid h-6 w-6 place-items-center rounded-full border-2 border-panel text-[9px] font-semibold text-white"
              style={{ background: `hsl(${hue(m.firm)} 45% 42%)` }}
            >
              {m.kind === "bot" ? "AI" : initials(m.displayName)}
            </span>
          ))}
        </div>
        <span className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted" title={room.tz}>
          {tzLabel(room.tz)} time
        </span>
      </header>

      <div ref={scroller} onScroll={onScroll} className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {hasMore && (
          <button
            onClick={() => {
              prevHeight.current = scroller.current?.scrollHeight ?? null;
              void loadOlder(room.id);
            }}
            className="mx-auto mb-3 block rounded-full border border-line px-3 py-1 text-[12px] text-muted hover:text-ink"
          >
            Load earlier messages
          </button>
        )}
        {messages.map((m) => {
          const day = localDay(m.ts, room.tz);
          const t = Date.parse(m.ts);
          const grouped = m.sender.uuid === lastSender && t - lastTs < 5 * 60_000 && day === lastDay;
          const dayBreak = day !== lastDay;
          lastDay = day;
          lastSender = m.sender.uuid;
          lastTs = t;
          const mentionsMe = !!meUuid && m.mentions.includes(meUuid);
          return (
            <div key={m.messageId}>
              {dayBreak && (
                <div className="my-3 flex items-center gap-3 text-[11px] text-muted">
                  <span className="h-px flex-1 bg-line" />
                  {day}
                  <span className="h-px flex-1 bg-line" />
                </div>
              )}
              <article
                id={`msg-${m.messageId}`}
                className={`group flex gap-3 rounded-md px-2 ${grouped ? "py-0.5" : "mt-2 py-1"} ${
                  highlight === m.messageId ? "flash" : ""
                } ${mentionsMe ? "border-l-2 border-mention bg-mention/5" : "hover:bg-raised/40"}`}
              >
                <div className="w-8 shrink-0">
                  {!grouped && (
                    <span
                      className="grid h-8 w-8 place-items-center rounded-md text-[11px] font-semibold text-white"
                      style={{ background: `hsl(${hue(m.sender.firm)} 45% 42%)` }}
                    >
                      {m.sender.kind === "bot" ? "AI" : initials(m.sender.displayName)}
                    </span>
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  {!grouped && (
                    <div className="flex items-baseline gap-2">
                      <span className="font-semibold">{m.sender.displayName}</span>
                      <span className="text-[12px] text-muted">{m.sender.firm}</span>
                      {m.via === "bot" && (
                        <span className="rounded bg-accent/15 px-1 text-[10px] font-medium text-accent">AGENT</span>
                      )}
                      {m.blastId && (
                        <span className="rounded bg-raised px-1 text-[10px] text-muted" title={m.blastId}>
                          BLAST
                        </span>
                      )}
                      <span className="num text-[11px] text-muted">{localTime(m.ts, room.tz)}</span>
                    </div>
                  )}
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <MessageText
                        text={m.text}
                        chips={m.chips}
                        members={room.members}
                        meUuid={meUuid}
                        onChip={(c, el) => setChip({ chip: c, rect: el.getBoundingClientRect() })}
                      />
                    </div>
                    {grouped && (
                      <span className="num invisible shrink-0 text-[11px] text-muted group-hover:visible">
                        {localTime(m.ts, room.tz)}
                      </span>
                    )}
                  </div>
                </div>
              </article>
            </div>
          );
        })}
      </div>

      <div className="h-5 px-6 text-[12px] italic text-muted">
        {typers.length > 0 && `${typers.join(", ")} ${typers.length > 1 ? "are" : "is"} typing…`}
      </div>
      <Composer room={room} />
      {chip && <ChipCard chip={chip.chip} anchor={chip.rect} onClose={() => setChip(null)} />}
    </section>
  );
}
