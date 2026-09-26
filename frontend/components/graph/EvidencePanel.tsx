"use client";
/** Edge evidence (`GET /graph/edges/evidence`): every assertion with the backend's own evidence status. */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { edgeEvidence, type GraphScopeParams } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { GraphSliceEdge, MemoryStatus } from "@/lib/api/types";
import { EVIDENCE_STATUS } from "@/lib/format/labels";
import { formatDateTime, validityText } from "@/lib/format/dates";
import { Chip } from "@/components/ui/Chip";
import { MemoryStatusChip, TemporalStateChip } from "@/components/ui/StatusChips";
import { ErrorState, LoadingRows, TruncationBanner } from "@/components/ui/States";

export function EvidencePanel({ edge, scope }: { edge: GraphSliceEdge; scope: GraphScopeParams }) {
  const [history, setHistory] = useState(false);
  const params = {
    source_entity_id: edge.source,
    target_entity_id: edge.target,
    predicate: edge.predicate ?? "",
    ...(history ? { temporal_scope: "all" as const } : scope),
  };
  const q = useQuery({ queryKey: qk.evidence(params), queryFn: ({ signal }) => edgeEvidence(params, signal) });
  return (
    <section aria-labelledby="evidence-title" className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="evidence-title" className="font-semibold">Evidence</h3>
        <label className="inline-flex items-center gap-1 text-xs">
          <input type="checkbox" checked={history} onChange={() => setHistory((h) => !h)} /> Include history
        </label>
      </div>
      {q.isPending ? (
        <LoadingRows rows={2} label="Loading evidence" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <>
          <p className="text-xs text-[var(--color-fg-muted)]">
            {q.data.evidence_available ? "At least one assertion has resolvable evidence." : "No assertion has resolvable evidence."}
          </p>
          {q.data.truncated ? <TruncationBanner>More assertions exist than the {q.data.limit} shown.</TruncationBanner> : null}
          {(q.data.assertions ?? []).length === 0 ? (
            <p className="text-sm">No assertion of this relationship is in the selected time scope.</p>
          ) : (
            <ol className="space-y-2">
              {(q.data.assertions ?? []).map((a) => {
                const status = EVIDENCE_STATUS[a.evidence_status];
                return (
                  <li key={a.relationship_id} className="rounded-md border border-[var(--color-border)] p-2 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <MemoryStatusChip status={a.status} />
                      <TemporalStateChip state={a.temporal_state} />
                      <span className="text-xs text-[var(--color-fg-muted)]">{validityText(a.valid_from, a.valid_until)}</span>
                    </div>
                    <p className="mt-1">
                      <Chip label={{ text: status.text, icon: status.tone === "current" ? "✓" : "!", tone: status.tone }} />
                    </p>
                    {a.memory ? (
                      <div className="mt-2 rounded bg-[var(--color-surface-2)] p-2">
                        <p className="text-xs text-[var(--color-fg-muted)]">Asserted by memory:</p>
                        <Link href={`/memories/${a.memory.id}`} className="content-text underline-offset-2 hover:underline">
                          {a.memory.content}
                        </Link>
                        <div className="mt-1 flex flex-wrap gap-2 text-xs">
                          <MemoryStatusChip status={a.memory.status as MemoryStatus} />
                          <span>Recorded {formatDateTime(a.memory.created_at)}</span>
                        </div>
                        {(a.memory.sources ?? []).length ? (
                          <ul className="mt-2 space-y-1 text-xs">
                            {(a.memory.sources ?? []).map((s) => (
                              <li key={s.source.id}>
                                <span className="font-medium">{s.source.source_type}</span>{" "}
                                {s.resolved ? "" : <span className="text-[var(--color-warn)]">(content unavailable) </span>}
                                {s.message_content ? <span className="content-text">“{s.message_content.slice(0, 200)}”</span> : null}
                                {s.document ? <Link className="underline" href={`/documents/${s.document.id}`}>{s.document.filename}</Link> : null}
                              </li>
                            ))}
                          </ul>
                        ) : null}
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          )}
        </>
      )}
    </section>
  );
}
