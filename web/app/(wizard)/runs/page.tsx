"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Card } from "@/components/Card";
import { listRuns, type RunSummary } from "@/lib/api";

export default function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);

  useEffect(() => {
    listRuns()
      .then(setRuns)
      .catch(() => setRuns([]));
  }, []);

  return (
    <Card className="p-10">
      <h1 className="font-display text-3xl font-semibold text-ink">Your runs</h1>

      {runs === null && <p className="mt-6 text-ink-soft">Loading...</p>}
      {runs?.length === 0 && <p className="mt-6 text-ink-soft">No runs yet.</p>}

      <div className="mt-6 space-y-3">
        {runs?.map((run) => (
          <Link
            key={run.run_id}
            href={`/step-3?run=${run.run_id}`}
            className="block rounded-xl border border-border bg-white p-4 hover:border-teal"
          >
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium text-ink">{run.market}</p>
                <p className="text-sm text-ink-soft">
                  {run.geography} · {run.category_prompt || "All relevant players"}
                </p>
              </div>
              <span
                className={`rounded-full px-3 py-1 text-xs font-medium ${
                  run.status === "done"
                    ? "bg-teal/10 text-teal"
                    : run.status === "error"
                      ? "bg-red-100 text-red-700"
                      : "bg-canvas-2 text-ink-soft"
                }`}
              >
                {run.status}
              </span>
            </div>
          </Link>
        ))}
      </div>
    </Card>
  );
}
