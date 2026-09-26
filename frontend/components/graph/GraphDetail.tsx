"use client";
import Link from "next/link";
import type { GraphScopeParams } from "@/lib/api/endpoints";
import type { GraphSliceEdge, GraphSliceNode, MemoryStatus } from "@/lib/api/types";
import { validityText } from "@/lib/format/dates";
import { MemoryStatusChip, TemporalStateChip } from "@/components/ui/StatusChips";
import { Tag } from "@/components/ui/Chip";
import { Button } from "@/components/ui/Button";
import { EvidencePanel } from "./EvidencePanel";

const EDGE_KIND_TEXT: Record<string, string> = {
  mentions: "This memory mentions this entity.",
  supersedes: "The newer memory replaced (superseded) the older one. Both are kept.",
  conflicts_with: "These memories dispute each other. Neither is preferred.",
  duplicate_of: "A confirmed duplicate of the canonical memory.",
  derived_from: "The memory was derived from this source.",
};

export function NodeDetail({
  node,
  onExpand,
  onReroot,
  expandDisabled,
}: {
  node: GraphSliceNode;
  onExpand: () => void;
  onReroot: () => void;
  expandDisabled: boolean;
}) {
  if (node.kind === "memory") {
    return (
      <div className="space-y-2 text-sm">
        <p className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">Memory</p>
        <p className="content-text">{node.label}</p>
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
      <p className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">Entity</p>
      <p className="text-base font-semibold">{node.label}</p>
      <div className="flex flex-wrap gap-2">
        {node.entity_type ? <Tag>{node.entity_type}</Tag> : null}
        {node.hops != null ? <Tag>{node.hops === 0 ? "focus" : `${node.hops} hop${node.hops === 1 ? "" : "s"} away`}</Tag> : null}
        {node.merged_from_count ? <Tag>⧉ {node.merged_from_count} merged into it</Tag> : null}
      </div>
      {node.aliases?.length ? <p className="text-xs">Also known as: {node.aliases.join(", ")}</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button onClick={onExpand} disabled={expandDisabled}>Expand neighbours</Button>
        <Button onClick={onReroot}>Focus here (re-root)</Button>
      </div>
      <Link className="underline" href={`/entities/${node.id}`}>Open entity page →</Link>
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
