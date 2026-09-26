/** An accessible inline explanation (works with keyboard and touch, unlike a title tooltip). */
export function Explain({ summary = "What does this mean?", children }: { summary?: string; children: React.ReactNode }) {
  return (
    <details className="inline-block text-xs text-[var(--color-fg-muted)]">
      <summary className="cursor-pointer select-none underline decoration-dotted">{summary}</summary>
      <div className="mt-1 max-w-prose">{children}</div>
    </details>
  );
}
