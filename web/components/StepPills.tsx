export function StepPills({ current }: { current: 1 | 2 | 3 }) {
  const steps = [1, 2, 3] as const;
  return (
    <div className="flex gap-2">
      {steps.map((step) => (
        <span
          key={step}
          className={`rounded-full px-4 py-1.5 text-sm font-medium border ${
            step === current
              ? "bg-teal text-white border-teal"
              : "bg-transparent text-ink-soft border-border"
          }`}
        >
          Step {step}
        </span>
      ))}
    </div>
  );
}
