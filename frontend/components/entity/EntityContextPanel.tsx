"use client";
/**
 * "At a glance" for an entity (Phase 15, M5/M7): what the bounded, user-scoped
 * `GET /entities/{id}/context` adds to the relationship lists already on the page.
 * Related-entity ranking is HEURISTIC and its inputs are shown on hover.
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getEntityContext } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { EntityContext } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format/dates";
import { RELATION_KIND } from "@/lib/format/labels";
import { useTimeLens } from "@/lib/time/lens";
import { Tag } from "@/components/ui/Chip";
import { ErrorState, LoadingRows } from "@/components/ui/States";

const TIMELINE_SHOWN = 8;

export function EntityContextPanel({ entityId }: { entityId: string }) {
  const { asOf } = useTimeLens();
  const q = useQuery({
    queryKey: qk.entityContext(entityId, asOf),
    queryFn: ({ signal }) => getEntityContext(entityId, asOf ?? undefined, signal),
  });
  return (
    <section aria-labelledby="context-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 id="context-title" className="mb-2 font-semibold">At a glance</h2>
      {q.isPending ? (
        <LoadingRows rows={2} label="Loading context" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <Body ctx={q.data} />
      )}
    </section>
  );
}

function Body({ ctx }: { ctx: EntityContext }) {
  const stated = [...ctx.current_relationships, ...ctx.historical_relationships].filter((r) => r.relation_kind !== "association");
  const events = ctx.timeline.slice(-TIMELINE_SHOWN);
  return (
    <div className="space-y-4 text-sm">
      {ctx.merged_from.length ? (
        <p>
          <span aria-hidden="true">⧉ </span>Also recorded under: {ctx.merged_from.map((m) => m.name).join(", ")} (merged into this entity).
        </p>
      ) : null}
      {ctx.conflicting_memory_total ? (
        <p role="status" className="text-[var(--color-warn)]">
          {ctx.conflicting_memory_total} memor{ctx.conflicting_memory_total === 1 ? "y" : "ies"} about this entity {ctx.conflicting_memory_total === 1 ? "is" : "are"} disputed.
        </p>
      ) : null}

      <div>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">Most related entities</h3>
        {ctx.related_entities.length === 0 ? (
          <p className="text-[var(--color-fg-muted)]">No related entities recorded.</p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {ctx.related_entities.map((r) => (
              <li key={r.entity_id}>
                <Link
                  prefetch={false}
                  href={`/entities/${r.entity_id}`}
                  title={`Heuristic rank: ${Object.entries(r.signals).map(([k, v]) => `${k} ${v}`).join(", ")}`}
                  className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] px-2 py-1 hover:border-[var(--color-accent)]"
                >
                  <span className="font-medium">{r.name}</span>
                  <span className="text-xs text-[var(--color-fg-muted)]">{r.predicates.join(", ")}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-1 text-xs text-[var(--color-fg-muted)]">Ranked by a heuristic: currently related, how many memories say so, and how many ways they relate.</p>
      </div>

      {stated.length ? (
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">Stated causes and consequences</h3>
          <ul className="space-y-1">
            {stated.map((r, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs">{r.direction === "outgoing" ? `—${r.predicate}→` : `←${r.predicate}—`}</span>
                <Link prefetch={false} className="underline" href={`/entities/${r.other_entity_id}`}>{r.other_name}</Link>
                <span title={RELATION_KIND[r.relation_kind]?.explain}><Tag>{RELATION_KIND[r.relation_kind]?.text ?? r.relation_kind}</Tag></span>
                {r.memory_ids.slice(0, 2).map((id) => <Link prefetch={false} key={id} className="text-xs underline" href={`/memories/${id}`}>memory</Link>)}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">Only what a memory said. Two things happening one after the other is never treated as cause and effect.</p>
        </div>
      ) : null}

      {events.length ? (
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">When facts began and ended</h3>
          <ol className="space-y-1">
            {events.map((ev, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-2">
                <time className="text-xs text-[var(--color-fg-muted)]" dateTime={ev.at}>{formatDateTime(ev.at)}</time>
                <span className="text-xs">{ev.kind === "began" ? "began" : "ended"}</span>
                <Link prefetch={false} className="underline content-text" href={`/memories/${ev.memory_id}`}>{ev.content}</Link>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      {ctx.truncated_memories || ctx.truncated_relationships ? (
        <p className="text-xs text-[var(--color-warn)]">Showing the most relevant items only; {ctx.memory_total} memories mention this entity in total.</p>
      ) : null}
    </div>
  );
}
