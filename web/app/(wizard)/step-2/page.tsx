"use client";

import { useRouter } from "next/navigation";
import { Card } from "@/components/Card";
import { StepPills } from "@/components/StepPills";
import { PrimaryButton, SecondaryButton } from "@/components/Buttons";
import { useWizardState } from "@/lib/useWizardState";

export default function Step2Page() {
  const router = useRouter();
  const { state, update, loaded } = useWizardState();

  function addSection() {
    update({ sections: [...state.sections, { name: "", description: "" }] });
  }

  function updateSection(index: number, patch: Partial<{ name: string; description: string }>) {
    const next = state.sections.slice();
    next[index] = { ...next[index], ...patch };
    update({ sections: next });
  }

  function removeSection(index: number) {
    update({ sections: state.sections.filter((_, i) => i !== index) });
  }

  if (!loaded) return null;

  return (
    <Card className="p-10">
      <StepPills current={2} />
      <h1 className="font-display mt-6 text-3xl font-semibold text-ink">Step 2 — Market structure</h1>
      <p className="mt-1 text-ink-soft">
        Market: <span className="font-medium text-ink">{state.market}</span> · {state.geography}
      </p>

      <p className="mt-6 text-ink">
        How should we define the scope of this market?
      </p>
      <div className="mt-3 space-y-2">
        <label className="flex items-center gap-3">
          <input
            type="radio"
            checked={state.mode === "write"}
            onChange={() => update({ mode: "write" })}
            className="accent-teal"
          />
          <span className="font-medium text-ink">Write it myself</span>
        </label>
        <label className="flex items-center gap-3">
          <input
            type="radio"
            checked={state.mode === "describe"}
            onChange={() => update({ mode: "describe" })}
            className="accent-teal"
          />
          <span className="font-medium text-ink">Describe it in your own words</span>
        </label>
      </div>

      {state.mode === "write" ? (
        <div className="mt-6 space-y-4">
          {state.sections.map((section, i) => (
            <div key={i} className="rounded-xl border border-border bg-white p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 space-y-3">
                  <input
                    value={section.name}
                    onChange={(e) => updateSection(i, { name: e.target.value })}
                    placeholder="e.g. Device-Agnostic Platform Providers"
                    className="w-full rounded-lg border border-border bg-white px-3 py-2 text-ink outline-none focus:border-teal"
                  />
                  <textarea
                    value={section.description}
                    onChange={(e) => updateSection(i, { description: e.target.value })}
                    placeholder="What to profile under this section — function, example companies, include/exclude notes..."
                    rows={3}
                    className="w-full rounded-lg border border-border bg-white px-3 py-2 text-ink outline-none focus:border-teal"
                  />
                </div>
                {state.sections.length > 1 && (
                  <button
                    type="button"
                    onClick={() => removeSection(i)}
                    className="text-sm text-ink-soft hover:text-red-700"
                  >
                    Remove
                  </button>
                )}
              </div>
            </div>
          ))}
          <SecondaryButton type="button" onClick={addSection}>
            + Add a section
          </SecondaryButton>
        </div>
      ) : (
        <div className="mt-6">
          <textarea
            value={state.freeformBrief}
            onChange={(e) => update({ freeformBrief: e.target.value })}
            placeholder="Describe the market scope in your own words: what roles to include (e.g. Manufacturers, Suppliers, Parent Companies), what to exclude, segmentation, independence rules..."
            rows={12}
            className="w-full rounded-xl border border-border bg-white px-4 py-3 text-ink outline-none focus:border-teal"
          />
        </div>
      )}

      <div className="mt-8 flex justify-between">
        <SecondaryButton type="button" onClick={() => router.push("/step-1")}>
          ← Back
        </SecondaryButton>
        <PrimaryButton type="button" onClick={() => router.push("/step-3")}>
          Next →
        </PrimaryButton>
      </div>
    </Card>
  );
}
