"use client";
import Link from "next/link";
import type { GraphScopeParams } from "@/lib/api/endpoints";
import type { GraphSliceEdge, GraphSliceNode, MemoryStatus } from "@/lib/api/types";
import { formatDateTime, validityText } from "@/lib/format/dates";
import { MemoryStatusChip, TemporalStateChip } from "@/components/ui/StatusChips";
import { useQuery } from "@tanstack/react-query";
import { getEntityContext } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { useTimeLens } from "@/lib/time/lens";
import { Tag } from "@/components/ui/Chip";
import { KindPill } from "@/components/ui/Surface";
import { Button } from "@/components/ui/Button";
import { EvidencePanel } from "./EvidencePanel";
import { ExplainPanel } from "./ExplainPanel";

const EDGE_KIND_TEXT: Record<string, string> = {
  mentions: "This memory mentions this entity.",
  supersedes: "The newer memory replaced (superseded) the older one. Both are kept.",
  conflicts_with: "These memories dispute each other. Neither is preferred.",
  duplicate_of: "A confirmed duplicate of the canonical memory.",
  derived_from: "The memory was derived from this source.",
};

/** What the existing, bounded `GET /entities/{id}/context` knows about this entity. */
function EntityKnowledge({ entityId, onFocus }: { entityId: string; onFocus: (id: string) => void }) {
  const { asOf } = useTimeLens();
  const q = useQuery({
    queryKey: qk.entityContext(entityId, asOf),
    queryFn: ({ signal }) => getEntityContext(entityId, asOf ?? undefined, signal),
  });
  if (q.isPending) return <p role="status" className="text-xs text-[var(--color-fg-muted)]">Loading what is known…</p>;
  if (q.isError) return <p className="text-xs text-[var(--color-fg-muted)]">Related knowledge could not be loaded.</p>;
  const ctx = q.data;
  const recent = ctx.timeline.slice(-4).reverse();
  const sources = [...new Set(ctx.timeline.map((e) => e.memory_id))].slice(0, 4);
  return (
    <div className="space-y-3 border-t border-[var(--color-border)] pt-3">
      <div>
        <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">Related knowledge</h3>
        {ctx.related_entities.length === 0 ? (
          <p className="text-xs text-[var(--color-fg-muted)]">No related entities recorded.</p>
        ) : (
          <ul className="space-y-1">
            {ctx.related_entities.slice(0, 6).map((r) => (
              <li key={r.entity_id} className="flex items-baseline justify-between gap-2">
                <button type="button" className="text-left underline" onClick={() => onFocus(r.entity_id)}>
                  {r.name}
                </button>
                <span className="text-xs text-[var(--color-fg-muted)]">{r.predicates.join(", ")}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">Recent history</h3>
        {recent.length === 0 ? (
          <p className="text-xs text-[var(--color-fg-muted)]">No dated facts recorded.</p>
        ) : (
          <ul className="space-y-1">
            {recent.map((ev, i) => (
              <li key={i} className="text-xs">
                <span className="text-[var(--color-fg-muted)]">{formatDateTime(ev.at)} · {ev.kind === "began" ? "began" : "ended"}</span>{" "}
                <Link className="content-text underline" href={`/memories/${ev.memory_id}`}>{ev.content}</Link>
              </li>
            ))}
          </ul>
        )}
      </div>
      {sources.length ? (
        <div>
          <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">Evidence</h3>
          <p className="text-xs">
            {sources.map((id, i) => (
              <span key={id}>{i ? " · " : ""}<Link className="underline" href={`/memories/${id}`}>memory {i + 1}</Link></span>
            ))}
            <span className="text-[var(--color-fg-muted)]"> — each opens “why the AI remembers this” with its source.</span>
          </p>
        </div>
      ) : null}
      <p className="text-xs text-[var(--color-fg-muted)]">{ctx.memory_total} memories mention this entity.</p>
    </div>
  );
}

export function NodeDetail({
  node,
  onExpand,
  onReroot,
  expandDisabled,
  onFindPath,
  onCollapse,
  onFocusEntity,
}: {
  node: GraphSliceNode;
  /** Select a related entity on the canvas if it is loaded, otherwise re-root on it. */
  onFocusEntity?: (id: string) => void;
  onExpand: () => void;
  onReroot: () => void;
  expandDisabled: boolean;
  /** Phase 15: ask how this entity connects to another. */
  onFindPath?: () => void;
  /** Phase 15: undo this node's expansion (only offered when it has one). */
  onCollapse?: () => void;
}) {
  if (node.kind === "memory") {
    return (
      <div className="space-y-2 text-sm">
        <KindPill kind="memory" />
        <p className="content-text reading">{node.label}</p>
        <div className="flex flex-wrap gap-2">
          {node.status ? <MemoryStatusChip status={node.status as MemoryStatus} /> : null}
          {node.temporal_state ? <TemporalStateChip state={node.temporal_state} /> : null}
        </div>
        <p className="text-xs text-[var(--color-fg-muted)]">{validityText(node.valid_from, node.valid_until)}</p>
        <Link className="underline" href={`/memories/${node.id}`}>Open memory & provenance →</Link>
      </div>
    );
  }
  return (
    <div className="space-y-2 text-sm">
      <KindPill kind="entity" entityType={node.entity_type} />
      <p className="reading text-xl font-semibold">{node.label}</p>
      <div className="flex flex-wrap gap-2">
        {node.hops != null ? <Tag>{node.hops === 0 ? "focus" : `${node.hops} hop${node.hops === 1 ? "" : "s"} away`}</Tag> : null}
        {node.merged_from_count ? <Tag>⧉ {node.merged_from_count} merged into it</Tag> : null}
      </div>
      {node.aliases?.length ? <p className="text-xs">Also known as: {node.aliases.join(", ")}</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button onClick={onExpand} disabled={expandDisabled}>Expand neighbours</Button>
        <Button onClick={onReroot}>Focus here (re-root)</Button>
        {onFindPath ? <Button onClick={onFindPath}>Find path to…</Button> : null}
        {onCollapse ? <Button onClick={onCollapse}>Collapse neighbours</Button> : null}
      </div>
      <Link className="underline" href={`/entities/${node.id}`}>Open entity page →</Link>
      {onFocusEntity ? <EntityKnowledge entityId={node.id} onFocus={onFocusEntity} /> : null}
    </div>
  );
}

export function EdgeDetail({
  edge,
  nodeLabel,
  scope,
}: {
  edge: GraphSliceEdge;
  nodeLabel: (id: string) => string;
  scope: GraphScopeParams;
}) {
  const header =
    edge.kind === "relates_to" ? (
      <p className="text-base font-semibold">
        {nodeLabel(edge.source)} <span className="font-mono text-sm">—{edge.predicate}→</span> {nodeLabel(edge.target)}
      </p>
    ) : (
      <p>{EDGE_KIND_TEXT[edge.kind] ?? edge.kind}</p>
    );
  return (
    <div className="space-y-3 text-sm">
      <p className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        {edge.kind === "relates_to" ? "Relationship" : "Link"}
      </p>
      {header}
      {edge.kind === "relates_to" ? (
        <>
          <div className="flex flex-wrap gap-2">
            {edge.status ? <MemoryStatusChip status={edge.status} /> : null}
            {edge.temporal_state ? <TemporalStateChip state={edge.temporal_state} /> : null}
            <Tag>
              {edge.assertion_count ?? 1} assertion{edge.assertion_count === 1 ? "" : "s"}
            </Tag>
          </div>
          <p className="text-xs text-[var(--color-fg-muted)]">
            {validityText(edge.valid_from, edge.valid_until)} (earliest start / latest end among the aggregated assertions)
          </p>
          {edge.predicate ? (
            <ExplainPanel source={edge.source} target={edge.target} predicate={edge.predicate} scope={scope} />
          ) : null}
          {edge.predicate ? <EvidencePanel edge={edge} scope={scope} /> : null}
        </>
      ) : (
        <div className="flex flex-wrap gap-3">
          <Link className="underline" href={`/memories/${edge.source}`}>Open source memory</Link>
          {edge.kind !== "mentions" ? <Link className="underline" href={`/memories/${edge.target}`}>Open target memory</Link> : null}
        </div>
      )}
    </div>
  );
}
