import { redirect } from "next/navigation";

import { Dashboard } from "@/components/dashboard";
import { LogoutButton } from "@/components/logout-button";
import { ApiError, getHealth, getMe, listFiles, type StoredFile } from "@/lib/api";
import { loginHref } from "@/lib/session";

/** The dashboard. This part renders on the Next server, which calls the backend
 *  directly at `API_INTERNAL_URL`: if it shows the user, that address works between
 *  containers and the cookie reached the backend. Everything in `Dashboard` runs in
 *  the browser, through `/api`. */
export default async function Home() {
  let me;
  let health;
  let files: StoredFile[];
  try {
    [me, health, files] = await Promise.all([getMe(), getHealth(), listFiles()]);
  } catch (error) {
    // `proxy.ts` let the page through, so a 401 here is a session that died since.
    if (error instanceof ApiError && error.status === 401) redirect(loginHref("/"));
    return (
      <main>
        <h1>delegate-mock</h1>
        <p role="alert" className="error">
          The Next server could not load the dashboard from the backend.{" "}
          {error instanceof Error ? error.message : String(error)}
        </p>
        <p className="muted">
          Check <code>API_INTERNAL_URL</code> in the frontend&apos;s environment and that the
          backend answers <code>/health/ready</code>.
        </p>
      </main>
    );
  }

  return (
    <main>
      <header className="top">
        <div>
          <h1>delegate-mock</h1>
          <p className="muted">
            {me.name} ({me.email}), {me.role} of {me.org_name}
          </p>
        </div>
        <LogoutButton />
      </header>

      <section className="card">
        <h2>Seen from the Next server</h2>
        <p className="muted">
          Loaded by the Next server straight from <code>API_INTERNAL_URL</code>, not through
          nginx. So the address and scheme below are the web container&apos;s; the browser&apos;s
          own are under &quot;Who am I via /api&quot;.
        </p>
        <dl>
          <dt>seen_ip</dt>
          <dd>{me.seen_ip ?? "unknown"}</dd>
          <dt>scheme</dt>
          <dd>{me.scheme}</dd>
          <dt>Backend version</dt>
          <dd>{health.version}</dd>
          <dt>Backend env</dt>
          <dd>{health.env}</dd>
        </dl>
      </section>

      <Dashboard isAdmin={me.role === "admin"} initialFiles={files} />

      <footer className="muted">Frontend build {process.env.BUILD_VERSION}</footer>
    </main>
  );
}
