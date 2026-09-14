import type { ButtonHTMLAttributes } from "react";

export function PrimaryButton({ className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className={`rounded-xl bg-teal px-6 py-3 font-medium text-white transition hover:bg-teal-dark disabled:cursor-not-allowed disabled:opacity-50 ${className}`}
    />
  );
}

export function SecondaryButton({ className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className={`rounded-xl border border-border bg-transparent px-6 py-3 font-medium text-ink transition hover:bg-canvas-2 disabled:cursor-not-allowed disabled:opacity-50 ${className}`}
    />
  );
}
