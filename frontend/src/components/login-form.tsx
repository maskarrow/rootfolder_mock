"use client";

import { useState } from "react";

import { ApiError, login } from "@/lib/api";

/** What the user sees for each refusal. 401 covers an unknown email and a wrong
 *  password alike: the backend does not tell them apart, on purpose. */
function message(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Wrong email or password.";
    if (error.status === 429) return "Too many failed attempts. Try again in 15 minutes.";
    // 403 here is `CheckOrigin`: the page's origin is missing from CORS_ORIGINS.
    if (error.status === 403) return `Refused by the backend: ${error.message}`;
    return error.message;
  }
  return "Something went wrong. Try again.";
}

export function LoginForm({ destination }: { destination: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setPending(true);
    setError(null);
    try {
      await login(email, password, remember);
      // A full load, not client navigation: the server-rendered dashboard must
      // start fresh with the new cookie.
      window.location.assign(destination);
    } catch (e) {
      setError(message(e));
      setPending(false);
    }
  }

  return (
    <form
      className="card"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label>
        Email
        <input
          type="email"
          autoComplete="username"
          autoFocus
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </label>
      <label>
        Password
        <input
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>
      {/* Unchecked, the cookie ends with the browser; checked, it lasts up to 30
          days unless idle for 8 hours. */}
      <label className="inline">
        <input
          type="checkbox"
          checked={remember}
          onChange={(event) => setRemember(event.target.checked)}
        />
        Remember me
      </label>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <button type="submit" disabled={pending}>
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
