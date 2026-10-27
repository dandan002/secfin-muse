import type { Chip, User } from "../lib/api";
import { useStore } from "../lib/store";

type Props = {
  text: string;
  chips: Chip[];
  members: User[];
  meUuid?: string;
  onChip: (chip: Chip, el: HTMLElement) => void;
};

/** Renders message text with clickable instrument chips and highlighted @mentions. */
export function MessageText({ text, chips, members, meUuid, onChip }: Props) {
  const term = useStore((s) => s.theme === "terminal");
  const valid = chips
    .filter((c) => c.start >= 0 && c.end > c.start && c.end <= text.length)
    .sort((a, b) => a.start - b.start);
  const parts: JSX.Element[] = [];
  let pos = 0;
  valid.forEach((c, i) => {
    if (c.start < pos) return;
    if (c.start > pos) parts.push(<Mentions key={`t${i}`} text={text.slice(pos, c.start)} members={members} meUuid={meUuid} />);
    parts.push(
      <button
        key={`c${i}`}
        onClick={(e) => onChip(c, e.currentTarget)}
        className={
          term
            ? "bg-accent px-0.5 font-mono font-semibold text-black hover:bg-mention"
            : "mx-0.5 inline-flex items-baseline gap-1 rounded border border-accent/40 bg-accent/10 px-1 font-mono text-[12px] text-accent hover:bg-accent/20"
        }
        title={`${c.composite} · ${c.isin}`}
      >
        {text.slice(c.start, c.end)}
      </button>,
    );
    pos = c.end;
  });
  if (pos < text.length) parts.push(<Mentions key="tail" text={text.slice(pos)} members={members} meUuid={meUuid} />);
  return <div className={`whitespace-pre-wrap break-words leading-5 ${term ? "inline" : ""}`}>{parts}</div>;
}

function Mentions({ text, members, meUuid }: { text: string; members: User[]; meUuid?: string }) {
  const bits = text.split(/(@[a-z0-9._-]+)/gi);
  return (
    <>
      {bits.map((b, i) => {
        if (!b.startsWith("@")) return <span key={i}>{b}</span>;
        const u = members.find((m) => m.handle.toLowerCase() === b.slice(1).toLowerCase());
        if (!u) return <span key={i}>{b}</span>;
        const me = u.uuid === meUuid;
        return (
          <span
            key={i}
            title={`${u.displayName} · ${u.firm}`}
            className={`rounded px-0.5 font-medium ${me ? "bg-mention/25 text-mention" : "text-mention"}`}
          >
            {b}
          </span>
        );
      })}
    </>
  );
}
