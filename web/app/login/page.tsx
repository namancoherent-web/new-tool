"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card } from "@/components/Card";
import { PrimaryButton } from "@/components/Buttons";
import { login, ApiError } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!email.trim()) {
      setError("Enter your email to sign in.");
      return;
    }
    setSubmitting(true);
    try {
      await login(email.trim());
      router.push("/step-1");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign in failed. Is the API running?");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex flex-1 items-center justify-center px-6 py-20">
      <div className="w-full max-w-xl">
        <p className="font-display text-sm font-semibold tracking-[0.2em] text-teal uppercase">
          Market Universe Finder
        </p>
        <h1 className="font-display mt-2 text-5xl font-semibold leading-tight text-ink">
          Vendor Universe
        </h1>
        <p className="mt-4 max-w-md text-ink-soft">
          Discover, verify, and classify real companies for any market — then download
          Excel and Word files when the run finishes.
        </p>

        <Card className="mt-10 p-8">
          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label htmlFor="email" className="mb-2 block text-sm font-medium text-ink">
                Email
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="w-full rounded-xl border border-border bg-white px-4 py-3 text-ink outline-none focus:border-teal"
              />
            </div>
            {error && <p className="text-sm text-red-700">{error}</p>}
            <PrimaryButton type="submit" disabled={submitting} className="w-full">
              {submitting ? "Signing in..." : "Sign in"}
            </PrimaryButton>
          </form>
        </Card>
      </div>
    </main>
  );
}
