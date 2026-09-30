"use client";
/**
 * Paths between two entities (Phase 15, M4/M11). Every step is a STORED
 * relationship with its asserting memories; a multi-step connection is labelled
 * "inferred from the graph". The ranking signals are heuristic and shown as such.
 */
import Link from "next/link";
import type { GraphPath, PathsResponse } from "@/lib/api/types";
import { EVIDENCE_BASIS, RELATION_KIND } from "@/lib/format/labels";
import { Tag } from "@/components/ui/Chip";
import { Button } from "@/components/ui/Button";
import { TemporalStateChip } from "@/components/ui/StatusChips";

const REASON: Record<string, string> = {
  same_entity: "These are the same entity.",
  not_connected_within_hops: "No stored relationships connect these two entities within the hop limit.",
  node_budget_exhausted: "The search reached its size limit before finding a connection. That does not mean there is none — try a more specific starting point.",
};

function PathView({ path, index, active, onShow }: { path: GraphPath; index: number; active: boolean; onShow: () => void }) {
  const basis = EVIDENCE_BASIS[path.basis];
  return (
    <li className={`space-y-2 rounded-md border p-2 ${active ? "border-[var(--color-accent)]" : "border-[var(--color-border)]"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold">Path {index + 1}</span>
        <Tag>{path.length} step{path.length === 1 ? "" : "s"}</Tag>
        <span title={basis?.explain}><Tag>{basis?.text ?? path.basis}</Tag></span>
        <Button className="ml-auto" onClick={onShow} aria-pressed={active}>{active ? "Shown on canvas" : "Show on canvas"}</Button>
      </div>
      <ol className="space-y-1">
        {path.steps.map((step, i) => (
          <li key={i} className="text-xs">
            <span className="font-medium">{step.from_name}</span>
            {step.relationships.map((r, j) => (
              <span key={j} className="mx-1 inline-flex flex-wrap items-center gap-1">
                <span className="font-mono">{r.direction === "forward" ? `—${r.predicate}→` : `←${r.predicate}—`}</span>
                <span title={RELATION_KIND[r.relation_kind]?.explain}><Tag>{RELATION_KIND[r.relation_kind]?.text ?? r.relation_kind}</Tag></span>
                <TemporalStateChip state={r.temporal_state} />
                {r.memory_ids.slice(0, 3).map((id) => (
                  <Link prefetch={false} key={id} className="underline" href={`/memories/${id}`}>memory</Link>
                ))}
                {r.memory_ids.length > 3 ? <span>+{r.memory_ids.length - 3} more</span> : null}
              </span>
            ))}
            <span className="font-medium">{step.to_name}</span>
          </li>
        ))}
      </ol>
      <details className="text-xs text-[var(--color-fg-muted)]">
        <summary className="cursor-pointer">How paths are ranked (heuristic)</summary>
        <p className="mt-1">
          Shorter, better-supported, currently-true and higher-confidence paths rank first. Score {path.score} from: length {path.signals.length},
          weakest step support {path.signals.min_support}, current fraction {path.signals.current_fraction}, weakest confidence {path.signals.min_confidence}.
        </p>
      </details>
    </li>
  );
}

export function PathPanel({
  result,
  fromLabel,
  toLabel,
  shown,
  onShow,
  onClose,
}: {
  result: PathsResponse;
  fromLabel: string;
  toLabel: string;
  shown: number | null;
  onShow: (index: number) => void;
  onClose: () => void;
}) {
  return (
    <section aria-label="Path between entities" className="space-y-2 text-sm">
      <div className="flex items-center justify-between gap-2">
        <p className="font-semibold">{fromLabel} ↔ {toLabel}</p>
        <Button onClick={onClose}>Close</Button>
      </div>
      {result.paths.length === 0 ? (
        <p role="status" className="rounded-md border border-dashed border-[var(--color-border)] p-3 text-xs">
          {REASON[result.reason ?? ""] ?? "No path found."}
        </p>
      ) : (
        <ul className="space-y-2">
          {result.paths.map((p, i) => (
            <PathView key={p.entity_ids.join(">")} path={p} index={i} active={shown === i} onShow={() => onShow(i)} />
          ))}
        </ul>
      )}
      {result.truncated && result.paths.length ? (
        <p className="text-xs text-[var(--color-warn)]">The search hit its size limit, so other connections may exist.</p>
      ) : null}
      <p className="text-xs text-[var(--color-fg-muted)]">Searched up to {result.max_hops} steps; looked at {result.explored_nodes} entities.</p>
    </section>
  );
}
