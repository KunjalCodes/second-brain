"use client";
/** Memory detail (docs/PHASE10_PLAN.md §6). Everything shown comes from the backend as-is. */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getMemory, memoryTimeline } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { Entity, MemoryDetail, Relationship } from "@/lib/api/types";
import { CONFIDENCE_EXPLAIN, MEMORY_TYPE } from "@/lib/format/labels";
import { formatDateTime, validityText } from "@/lib/format/dates";
import { MemoryStatusChip, TemporalStateChip } from "@/components/ui/StatusChips";
import { Tag } from "@/components/ui/Chip";
import { Explain } from "@/components/ui/Explain";
import { ErrorState, LoadingRows } from "@/components/ui/States";
import { ProvenancePanel } from "@/components/provenance/ProvenancePanel";
import { LinkedMemory } from "./LinkedMemory";
import { RetractDialog } from "./RetractDialog";
import { EvolutionPanel } from "./EvolutionPanel";
import { WhyRemembered } from "./WhyRemembered";

function entityName(entities: Entity[], id: string): string {
  return entities.find((e) => e.id === id)?.name ?? "another entity";
}

function RelationshipRow({ rel, entities }: { rel: Relationship; entities: Entity[] }) {
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--color-border)] p-2 text-sm">
      <Link className="underline" href={`/entities/${rel.source_entity_id}`}>{entityName(entities, rel.source_entity_id)}</Link>
      <span className="font-mono text-xs">—{rel.predicate}→</span>
      <Link className="underline" href={`/entities/${rel.target_entity_id}`}>{entityName(entities, rel.target_entity_id)}</Link>
      <MemoryStatusChip status={rel.status} />
      <span className="text-xs text-[var(--color-fg-muted)]">{validityText(rel.valid_from, rel.valid_until)}</span>
      <Link className="ml-auto text-xs underline" href={`/graph?focus=${rel.source_entity_id}`}>Show in graph</Link>
    </li>
  );
}

function LinkGroup({ title, ids }: { title: string; ids: string[] }) {
  if (!ids.length) return null;
  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">{title}</h3>
      <div className="space-y-1">{ids.map((id) => <LinkedMemory key={id} id={id} />)}</div>
    </div>
  );
}

function Lifecycle({ d }: { d: MemoryDetail }) {
  const links = {
    supersedes: d.supersedes ?? [],
    supersededBy: d.superseded_by ?? [],
    conflicts: d.conflicts_with ?? [],
    duplicateOf: d.duplicate_of ?? [],
    duplicates: d.duplicates ?? [],
  };
  const any = Object.values(links).reduce((n, l) => n + l.length, 0);
  return (
    <section aria-labelledby="lifecycle-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 id="lifecycle-title" className="font-semibold">History & links</h2>
        <Link className="text-sm underline" href={`/memories/${d.memory.id}/timeline`}>Open timeline →</Link>
      </div>
      {any === 0 ? (
        <p className="text-sm text-[var(--color-fg-muted)]">This memory has not replaced, been replaced by, duplicated or disputed any other memory.</p>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          <LinkGroup title="Superseded by (newer)" ids={links.supersededBy} />
          <LinkGroup title="Supersedes (older)" ids={links.supersedes} />
          <LinkGroup title="Disputed with" ids={links.conflicts} />
          <LinkGroup title="Duplicate of (canonical)" ids={links.duplicateOf} />
          <LinkGroup title="Duplicates of this memory" ids={links.duplicates} />
        </div>
      )}
    </section>
  );
}

export function MemoryDetailView({ id }: { id: string }) {
  const query = useQuery({ queryKey: qk.memory(id), queryFn: ({ signal }) => getMemory(id, signal) });
  // Server-computed temporal state (relative to now) comes from the timeline slice.
  const timeline = useQuery({ queryKey: qk.timeline(id), queryFn: ({ signal }) => memoryTimeline(id, signal) });
  const serverState = timeline.data?.nodes?.find((n) => n.id === id)?.temporal_state ?? null;

  if (query.isPending) return <LoadingRows rows={4} label="Loading memory" />;
  if (query.isError)
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} notFound="This memory doesn't exist, or it isn't yours." />;

  const d = query.data;
  const m = d.memory;
  const entities = d.entities ?? [];
  const relationships = d.relationships ?? [];
  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm"><Link className="underline" href="/memories">← Memories</Link></nav>
      <article className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <h1 className="sr-only">Memory</h1>
        <p className="content-text text-base">{m.content}</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <MemoryStatusChip status={m.status} />
          {serverState ? <TemporalStateChip state={serverState} asOfLabel="now" /> : null}
          <Tag>{MEMORY_TYPE[m.memory_type].text}</Tag>
        </div>
        <dl className="mt-3 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
          <dt className="text-[var(--color-fg-muted)]">When it was true</dt>
          <dd>{validityText(m.valid_from, m.valid_until)}</dd>
          <dt className="text-[var(--color-fg-muted)]">Recorded</dt>
          <dd>{formatDateTime(m.created_at)}</dd>
          <dt className="text-[var(--color-fg-muted)]">Last updated</dt>
          <dd>{formatDateTime(m.updated_at)}</dd>
          <dt className="text-[var(--color-fg-muted)]">Extraction confidence</dt>
          <dd>
            {m.confidence.toFixed(2)} <Explain summary="what is this?">{CONFIDENCE_EXPLAIN}</Explain>
          </dd>
          {m.invalidation_reason ? (
            <>
              <dt className="text-[var(--color-fg-muted)]">Retraction reason</dt>
              <dd className="content-text">{m.invalidation_reason}</dd>
            </>
          ) : null}
        </dl>
        <Explain summary="“When it was true” vs “Recorded”">
          “When it was true” is the fact’s validity in the world. “Recorded” is when the AI learned it. A memory recorded in June can be about something true since January.
        </Explain>
        {m.status !== "invalid" ? <div className="mt-3"><RetractDialog memoryId={m.id} /></div> : null}
      </article>

      <section aria-labelledby="entities-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <h2 id="entities-title" className="mb-2 font-semibold">Entities mentioned</h2>
        {entities.length ? (
          <ul className="flex flex-wrap gap-2">
            {entities.map((e) => (
              <li key={e.id}>
                <Link href={`/entities/${e.id}`} className="inline-block rounded-full border border-[var(--color-border)] px-2 py-0.5 text-sm hover:border-[var(--color-accent)]">
                  {e.name} <span className="text-xs text-[var(--color-fg-muted)]">· {e.entity_type}</span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-[var(--color-fg-muted)]">No entities recorded for this memory.</p>
        )}
        <h2 className="mb-2 mt-4 font-semibold">Relationships this memory asserts</h2>
        {relationships.length ? (
          <ul className="space-y-1">{relationships.map((r) => <RelationshipRow key={r.id} rel={r} entities={entities} />)}</ul>
        ) : (
          <p className="text-sm text-[var(--color-fg-muted)]">This memory asserts no relationship between entities.</p>
        )}
      </section>

      <WhyRemembered memoryId={m.id} />
      <Lifecycle d={d} />
      <EvolutionPanel memoryId={m.id} />
      <ProvenancePanel memoryId={m.id} conflictsWith={d.conflicts_with ?? []} />

      <details className="text-xs text-[var(--color-fg-muted)]">
        <summary className="cursor-pointer">Technical details</summary>
        <p className="mt-1 font-mono">memory id: {m.id}</p>
        {relationships.map((r) => <p key={r.id} className="font-mono">relationship id: {r.id}</p>)}
      </details>
    </div>
  );
}
