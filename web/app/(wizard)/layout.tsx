"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useSession } from "@/lib/useSession";
import { logout } from "@/lib/api";
import { useWizardState } from "@/lib/useWizardState";

export default function WizardLayout({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const router = useRouter();
  const { reset } = useWizardState();

  useEffect(() => {
    if (session === null) router.replace("/login");
  }, [session, router]);

  async function handleSignOut() {
    await logout().catch(() => {});
    router.push("/login");
  }

  function handleNewSearch() {
    reset();
    router.push("/step-1");
  }

  if (session === undefined || session === null) {
    return <main className="flex flex-1 items-center justify-center text-ink-soft">Loading...</main>;
  }

  return (
    <div className="flex flex-1 flex-col px-6 py-8">
      <header className="mx-auto flex w-full max-w-4xl items-center justify-between pb-6">
        <div>
          <p className="font-display text-sm font-semibold tracking-[0.2em] text-teal uppercase">
            Market Universe Finder
          </p>
          <p className="text-sm text-ink-soft">{session.email}</p>
        </div>
        <nav className="flex items-center gap-6 text-sm font-medium text-ink">
          <button onClick={handleNewSearch} className="hover:text-teal">
            New search
          </button>
          <Link href="/runs" className="hover:text-teal">
            Your runs
          </Link>
          <button onClick={handleSignOut} className="hover:text-teal">
            Sign out
          </button>
        </nav>
      </header>
      <main className="mx-auto w-full max-w-4xl flex-1">{children}</main>
    </div>
  );
}
