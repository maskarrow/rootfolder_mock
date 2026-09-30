"use client";

import { useState } from "react";

import { logout } from "@/lib/api";

export function LogoutButton() {
  const [leaving, setLeaving] = useState(false);

  async function leave() {
    setLeaving(true);
    try {
      await logout();
    } finally {
      // A full load, not `router.push`, so no state of the logged-out user stays in
      // memory.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign("/login");
    }
  }

  return (
    <button type="button" className="secondary" onClick={() => void leave()} disabled={leaving}>
      {leaving ? "Logging out…" : "Log out"}
    </button>
  );
}
