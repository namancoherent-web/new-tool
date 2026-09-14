"use client";

import { useEffect, useState } from "react";
import { getSession, type Session } from "./api";

export function useSession() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);

  useEffect(() => {
    getSession()
      .then(setSession)
      .catch(() => setSession(null));
  }, []);

  return session; // undefined = loading, null = not logged in, Session = logged in
}
