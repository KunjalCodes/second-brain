"use client";
/**
 * "Why is this relationship here, and is it current?" (Phase 15, M11).
 * Everything shown is a stored window, status, link or source; the sentence is
 * assembled by the server from those fields, never generated.
 */
import { useQuery } from "@tanstack/react-query";
import { explainEdge, type GraphScopeParams } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { EVIDENCE_BASIS, RELATION_KIND } from "@/lib/format/labels";
import { Tag } from "@/components/ui/Chip";
import { ErrorState, LoadingRows } from "@/components/ui/States";

export function ExplainPanel({
  source,
  target,
  predicate,
  scope,
}: {
  source: string;
  target: string;
  predicate: string;
  scope: GraphScopeParams;
}) {
  const params = { source_entity_id: source, target_entity_id: target, predicate, ...scope };
  const q = useQuery({
    queryKey: qk.edgeExplanation(params),
    queryFn: ({ signal }) => explainEdge(params, signal),
  });
  return (
    <section aria-label="Why this relationship exists" className="space-y-2 rounded-md border border-[var(--color-border)] p-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">Why is this here?</h3>
      {q.isPending ? (
        <LoadingRows rows={2} label="Explaining relationship" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} notFound="No stored relationship like this exists in the chosen time scope." />
      ) : (
        <>
          <p>{q.data.why}</p>
          <div className="flex flex-wrap gap-2">
            {q.data.basis.map((b) => (
              <span key={b} title={EVIDENCE_BASIS[b]?.explain}><Tag>{EVIDENCE_BASIS[b]?.text ?? b}</Tag></span>
            ))}
            <span title={RELATION_KIND[q.data.relation_kind]?.explain}>
              <Tag>{RELATION_KIND[q.data.relation_kind]?.text ?? q.data.relation_kind}</Tag>
            </span>
          </div>
          <p className="text-xs text-[var(--color-fg-muted)]">
            {q.data.supporting_memories.length
              ? `Supported by ${q.data.supporting_memories.length} memor${q.data.supporting_memories.length === 1 ? "y" : "ies"} — listed with their sources below.`
              : "No supporting memory is on record."}
          </p>
          {q.data.truncated ? <p className="text-xs text-[var(--color-warn)]">Showing the first supporting memories only.</p> : null}
        </>
      )}
    </section>
  );
}
