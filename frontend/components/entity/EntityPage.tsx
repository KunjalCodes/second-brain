"use client";
/**
 * Entity page (PRODUCT.md §8, docs/PHASE10_PLAN.md §5/§8). Merge resolution is
 * the backend's: `GET /entities/{id}` answers with the canonical entity; the
 * UI only notices that the returned id differs from the one asked for.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { getEntity, getEntityHistory, getEntityMemories } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { RelationshipView } from "@/lib/api/types";
import { useTimeLens } from "@/lib/time/lens";
import { formatDateTime, validityText } from "@/lib/format/dates";
import { MemoryStatusChip } from "@/components/ui/StatusChips";
import { Tag } from "@/components/ui/Chip";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, LoadingRows, NotTimeFiltered } from "@/components/ui/States";
import { MemoryRow } from "@/components/memory/MemoryRow";

function RelList({ items, selfId }: { items: RelationshipView[]; selfId: string }) {
  if (!items.length) return <p className="text-sm text-[var(--color-fg-muted)]">None.</p>;
  return (
    <ul className="space-y-1">
      {items.map(({ relationship: r, source_entity: s, target_entity: t }) => (
        <li key={r.id} className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--color-border)] p-2 text-sm">
          {s.id === selfId ? <span className="font-medium">{s.name}</span> : <Link className="underline" href={`/entities/${s.id}`}>{s.name}</Link>}
          <span className="font-mono text-xs">—{r.predicate}→</span>
          {t.id === selfId ? <span className="font-medium">{t.name}</span> : <Link className="underline" href={`/entities/${t.id}`}>{t.name}</Link>}
          <MemoryStatusChip status={r.status} />
          <span className="text-xs text-[var(--color-fg-muted)]">{validityText(r.valid_from, r.valid_until)}</span>
          {r.memory_id ? <Link className="ml-auto text-xs underline" href={`/memories/${r.memory_id}`}>asserting memory</Link> : null}
        </li>
      ))}
    </ul>
  );
}

const PAGE = 25;

export function EntityPage({ id }: { id: string }) {
  const router = useRouter();
  const { asOf } = useTimeLens();
  const [page, setPage] = useState(1);
  const entity = useQuery({ queryKey: qk.entity(id), queryFn: ({ signal }) => getEntity(id, signal) });
  const canonicalId = entity.data?.entity.id;
  const redirected = Boolean(canonicalId && canonicalId !== id);

  useEffect(() => {
    if (redirected && canonicalId) router.replace(`/entities/${canonicalId}?merged_from=1`, { scroll: false });
  }, [redirected, canonicalId, router]);

  const effectiveId = canonicalId ?? id;
  const history = useQuery({
    queryKey: qk.entityHistory(effectiveId, asOf ?? undefined),
    queryFn: ({ signal }) => getEntityHistory(effectiveId, asOf ?? undefined, signal),
    enabled: Boolean(canonicalId),
  });
  const memories = useQuery({
    queryKey: qk.entityMemories(effectiveId, page),
    queryFn: ({ signal }) => getEntityMemories(effectiveId, PAGE, (page - 1) * PAGE, signal),
    enabled: Boolean(canonicalId),
  });

  if (entity.isPending) return <LoadingRows rows={3} label="Loading entity" />;
  if (entity.isError)
    return <ErrorState error={entity.error} onRetry={() => void entity.refetch()} notFound="This entity doesn't exist, or it isn't yours." />;

  const e = entity.data.entity;
  const mergedNotice =
    redirected || (typeof window !== "undefined" && new URLSearchParams(window.location.search).get("merged_from") === "1");

  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm"><Link className="underline" href="/entities">← Entities</Link></nav>
      {mergedNotice ? (
        <p role="status" className="rounded-md border border-[var(--color-info)] px-3 py-2 text-sm">
          <span aria-hidden="true">⧉ </span>The entity you opened was merged into <strong>{e.name}</strong>. Showing the merged entity.
        </p>
      ) : null}
      <header className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <h1 className="text-xl font-semibold">{e.name}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <Tag>{e.entity_type}</Tag>
          <span className="text-[var(--color-fg-muted)]">
            {entity.data.memory_count ?? 0} memories · {entity.data.relationship_count ?? 0} relationships · first recorded {formatDateTime(e.created_at)}
          </span>
        </div>
        {e.aliases?.length ? <p className="mt-1 text-sm">Also known as: {e.aliases.join(", ")}</p> : null}
        <div className="mt-3 flex flex-wrap gap-2 text-sm">
          <Link className="underline" href={`/graph?focus=${e.id}`}>Explore in graph →</Link>
          <Link className="underline" href={`/memories?entity=${encodeURIComponent(e.name)}`}>Filter memories by this entity →</Link>
        </div>
      </header>

      <section aria-labelledby="state-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <h2 id="state-title" className="mb-1 font-semibold">
          Relationships {asOf ? `as of ${formatDateTime(asOf)}` : "now"}
        </h2>
        <p className="mb-3 text-xs text-[var(--color-fg-muted)]">
          “True then” means the relationship’s validity window contains that instant; “no longer true” means it had ended by then. Computed by the server.
        </p>
        {history.isPending ? (
          <LoadingRows rows={2} label="Loading relationships" />
        ) : history.isError ? (
          <ErrorState error={history.error} onRetry={() => void history.refetch()} />
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <h3 className="mb-1 text-sm font-semibold">{asOf ? "True then" : "True now"}</h3>
              <RelList items={history.data.current ?? []} selfId={e.id} />
            </div>
            <div>
              <h3 className="mb-1 text-sm font-semibold">No longer true (historical)</h3>
              <RelList items={history.data.historical ?? []} selfId={e.id} />
            </div>
          </div>
        )}
      </section>

      <section aria-labelledby="mem-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h2 id="mem-title" className="font-semibold">Memories mentioning {e.name}</h2>
          <NotTimeFiltered />
        </div>
        {memories.isPending ? (
          <LoadingRows rows={3} label="Loading memories" />
        ) : memories.isError ? (
          <ErrorState error={memories.error} onRetry={() => void memories.refetch()} />
        ) : memories.data.length === 0 ? (
          <EmptyState title={page === 1 ? "No memories mention this entity" : "No more memories"} />
        ) : (
          <ul className="space-y-2">{memories.data.map((m) => <MemoryRow key={m.id} memory={m} />)}</ul>
        )}
        <div className="mt-3 flex gap-2">
          <Button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>← Newer</Button>
          <Button disabled={!memories.data || memories.data.length < PAGE} onClick={() => setPage((p) => p + 1)}>Older →</Button>
        </div>
      </section>
    </div>
  );
}
