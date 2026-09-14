"use client";

import { useRouter } from "next/navigation";
import { Card } from "@/components/Card";
import { StepPills } from "@/components/StepPills";
import { PrimaryButton } from "@/components/Buttons";
import { useWizardState } from "@/lib/useWizardState";

export default function Step1Page() {
  const router = useRouter();
  const { state, update, loaded } = useWizardState();

  function handleNext(e: React.FormEvent) {
    e.preventDefault();
    if (!state.market.trim()) return;
    router.push("/step-2");
  }

  if (!loaded) return null;

  return (
    <Card className="p-10">
      <StepPills current={1} />
      <h1 className="font-display mt-6 text-3xl font-semibold text-ink">Market & geography</h1>
      <p className="mt-2 text-ink-soft">Name the landscape you want mapped.</p>

      <form onSubmit={handleNext} className="mt-8 space-y-6">
        <div>
          <label className="mb-2 block text-sm font-medium text-ink">Market</label>
          <input
            value={state.market}
            onChange={(e) => update({ market: e.target.value })}
            placeholder="e.g. Global Food Thin Wafers Market"
            className="w-full rounded-xl border border-border bg-white px-4 py-3 text-ink outline-none focus:border-teal"
          />
        </div>
        <div>
          <label className="mb-2 block text-sm font-medium text-ink">Geography</label>
          <input
            value={state.geography}
            onChange={(e) => update({ geography: e.target.value })}
            placeholder="global"
            className="w-full rounded-xl border border-border bg-white px-4 py-3 text-ink outline-none focus:border-teal"
          />
        </div>
        <div className="flex justify-end">
          <PrimaryButton type="submit" disabled={!state.market.trim()}>
            Next
          </PrimaryButton>
        </div>
      </form>
    </Card>
  );
}
