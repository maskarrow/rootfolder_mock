import { SESSION_COOKIE, loginHref } from "@/lib/session";

/** Where the API is, seen from where the code runs.
 *
 *  **In the browser: `/api`, same origin as the pages.** The session cookie is bound
 *  to the page's domain, so it goes along with every request, without CORS. Locally
 *  the rewrite in `next.config.ts` sends `/api/...` to the backend; in production
 *  nginx does. Both strip the prefix.
 *
 *  **On the server (Server Components): straight to the backend**, at
 *  `API_INTERNAL_URL`, read at run time so one build serves every environment.
 *  `serverHeaders` adds the cookie. */
export function getBaseUrl(): string {
  if (typeof window !== "undefined") return "/api";
  return process.env.API_INTERNAL_URL ?? "http://localhost:8010";
}

/** The user's session cookie on the server → backend hop; without it the backend
 *  sees an anonymous request. Only our cookie is forwarded. `next/headers` loads
 *  dynamically because this module is also imported by client components. */
async function serverHeaders(): Promise<Record<string, string>> {
  if (typeof window !== "undefined") return {};
  const { cookies } = await import("next/headers");
  const session = (await cookies()).get(SESSION_COOKIE);
  return session ? { cookie: `${SESSION_COOKIE}=${session.value}` } : {};
}

/** The session died while the page was open: the browser goes to login and comes
 *  back afterwards. Nothing on the server, where `proxy.ts` already checked. */
export function redirectIfSessionDied(status: number): void {
  if (status !== 401 || typeof window === "undefined") return;
  window.location.assign(loginHref(window.location.pathname + window.location.search));
}

/** An API error that keeps the HTTP status. Status 0 = no answer at all. */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** FastAPI's `{ "detail": "..." }`. Anything else did not come from the backend (a
 *  proxy's HTML error page, say), which is worth saying in a deploy test. */
function errorDetail(status: number, body: string): string {
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed?.detail === "string") return `${status}: ${parsed.detail}`;
  } catch {
    // not JSON
  }
  const excerpt = body.replace(/\s+/g, " ").trim().slice(0, 200);
  return `${status}: the response was not JSON, probably from a proxy: ${excerpt || "(empty)"}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const baseUrl = getBaseUrl();
  let res: Response;
  try {
    // Live backend data: never cached.
    res = await fetch(`${baseUrl}${path}`, {
      cache: "no-store",
      ...init,
      headers: { ...(await serverHeaders()), ...init?.headers },
    });
  } catch {
    throw new ApiError(0, `Cannot reach the backend (${baseUrl}).`);
  }

  if (!res.ok) {
    // Login itself answers 401 to a wrong password: that is the form's error, not a
    // dead session.
    if (path !== "/auth/login") redirectIfSessionDied(res.status);
    throw new ApiError(res.status, errorDetail(res.status, await res.text()));
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function postJSON<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export type Me = {
  id: string;
  email: string;
  name: string;
  role: "admin" | "member";
  org_id: string;
  org_name: string;
  seen_ip: string | null;
  scheme: string;
};

export type Health = { status: string; version: string; env: string };

export type Item = {
  id: string;
  title: string;
  body: string;
  pinned: boolean;
  created_at: string;
};

export type StoredFile = {
  id: string;
  filename: string;
  size_bytes: number;
  status: "processing" | "done" | "interrupted";
  created_at: string;
  finished_at: string | null;
};

export type CheckResult = { ok: boolean; detail: string };

export const login = (email: string, password: string, remember: boolean) =>
  postJSON<Me>("/auth/login", { email, password, remember });

export const logout = () => postJSON<void>("/auth/logout");

export const getMe = () => request<Me>("/auth/me");

export const getHealth = () => request<Health>("/health/live");

export const listItems = () => request<Item[]>("/items");

export const searchItems = (q: string) =>
  request<Item[]>(`/items/search?${new URLSearchParams({ q })}`);

export const similarItems = (id: string) => request<Item[]>(`/items/${id}/similar`);

export const listFiles = () => request<StoredFile[]>("/files");

export const downloadHref = (id: string) => `/api/files/${id}/download`;

export const checkProvider = () => postJSON<CheckResult>("/checks/provider");

export const sendTestEmail = () => postJSON<CheckResult>("/checks/email");

/** The upload, with progress. `XMLHttpRequest` because `fetch` reports no upload
 *  progress, and a 450 MB file must show that it is moving. */
export function uploadFile(file: File, onProgress: (fraction: number) => void) {
  return new Promise<StoredFile>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/files");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText) as StoredFile);
        return;
      }
      redirectIfSessionDied(xhr.status);
      reject(new ApiError(xhr.status, errorDetail(xhr.status, xhr.responseText)));
    };
    xhr.onerror = () => reject(new ApiError(0, "The upload failed: no answer from the server."));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

export type StreamEvent = { event: string; data: unknown };

/** POSTs to `/stream` and calls `onEvent` for each event as it arrives, including
 *  FastAPI's `: ping` keep-alive comments (as `event: "ping"`). */
export async function runStream(onEvent: (event: StreamEvent) => void): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/stream", { method: "POST" });
  } catch {
    throw new ApiError(0, "Cannot reach the backend (/api).");
  }
  if (!res.ok || !res.body) {
    redirectIfSessionDied(res.status);
    throw new ApiError(res.status, errorDetail(res.status, await res.text()));
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    // Events end with a blank line; the last piece may be incomplete.
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) onEvent(parseBlock(block));
  }
}

function parseBlock(block: string): StreamEvent {
  let event = "message";
  let data: unknown = null;
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) event = "ping";
    else if (line.startsWith("event:")) event = line.slice("event:".length).trim();
    else if (line.startsWith("data:")) data = JSON.parse(line.slice("data:".length));
  }
  return { event, data };
}
