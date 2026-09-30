import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE, loginHref } from "@/lib/session";

/** The app's gate: runs before every page except `/login` and asks the backend
 *  whether the cookie's session is still good (`GET /auth/me`). If not, the user
 *  goes to `/login?next=...` and the dead cookie is deleted.
 *
 *  It asks the backend rather than checking for the cookie because a cookie can
 *  outlive its session (expired, closed elsewhere, account deactivated).
 *
 *  A backend that does not answer is not a logout: the page goes on and shows its
 *  own connection error, which in a deploy points at `API_INTERNAL_URL`. */

const PUBLIC = new Set(["/login"]);

// Read at run time, when the server starts: one build serves every environment.
const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://localhost:8010";

/** `true` = live session, `false` = dead one, `null` = the backend did not answer. */
async function sessionIsLive(token: string): Promise<boolean | null> {
  try {
    const res = await fetch(`${API_INTERNAL_URL}/auth/me`, {
      headers: { cookie: `${SESSION_COOKIE}=${token}` },
      cache: "no-store",
    });
    if (res.status === 401) return false;
    return res.ok ? true : null;
  } catch {
    return null;
  }
}

const DEVELOPMENT = process.env.NODE_ENV === "development";

/** The page's content security policy.
 *
 *  Scripts pass only with the request's nonce: Next puts it on its own scripts by
 *  reading this header from the request, and `'strict-dynamic'` lets them load their
 *  chunks. This works only for pages rendered per request, which `layout.tsx`
 *  guarantees. Everything is same-origin (`next/font` self-hosts the font, `/api` is
 *  the same origin). Styles stay `'unsafe-inline'` because React writes `style`
 *  attributes (the progress bar). In development React needs `eval` for error
 *  stacks, and hot reload talks over websockets. */
function policy(nonce: string): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${DEVELOPMENT ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    `connect-src 'self'${DEVELOPMENT ? " ws: wss:" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

/** Lets the page through, with the policy on the response and the nonce on the request. */
function proceed(request: NextRequest): NextResponse {
  const nonce = btoa(crypto.randomUUID());
  const csp = policy(nonce);

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export async function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (PUBLIC.has(pathname)) return proceed(request);

  const token = request.cookies.get(SESSION_COOKIE)?.value;
  const live = token ? await sessionIsLive(token) : false;
  if (live !== false) return proceed(request);

  const response = NextResponse.redirect(new URL(loginHref(pathname + search), request.url));
  if (token) response.cookies.delete(SESSION_COOKIE);
  return response;
}

export const config = {
  matcher: [
    // Everything except `/api` and static files. `/api` also stays out because
    // `proxy` buffers the body of any request it touches, and uploads are large.
    "/((?!api|_next/static|_next/image|favicon.ico|.*\\.[a-zA-Z0-9]+$).*)",
  ],
};
