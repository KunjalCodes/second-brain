import type { ReactNode } from "react";
import { TONE_VAR, type Tone } from "@/lib/format/glyphs";

export interface TimelineEntry {
  key: string;
  at?: string | null;
  glyph: string;
  label: string;
  tone: Tone;
  body: ReactNode;
}

/** A vertical timeline with typed markers: the marker carries a glyph AND a text label. */
export function Timeline({ entries, label }: { entries: TimelineEntry[]; label: string }) {
  return (
    <ol aria-label={label} className="relative space-y-3 border-l border-[var(--color-border)] pl-5">
      {entries.map((e) => (
        <li key={e.key} className="relative">
          <span
            aria-hidden="true"
            className="absolute -left-[31px] top-0.5 flex h-5 w-5 items-center justify-center rounded-full border bg-[var(--color-surface)] text-[11px]"
            style={{ color: TONE_VAR[e.tone], borderColor: TONE_VAR[e.tone] }}
          >
            {e.glyph}
          </span>
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-xs font-semibold uppercase tracking-wide" style={{ color: TONE_VAR[e.tone] }}>
              {e.label}
            </span>
            {e.at ? (
              <time dateTime={e.at} className="text-xs text-[var(--color-fg-muted)]">
                {new Date(e.at).toLocaleString()}
              </time>
            ) : null}
          </div>
          <div className="content-text text-sm">{e.body}</div>
        </li>
      ))}
    </ol>
  );
}

/** Memory -> Evidence -> Source -> History: the "why does the AI know this" path. */
export function TraceStrip({ steps }: { steps: { label: string; href: string }[] }) {
  return (
    <nav aria-label="Trace" className="flex flex-wrap items-center gap-1 text-xs">
      {steps.map((s, i) => (
        <span key={s.label} className="inline-flex items-center gap-1">
          {i > 0 ? <span aria-hidden="true" className="text-[var(--color-fg-muted)]">→</span> : null}
          <a href={s.href} className="rounded-full border border-[var(--color-border)] px-2 py-0.5 hover:border-[var(--color-accent)]">
            {s.label}
          </a>
        </span>
      ))}
    </nav>
  );
}
