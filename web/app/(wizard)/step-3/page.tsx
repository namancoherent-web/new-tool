"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Card } from "@/components/Card";
import { StepPills } from "@/components/StepPills";
import { PrimaryButton, SecondaryButton } from "@/components/Buttons";
import { useWizardState, buildBrief } from "@/lib/useWizardState";
import { startRun, stopRun, getRun, downloadUrl, type RunSummary, ApiError } from "@/lib/api";

const POLL_INTERVAL_MS = 2000;

export default function Step3Page() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const existingRunId = searchParams.get("run");
  const { state, loaded } = useWizardState();
  const [runId, setRunId] = useState<string | null>(existingRunId);
  const [run, setRun] = useState<RunSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [briefOpen, setBriefOpen] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const brief = buildBrief(state);

  function pollRun(id: string) {
    pollRef.current = setInterval(async () => {
      try {
        const summary = await getRun(id);
        setRun(summary);
        if (summary.status !== "running" && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch (err) {
        if (pollRef.current) clearInterval(pollRef.current);
        setError(err instanceof ApiError ? err.message : "Lost connection to the run.");
      }
    }, POLL_INTERVAL_MS);
  }

  // resuming a run from "Your runs" -- fetch its current state once, then
  // keep polling only if it's still in progress
  useEffect(() => {
    if (!existingRunId) return;
    getRun(existingRunId)
      .then((summary) => {
        setRun(summary);
        if (summary.status === "running") pollRun(existingRunId);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load that run."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existingRunId]);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  async function handleStart() {
    setError(null);
    if (!brief.trim()) {
      setError(
        "Your market scope description is empty, so nothing would be sent to Google AI Mode " +
          "except a generic fallback query. Go back to Step 2 and fill in your description before starting."
      );
      return;
    }
    setStarting(true);
    try {
      const { run_id } = await startRun({
        market: state.market,
        geography: state.geography,
        category_prompt: "",
        brief,
      });
      setRunId(run_id);
      pollRun(run_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the run. Is the API running?");
    } finally {
      setStarting(false);
    }
  }

  async function handleStop() {
    if (!runId) return;
    setStopping(true);
    try {
      await stopRun(runId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not stop the run.");
    } finally {
      setStopping(false);
    }
  }

  if (!loaded) return null;

  const isRunning = run?.status === "running" || (runId && !run);
  const isDone = run?.status === "done";
  const isError = run?.status === "error";
  const isCancelled = run?.status === "cancelled";

  return (
    <Card className="p-10">
      <StepPills current={3} />
      <h1 className="font-display mt-6 text-3xl font-semibold text-ink">Step 3 — Review & run</h1>

      <div className="mt-6 grid grid-cols-2 gap-4">
        <div className="rounded-xl border border-border bg-white p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-soft">Market</p>
          <p className="mt-1 font-medium text-ink">{state.market}</p>
        </div>
        <div className="rounded-xl border border-border bg-white p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-soft">Geography</p>
          <p className="mt-1 font-medium text-ink">{state.geography}</p>
        </div>
      </div>

      {brief && (
        <div className="mt-4">
          <button
            type="button"
            onClick={() => setBriefOpen((v) => !v)}
            className="text-sm font-medium text-teal"
          >
            {briefOpen ? "▾" : "▸"} Full structure brief
          </button>
          {briefOpen && (
            <pre className="mt-2 whitespace-pre-wrap rounded-xl border border-border bg-white p-4 text-sm text-ink-soft">
              {brief}
            </pre>
          )}
        </div>
      )}

      {error && <p className="mt-6 text-sm text-red-700">{error}</p>}

      {!runId && (
        <div className="mt-8 flex justify-between">
          <SecondaryButton type="button" onClick={() => router.push("/step-2")}>
            ← Back
          </SecondaryButton>
          <PrimaryButton type="button" onClick={handleStart} disabled={starting}>
            {starting ? "Starting..." : "Start run"}
          </PrimaryButton>
        </div>
      )}

      {isRunning && (
        <div className="mt-8">
          <div className="flex items-center justify-between">
            <h2 className="font-display text-xl font-semibold text-ink">Running...</h2>
            <SecondaryButton type="button" onClick={handleStop} disabled={stopping}>
              {stopping ? "Stopping..." : "Stop"}
            </SecondaryButton>
          </div>
          <p className="mt-1 text-sm text-ink-soft">
            This can take a while for a full discovery pass. Do not close this tab.
          </p>
          <div className="mt-4 max-h-80 space-y-1 overflow-y-auto rounded-xl border border-border bg-white p-4 font-mono text-xs text-ink-soft">
            {run?.progress_log.length ? (
              run.progress_log.map((entry, i) => (
                <div key={i}>
                  [{entry.stage}] {entry.detail}
                </div>
              ))
            ) : (
              <div>Starting...</div>
            )}
          </div>
        </div>
      )}

      {isError && (
        <div className="mt-8 rounded-xl border border-red-200 bg-red-50 p-4 text-red-800">
          Run failed: {run?.error}
        </div>
      )}

      {isCancelled && (
        <div className="mt-8 rounded-xl border border-border bg-canvas-2 p-4 text-ink-soft">
          Run stopped. Go back and click Start run to try again.
        </div>
      )}

      {isDone && run && (
        <div className="mt-8">
          <h2 className="font-display text-xl font-semibold text-ink">
            Done — {run.companies_count} companies found
          </h2>
          <div className="mt-3 grid grid-cols-3 gap-4 text-center">
            <div className="rounded-xl border border-border bg-white p-4">
              <p className="text-2xl font-semibold text-ink">{run.total_candidates_found}</p>
              <p className="text-xs text-ink-soft">Candidates found</p>
            </div>
            <div className="rounded-xl border border-border bg-white p-4">
              <p className="text-2xl font-semibold text-ink">{run.total_verified}</p>
              <p className="text-xs text-ink-soft">Passed verification</p>
            </div>
            <div className="rounded-xl border border-border bg-white p-4">
              <p className="text-2xl font-semibold text-ink">{run.companies_count}</p>
              <p className="text-xs text-ink-soft">Final companies</p>
            </div>
          </div>

          <div className="mt-6 flex gap-3">
            {(run.download_formats ?? []).map((fmt) => (
              <a
                key={fmt}
                href={downloadUrl(run.run_id, fmt)}
                className="rounded-xl border border-teal px-5 py-2.5 font-medium text-teal hover:bg-teal hover:text-white"
              >
                Download {fmt.toUpperCase()}
              </a>
            ))}
          </div>

          {run.companies_preview && run.companies_preview.length > 0 && (
            <div className="mt-8 overflow-x-auto rounded-xl border border-border bg-white">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border bg-canvas-2 text-left text-ink-soft">
                    <th className="px-4 py-2 font-medium">Company</th>
                    <th className="px-4 py-2 font-medium">Website</th>
                    <th className="px-4 py-2 font-medium">HQ</th>
                    <th className="px-4 py-2 font-medium">Category</th>
                    <th className="px-4 py-2 font-medium">Confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {run.companies_preview.map((c, i) => (
                    <tr key={i} className="border-b border-border last:border-0">
                      <td className="px-4 py-2 text-ink">{c.company_name}</td>
                      <td className="px-4 py-2 text-ink-soft">{c.website}</td>
                      <td className="px-4 py-2 text-ink-soft">{c.hq_country}</td>
                      <td className="px-4 py-2 text-ink-soft">{c.category}</td>
                      <td className="px-4 py-2 text-ink-soft">{c.confidence}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
