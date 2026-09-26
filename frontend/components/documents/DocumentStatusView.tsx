import type { DocumentResponse } from "@/lib/api/types";
import { DOCUMENT_PIPELINE, DOCUMENT_STATUS } from "@/lib/format/labels";
import { documentErrorText } from "@/lib/format/documents";
import { Chip } from "@/components/ui/Chip";

export function DocumentStatusChip({ doc }: { doc: DocumentResponse }) {
  const s = DOCUMENT_STATUS[doc.status];
  const icon = s.tone === "current" ? "✓" : s.tone === "danger" ? "✕" : s.tone === "warn" ? "!" : "…";
  return <Chip label={{ text: s.text, icon, tone: s.tone }} prefix="Status" />;
}

export function DocumentPipeline({ doc }: { doc: DocumentResponse }) {
  if (doc.status === "failed") {
    return (
      <p role="alert" className="text-sm text-[var(--color-danger)]">
        {documentErrorText(doc.error_code, doc.error_detail)} This document was <strong>not</strong> learned from.
      </p>
    );
  }
  if (doc.status === "partially_complete") {
    return (
      <p className="text-sm text-[var(--color-warn)]">
        Some chunks couldn’t be extracted; memories were still learned from the rest. See the chunk list for which ones.
      </p>
    );
  }
  const idx = DOCUMENT_PIPELINE.indexOf(doc.status === "chunked" ? "extracting" : doc.status);
  return (
    <ol className="flex flex-wrap gap-1 text-xs" aria-label="Processing steps">
      {DOCUMENT_PIPELINE.map((step, i) => (
        <li
          key={step}
          aria-current={i === idx ? "step" : undefined}
          className={`rounded px-1.5 py-0.5 ${i < idx ? "bg-[var(--color-surface-2)] text-[var(--color-fg-muted)]" : i === idx ? "border border-[var(--color-accent)] font-semibold" : "text-[var(--color-fg-muted)]"}`}
        >
          {i < idx ? "✓ " : ""}
          {DOCUMENT_STATUS[step].text}
        </li>
      ))}
    </ol>
  );
}
