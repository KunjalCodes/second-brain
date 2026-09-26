"use client";
/**
 * A memory's supersession / duplicate / dispute chain from
 * `GET /graph/memories/{id}/timeline` (docs/PHASE10_PLAN.md §8). Order,
 * statuses and temporal states are the server's; the bars below are only a
 * drawing of each memory's valid-time window on a shared axis.
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { memoryTimeline } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { GraphSlice, GraphSliceEdge, GraphSliceNode, MemoryStatus } from "@/lib/api/types";
import { formatDate, formatDateTime, validityText } from "@/lib/format/dates";
import { MemoryStatusChip, TemporalStateChip } from "@/components/ui/StatusChips";
import { ErrorState, LoadingRows, TruncationBanner } from "@/components/ui/States";

const EDGE_TEXT: Record<string, string> = {
  supersedes: "supersedes",
  duplicate_of: "is a duplicate of",
  conflicts_with: "disputes",
};

export interface Bar {
  leftPct: number;
  widthPct: number;
  openStart: boolean;
  openEnd: boolean;
}

/** Pure geometry: place [from, until) on an axis spanning all windows and the server's "now". */
export function computeBars(slice: GraphSlice): Map<string, Bar> {
  const nodes = slice.nodes ?? [];
  const t = (iso: string | null | undefined) => (iso ? new Date(iso).getTime() : null);
  const points: number[] = [new Date(slice.as_of).getTime()];
  for (const n of nodes) {
    for (const v of [t(n.valid_from), t(n.valid_until), t(n.created_at)]) if (v !== null) points.push(v);
  }
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = Math.max(max - min, 1);
  const bars = new Map<string, Bar>();
  for (const n of nodes) {
    const from = t(n.valid_from) ?? min;
    const until = t(n.valid_until) ?? max;
    bars.set(n.id, {
      leftPct: ((from - min) / span) * 100,
      widthPct: Math.max(((until - from) / span) * 100, 0.8),
      openStart: n.valid_from == null,
      openEnd: n.valid_until == null,
    });
  }
  return bars;
}

function linksFor(node: GraphSliceNode, edges: GraphSliceEdge[], labels: Map<string, string>) {
  return edges
    .filter((e) => e.source === node.id || e.target === node.id)
    .map((e) => {
      const outgoing = e.source === node.id;
      const other = outgoing ? e.target : e.source;
      const verb = EDGE_TEXT[e.kind] ?? e.kind;
      const text = outgoing ? `${verb} →` : e.kind === "conflicts_with" ? "disputed by ←" : `← ${verb} this`;
      return { key: `${e.id}-${node.id}`, text, other, otherLabel: labels.get(other) ?? "memory" };
    });
}

export function MemoryTimeline({ id }: { id: string }) {
  const q = useQuery({ queryKey: qk.timeline(id), queryFn: ({ signal }) => memoryTimeline(id, signal) });
  if (q.isPending) return <LoadingRows rows={3} label="Loading timeline" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} notFound="This memory doesn't exist, or it isn't yours." />;
  const slice = q.data;
  const nodes = slice.nodes ?? [];
  const edges = slice.edges ?? [];
  const bars = computeBars(slice);
  const labels = new Map(nodes.map((n) => [n.id, n.label]));

  return (
    <div className="space-y-3">
      <p className="text-sm text-[var(--color-fg-muted)]">
        Oldest first. Bars show when each memory was <em>true in the world</em> (valid time), not when it was recorded. Temporal states are relative to {formatDateTime(slice.as_of)}.
      </p>
      {slice.truncated_nodes ? (
        <TruncationBanner>
          The chain continues beyond {slice.budget.max_nodes} memories. Open the oldest memory shown to continue.
        </TruncationBanner>
      ) : null}
      <ol className="space-y-2" aria-label="Memory timeline">
        {nodes.map((n) => {
          const bar = bars.get(n.id)!;
          return (
            <li
              key={n.id}
              aria-current={n.id === id ? "true" : undefined}
              className={`rounded-lg border bg-[var(--color-surface)] p-3 ${n.id === id ? "border-[var(--color-accent)]" : "border-[var(--color-border)]"}`}
            >
              <div className="flex flex-wrap items-center gap-2">
                {n.status ? <MemoryStatusChip status={n.status as MemoryStatus} /> : null}
                {n.temporal_state ? <TemporalStateChip state={n.temporal_state} /> : null}
                <span className="text-xs text-[var(--color-fg-muted)]">
                  {validityText(n.valid_from, n.valid_until)} · Recorded {formatDate(n.created_at)}
                </span>
              </div>
              <Link href={`/memories/${n.id}`} className="content-text mt-1 block underline-offset-2 hover:underline">
                {n.label}
              </Link>
              <div className="relative mt-2 h-2 rounded bg-[var(--color-surface-2)]" aria-hidden="true">
                <div
                  className="absolute top-0 h-2 rounded bg-[var(--color-accent)] opacity-70"
                  style={{ left: `${bar.leftPct}%`, width: `${bar.widthPct}%` }}
                />
              </div>
              <ul className="mt-2 space-y-0.5 text-xs">
                {linksFor(n, edges, labels).map((l) => (
                  <li key={l.key}>
                    <span className="text-[var(--color-fg-muted)]">{l.text}</span>{" "}
                    <Link className="underline" href={`/memories/${l.other}`}>
                      {l.otherLabel.slice(0, 80)}
                    </Link>
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
