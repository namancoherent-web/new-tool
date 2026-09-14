"use client";

import { useEffect, useState } from "react";

export interface WizardState {
  market: string;
  geography: string;
  categoryPrompt: string;
  mode: "write" | "describe";
  sections: { name: string; description: string }[];
  freeformBrief: string;
}

const DEFAULT_STATE: WizardState = {
  market: "",
  geography: "Global",
  categoryPrompt: "",
  mode: "write",
  sections: [{ name: "", description: "" }],
  freeformBrief: "",
};

const STORAGE_KEY = "market-universe-finder:wizard";

export function useWizardState() {
  const [state, setState] = useState<WizardState>(DEFAULT_STATE);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw) {
      try {
        setState({ ...DEFAULT_STATE, ...JSON.parse(raw) });
      } catch {
        // ignore corrupt stored state, fall back to defaults
      }
    }
    setLoaded(true);
  }, []);

  useEffect(() => {
    if (loaded) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }, [state, loaded]);

  function update(patch: Partial<WizardState>) {
    setState((prev) => ({ ...prev, ...patch }));
  }

  function reset() {
    sessionStorage.removeItem(STORAGE_KEY);
    setState(DEFAULT_STATE);
  }

  return { state, update, reset, loaded };
}

/** Combine Step 2's inputs into the single free-text brief the pipeline expects. */
export function buildBrief(state: WizardState): string {
  if (state.mode === "describe") {
    return state.freeformBrief.trim();
  }
  const sectionLines = state.sections
    .filter((s) => s.name.trim())
    .map((s) => `${s.name.trim()}${s.description.trim() ? `: ${s.description.trim()}` : ""}`);
  return sectionLines.length
    ? `Include companies from the following sections/segments:\n${sectionLines.join("\n")}`
    : "";
}
