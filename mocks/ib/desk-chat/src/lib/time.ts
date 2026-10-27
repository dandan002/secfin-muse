const TZ_LABEL: Record<string, string> = {
  "America/New_York": "NY",
  "Europe/London": "LDN",
  "Asia/Tokyo": "TYO",
  "Europe/Frankfurt": "FRA",
};

export function tzLabel(tz: string): string {
  return TZ_LABEL[tz] ?? tz.split("/").pop() ?? tz;
}

/** Exchange-local time for a room, e.g. "09:31:07". */
export function localTime(iso: string, tz: string, seconds = true): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    hour: "2-digit",
    minute: "2-digit",
    second: seconds ? "2-digit" : undefined,
    hour12: false,
  }).format(new Date(iso));
}

export function localDay(iso: string, tz: string): string {
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "short", day: "2-digit", month: "short" }).format(
    new Date(iso),
  );
}

export function fmtQty(q: number): string {
  if (q >= 1_000_000) return `${+(q / 1_000_000).toFixed(2)}m`;
  if (q >= 1_000) return `${+(q / 1_000).toFixed(1)}k`;
  return String(q);
}
