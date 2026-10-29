export const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8100";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body: unknown,
  ) {
    super(message);
  }
}

function trader(): string {
  try {
    return localStorage.getItem("loandesk.trader") || "desk";
  } catch {
    return "desk";
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(API_URL + path, {
    method,
    headers: { "content-type": "application/json", "x-trader": trader() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  const data = text ? JSON.parse(text) : null;
  if (!r.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : d?.error ?? r.statusText;
    throw new ApiError(r.status, msg, d);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
};

export const qs = (params: Record<string, string | number | undefined | null>) => {
  const p = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return p.length ? "?" + new URLSearchParams(p.map(([k, v]) => [k, String(v)])).toString() : "";
};
