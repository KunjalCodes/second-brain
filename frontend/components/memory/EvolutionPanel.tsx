"use client";
/**
 * How this knowledge evolved (Phase 15, M6): the memory's bounded history chain
 * as typed events. Every event is a stored memory, link, extraction decision or
 * review — nothing is overwritten and nothing is implied.
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getMemoryEvolution } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { formatDateTime } from "@/lib/format/dates";
import { EVOLUTION_KIND } from "@/lib/format/labels";
import { Tag } from "@/components/ui/Chip";
import { ErrorState, LoadingRows } from "@/components/ui/States";
import { MemoryStatusChip } from "@/components/ui/StatusChips";

export function EvolutionPanel({ memoryId }: { memoryId: string }) {
  const q = useQuery({ queryKey: qk.evolution(memoryId), queryFn: ({ signal }) => getMemoryEvolution(memoryId, signal) });
  return (
    <section aria-labelledby="evolution-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 id="evolution-title" className="mb-2 font-semibold">How this knowledge evolved</h2>
      {q.isPending ? (
        <LoadingRows rows={2} label="Loading history" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : q.data.events.length <= 1 && q.data.events[0]?.kind === "new_fact" ? (
        <p className="text-sm text-[var(--color-fg-muted)]">Recorded once. Nothing has replaced, disputed, merged or restated it.</p>
      ) : (
        <ol className="space-y-2">
          {q.data.events.map((ev, i) => (
            <li key={i} className="rounded-md border border-[var(--color-border)] p-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Tag>{EVOLUTION_KIND[ev.kind] ?? ev.kind}</Tag>
                <time className="text-xs text-[var(--color-fg-muted)]" dateTime={ev.at}>{formatDateTime(ev.at)}</time>
                <MemoryStatusChip status={ev.status} />
                {ev.memory_id === memoryId ? <span className="text-xs">(this memory)</span> : null}
              </div>
              <p className="content-text mt-1">
                <Link prefetch={false} className="underline" href={`/memories/${ev.memory_id}`}>{ev.content}</Link>
              </p>
              {ev.detail.toLowerCase() !== (EVOLUTION_KIND[ev.kind] ?? "").toLowerCase() ? (
                <p className="text-xs text-[var(--color-fg-muted)]">{ev.detail}</p>
              ) : null}
              {ev.related_memory_ids.length ? (
                <p className="text-xs">
                  Related:{" "}
                  {ev.related_memory_ids.map((id) => <Link prefetch={false} key={id} className="mr-2 underline" href={`/memories/${id}`}>memory</Link>)}
                </p>
              ) : null}
            </li>
          ))}
        </ol>
      )}
      {q.data?.truncated ? <p className="mt-2 text-xs text-[var(--color-warn)]">Showing the first events only.</p> : null}
    </section>
  );
}
