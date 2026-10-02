"use client";
/**
 * "Why is this remembered?" (Phase 14, M17). Renders GET /memories/{id}/explanation
 * as-is: every line comes from a stored record. The model's own note is shown as
 * plain text and labelled as model-written (ADR 0029).
 */
import { useQuery } from "@tanstack/react-query";
import { getMemoryExplanation } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { ExtractionDecisionView } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format/dates";
import { Tag } from "@/components/ui/Chip";
import { ErrorState, LoadingRows } from "@/components/ui/States";

const WORTHINESS: Record<string, string> = {
  explicit_user_instruction: "You asked for this to be remembered",
  model_persistent: "The extraction model judged it a durable fact",
  model_temporary: "Judged temporary",
  model_uncertain: "Judged uncertain",
  transient_activity: "Describes an activity in progress",
  hypothetical: "Hypothetical",
  calculation: "An intermediate calculation",
};

function Decision({ d }: { d: ExtractionDecisionView }) {
  return (
    <li className="space-y-1 rounded-md border border-[var(--color-border)] p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Tag>{d.decision}</Tag>
        <span className="text-xs text-[var(--color-fg-muted)]">
          {d.role === "creation" ? "created this memory" : "confirmed this memory"} · {formatDateTime(d.recorded_at)} · from a{" "}
          {d.source_kind === "message" ? "conversation message" : "document passage"}
        </span>
      </div>
      <p>
        <span className="font-medium">Why it was kept: </span>
        {d.worthiness_rule ? WORTHINESS[d.worthiness_rule] ?? d.worthiness_rule : "Recorded before this was tracked"}
      </p>
      {d.temporal_interpretation ? (
        <p>
          <span className="font-medium">When it is true: </span>
          {d.temporal_interpretation}
        </p>
      ) : null}
      <p className="text-[var(--color-fg-muted)]">
        <span className="font-medium">Decision: </span>
        {d.reason}
      </p>
      {d.model_reasoning ? (
        <p className="text-[var(--color-fg-muted)]">
          <span className="font-medium">Model&apos;s note (model-written): </span>
          {d.model_reasoning}
        </p>
      ) : null}
      {d.model_confidence != null ? (
        <p className="text-xs text-[var(--color-fg-muted)]">Model confidence: {d.model_confidence.toFixed(2)}</p>
      ) : null}
    </li>
  );
}

export function WhyRemembered({ memoryId }: { memoryId: string }) {
  const q = useQuery({
    queryKey: qk.explanation(memoryId),
    queryFn: ({ signal }) => getMemoryExplanation(memoryId, signal),
  });
  return (
    <section aria-labelledby="why-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 id="why-title" className="mb-2 font-semibold">How this memory was decided</h2>
      {q.isPending ? (
        <LoadingRows rows={2} label="Loading explanation" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : q.data.origin === "direct" ? (
        <p className="text-sm text-[var(--color-fg-muted)]">
          This memory was not extracted from a conversation or document, so there is no extraction decision on record. It was added directly, or recorded from a Git commit or coding-agent event; see its source above.
        </p>
      ) : (
        <ul className="space-y-2">{q.data.decisions.map((d, i) => <Decision key={i} d={d} />)}</ul>
      )}
      {q.data?.review.length ? (
        <p className="mt-3 text-sm">
          In review: {q.data.review.map((r) => `${r.kind} (${r.status})`).join(", ")}
        </p>
      ) : null}
      {q.data ? <p className="mt-3 text-xs text-[var(--color-fg-muted)]">{q.data.confidence_note}</p> : null}
    </section>
  );
}
