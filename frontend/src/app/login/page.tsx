import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { LoginForm } from "@/components/login-form";
import { getMe } from "@/lib/api";
import { safeReturnPath } from "@/lib/session";

export const metadata: Metadata = {
  title: "Sign in · rootfolder-mock",
};

/** Public in `proxy.ts`, so it asks itself: someone with a good session skips the
 *  form and goes straight where they wanted. */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const { next } = await searchParams;
  const destination = safeReturnPath(next, "/");

  const loggedIn = await getMe().then(
    () => true,
    () => false
  );
  if (loggedIn) redirect(destination);

  return (
    <main className="narrow">
      <h1>rootfolder-mock</h1>
      <p className="muted">
        A deploy rehearsal app. Accounts are created on the server with{" "}
        <code>create_user.py</code>.
      </p>
      <LoginForm destination={destination} />
    </main>
  );
}
