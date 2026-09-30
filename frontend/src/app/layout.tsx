import type { Metadata } from "next";
import { Figtree } from "next/font/google";
import { connection } from "next/server";

import "./globals.css";

/** The one web font, through `next/font/google` as in Delegate. The build downloads
 *  it, so `next build` needs internet; the page then serves it from its own origin,
 *  which the CSP requires. `latin-ext` brings the Romanian diacritics. */
const sans = Figtree({
  variable: "--font-sans",
  subsets: ["latin", "latin-ext"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "delegate-mock",
  description: "A deploy rehearsal app with Delegate's technical shape.",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Every page renders per request. The CSP nonce from `proxy.ts` reaches Next's
  // scripts only in a dynamic render; a page prerendered at build would ship its
  // scripts without it, and the browser would block them in production.
  await connection();

  return (
    <html lang="en" className={sans.variable}>
      <body>{children}</body>
    </html>
  );
}
