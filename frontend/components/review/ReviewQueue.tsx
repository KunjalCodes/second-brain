"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { listEntityReview, listMemoryReview, reviewHistory } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { formatDateTime } from "@/lib/format/dates";
import { Tag } from "@/components/ui/Chip";
import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/ui/States";
import { Pagination } from "@/components/ui/Pagination";

export const KIND_TEXT: Record<string, string> = {
  likely_duplicate: "Likely duplicate",
  flagged: "Possible duplicate (flagged)",
  conflicting: "Conflict",
};

export const ACTION_TEXT: Record<string, string> = {
  CONFIRM_DUPLICATE: "Confirmed duplicate",
  CONFIRM_DISTINCT: "Kept as different memories",
  RESOLVE_CONFLICT_SUPERSEDE: "Conflict resolved — one replaced the other",
  RESOLVE_CONFLICT_KEEP_BOTH: "Conflict acknowledged — both kept",
  RESOLVE_CONFLICT_REJECT: "Marked not a real conflict",
  MERGE_ENTITY: "Entities merged",
  REJECT_ENTITY_MERGE: "Entities kept separate",
  REVERSE_MERGE: "Entity merge undone",
};

type Tab = "memories" | "entities" | "history";

export function ReviewQueue() {
  const [tab, setTab] = useState<Tab>("memories");
  const [page, setPage] = useState(1);
  const mem = useQuery({ queryKey: qk.reviewMemories(page), queryFn: ({ signal }) => listMemoryReview(25, (page - 1) * 25, signal), enabled: tab === "memories" });
  const ent = useQuery({ queryKey: qk.reviewEntities(page), queryFn: ({ signal }) => listEntityReview(25, (page - 1) * 25, signal), enabled: tab === "entities" });
  const hist = useQuery({ queryKey: qk.reviewHistory(page), queryFn: ({ signal }) => reviewHistory(25, (page - 1) * 25, signal), enabled: tab === "history" });
  const active = tab === "memories" ? mem : tab === "entities" ? ent : hist;

  return (
    <div>
      <PageHeader title="Review" />
      <p className="mb-3 max-w-prose text-sm text-[var(--color-fg-muted)]">
        When the AI suspects two memories are the same, contradict each other, or two entities are the same thing, it asks you instead of deciding. Candidates only appear after extraction or semantic checks flag them.
      </p>
      <div role="tablist" aria-label="Review queues" className="mb-3 flex gap-1 border-b border-[var(--color-border)]">
        {(["memories", "entities", "history"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            type="button"
            aria-selected={tab === t}
            onClick={() => {
              setTab(t);
              setPage(1);
            }}
            className={`px-3 py-1.5 text-sm ${tab === t ? "border-b-2 border-[var(--color-accent)] font-semibold" : "text-[var(--color-fg-muted)]"}`}
          >
            {t === "memories" ? "Memories" : t === "entities" ? "Entity merges" : "Decision history"}
          </button>
        ))}
      </div>
      <div role="tabpanel">
        {active.isPending ? (
          <LoadingRows rows={3} label="Loading review items" />
        ) : active.isError ? (
          <ErrorState error={active.error} onRetry={() => void active.refetch()} />
        ) : tab === "memories" && mem.data ? (
          mem.data.items.length === 0 ? (
            <EmptyState title="Nothing to review">No duplicate or conflicting memories are waiting for a decision.</EmptyState>
          ) : (
            <>
              <ul className="space-y-2">
                {mem.data.items.map((c) => (
                  <li key={c.id}>
                    <Link href={`/review/memories/${c.id}`} className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3 hover:border-[var(--color-accent)]">
                      <Tag>{KIND_TEXT[c.kind] ?? c.kind}</Tag>
                      {c.score !== null && c.score !== undefined ? <span className="text-xs">similarity {c.score.toFixed(2)} (relative)</span> : null}
                      <span className="ml-auto text-xs text-[var(--color-fg-muted)]">flagged {formatDateTime(c.created_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
              <Pagination page={page} pageSize={25} total={mem.data.total} onPage={setPage} />
            </>
          )
        ) : tab === "entities" && ent.data ? (
          ent.data.items.length === 0 ? (
            <EmptyState title="No entity merges to review" />
          ) : (
            <>
              <ul className="space-y-2">
                {ent.data.items.map((c) => (
                  <li key={c.id}>
                    <Link href={`/review/entities/${c.id}`} className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3 hover:border-[var(--color-accent)]">
                      <Tag>Possible same entity</Tag>
                      {c.score !== null && c.score !== undefined ? <span className="text-xs">similarity {c.score.toFixed(2)} (relative)</span> : null}
                      <span className="ml-auto text-xs text-[var(--color-fg-muted)]">{formatDateTime(c.created_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
              <Pagination page={page} pageSize={25} total={ent.data.total} onPage={setPage} />
            </>
          )
        ) : hist.data ? (
          hist.data.items.length === 0 ? (
            <EmptyState title="No decisions yet" />
          ) : (
            <>
              <ul className="space-y-2">
                {hist.data.items.map((h) => (
                  <li key={h.id} className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <strong>{ACTION_TEXT[h.action] ?? h.action}</strong>
                      <span className="text-xs text-[var(--color-fg-muted)]">{formatDateTime(h.created_at)}</span>
                      <Link
                        className="ml-auto text-xs underline"
                        href={h.target_type === "entity_merge_candidate" ? `/review/entities/${h.target_id}` : `/review/memories/${h.target_id}`}
                      >
                        Open decision →
                      </Link>
                    </div>
                    {h.reason ? <p className="content-text mt-1 text-xs">Reason: {h.reason}</p> : null}
                  </li>
                ))}
              </ul>
              <Pagination page={page} pageSize={25} total={hist.data.total} onPage={setPage} />
            </>
          )
        ) : null}
      </div>
    </div>
  );
}
