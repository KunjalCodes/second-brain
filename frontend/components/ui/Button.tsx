import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANT: Record<Variant, string> = {
  primary: "bg-[var(--color-accent)] text-[var(--color-accent-fg)] border-transparent hover:brightness-110",
  secondary: "bg-[var(--color-surface)] text-[var(--color-fg)] border-[var(--color-border)] hover:bg-[var(--color-surface-2)]",
  ghost: "bg-transparent text-[var(--color-fg)] border-transparent hover:bg-[var(--color-surface-2)]",
  danger: "bg-[var(--color-danger)] text-[var(--color-bg)] border-transparent hover:brightness-110",
};

export function Button({ variant = "secondary", className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT[variant]} ${className}`}
    />
  );
}
