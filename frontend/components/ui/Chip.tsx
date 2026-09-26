import type { Label } from "@/lib/format/labels";

const TONE: Record<Label["tone"], string> = {
  current: "text-[var(--color-current)] border-[var(--color-current)]",
  warn: "text-[var(--color-warn)] border-[var(--color-warn)]",
  danger: "text-[var(--color-danger)] border-[var(--color-danger)]",
  info: "text-[var(--color-info)] border-[var(--color-info)]",
  muted: "text-[var(--color-muted)] border-[var(--color-border)]",
  neutral: "text-[var(--color-fg-muted)] border-[var(--color-border)]",
};

/** A status chip: icon + text + tone. Colour is never the only signal. */
export function Chip({ label, prefix, title }: { label: Pick<Label, "text" | "icon" | "tone">; prefix?: string; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${TONE[label.tone]}`}
    >
      <span aria-hidden="true">{label.icon}</span>
      {prefix ? <span className="sr-only">{prefix}: </span> : null}
      {label.text}
    </span>
  );
}

export function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded border border-[var(--color-border)] bg-[var(--color-surface-2)] px-1.5 py-0.5 text-xs text-[var(--color-fg-muted)]">
      {children}
    </span>
  );
}
