import type { NextConfig } from "next";

// Where the backend listens, seen from the Next server. `src/lib/api.ts` and
// `src/proxy.ts` read the same variable at run time for their own requests.
const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://localhost:8010";

// The version of this build, given to `next build` (the image build sets it). The
// same value in every environment the image runs in, so baking it in is right,
// unlike an address or a key.
const APP_VERSION = process.env.APP_VERSION;

const nextConfig: NextConfig = {
  // A self-contained server (`.next/standalone/server.js`) with only the
  // dependencies it uses, for the container image.
  output: "standalone",

  // No `X-Powered-By: Next.js`.
  poweredByHeader: false,

  // Static assets live under `/_next/static/<build id>/`; with the version as the
  // build id, a deploy's assets are recognizable in the proxy's logs.
  generateBuildId: async () => APP_VERSION || null,

  // Read by the footer. Not `NEXT_PUBLIC_*`: nothing environment-specific is baked
  // into the build, so one build serves dev and prod.
  env: {
    BUILD_VERSION: APP_VERSION || "dev",
  },

  experimental: {
    // Next buffers the body of any request passing through the rewrite and, past
    // this limit, silently passes on only the first part. Local only: in
    // production `/api` goes from the proxy straight to the backend. Above the
    // backend's 500 MB cap, so the backend is the one that answers 413.
    proxyClientMaxBodySize: "510mb",
  },

  // Browser `/api/...` reaches the backend without the prefix: page and API share an
  // origin, so the session cookie goes along without CORS. For local development
  // only: the destination is fixed when the app is built, and in production nginx
  // routes `/api/` to the backend before a request ever reaches Next.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_INTERNAL_URL}/:path*` }];
  },

  // Security headers on every UI response. The CSP is not here: it needs a fresh
  // nonce per request, so `src/proxy.ts` sets it.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          // Duplicates `frame-ancestors` for old browsers (clickjacking).
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), payment=()",
          },
          { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
          // Effective only over HTTPS. No `includeSubDomains`: other subdomains are
          // not the app's.
          { key: "Strict-Transport-Security", value: "max-age=31536000" },
        ],
      },
    ];
  },
};

export default nextConfig;
