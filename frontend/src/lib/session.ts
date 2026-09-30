/** Session facts shared by `proxy.ts` and the rest of the app. Kept dependency-free
 *  on purpose: `proxy.ts` runs before pages and must not pull in `api.ts` and
 *  `next/headers`. */

/** The session cookie set by the backend at login (`backend/app/services/auth.py`, `COOKIE`). */
export const SESSION_COOKIE = "mock_session";

/** The login page, with the path to return to afterwards. */
export function loginHref(next?: string): string {
  return next && next !== "/" ? `/login?next=${encodeURIComponent(next)}` : "/login";
}

/** A probe origin to resolve `next` against; it never has to exist. */
const PROBE_ORIGIN = "http://mock.invalid";

/** Where to go after login: only a path inside the app.
 *
 *  `next` comes from the address bar, so anyone can write it; unchecked,
 *  `/login?next=https://evil.example` would send a freshly logged-in user to a
 *  look-alike site. Browsers strip tabs and newlines before resolving, so
 *  `/<TAB>/evil.example` becomes `//evil.example`; hence:
 *  - any control character or `\` falls back to the default;
 *  - the resolved URL must stay on the same origin;
 *  - only the resolved path is returned, never the input text. */
export function safeReturnPath(next: string | null | undefined, fallback: string): string {
  if (!next || !next.startsWith("/")) return fallback;
  for (const char of next) {
    const code = char.charCodeAt(0);
    if (code < 0x20 || code === 0x7f || char === "\\") return fallback;
  }

  let url: URL;
  try {
    url = new URL(next, PROBE_ORIGIN);
  } catch {
    return fallback;
  }
  if (url.origin !== PROBE_ORIGIN || url.pathname === "/login") return fallback;
  return url.pathname + url.search + url.hash;
}
