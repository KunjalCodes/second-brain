"use client";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { confirmDistinct, confirmDuplicate, getMemoryReview, resolveConflict } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { MemoryDetail, MemoryReviewCandidateDetail } from "@/lib/api/types";
import { validityText } from "@/lib/format/dates";
import { MemoryStatusChip } from "@/components/ui/StatusChips";
import { Tag } from "@/components/ui/Chip";
import { Button } from "@/components/ui/Button";
import { ErrorState, LoadingRows } from "@/components/ui/States";
import { ProvenancePanel } from "@/components/provenance/ProvenancePanel";
import { DecisionDialog } from "./DecisionDialog";
import { KIND_TEXT } from "./ReviewQueue";

type Decision =
  | { kind: "duplicate" }
  | { kind: "distinct" }
  | { kind: "supersede"; winner: "a" | "b" }
  | { kind: "keep_both" }
  | { kind: "reject" };

function Side({ label, d }: { label: string; d: MemoryDetail }) {
  return (
    <div className="space-y-2 panel p-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">{label}</p>
      <p className="content-text">{d.memory.content}</p>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <MemoryStatusChip status={d.memory.status} />
        <span>{validityText(d.memory.valid_from, d.memory.valid_until)}</span>
      </div>
      <Link className="text-sm underline" href={`/memories/${d.memory.id}`}>Open memory →</Link>
      <ProvenancePanel memoryId={d.memory.id} />
    </div>
  );
}

function EvidenceList({ evidence }: { evidence: Record<string, unknown> }) {
  const entries = Object.entries(evidence ?? {});
  if (!entries.length) return null;
  return (
    <details className="text-xs">
      <summary className="cursor-pointer">Why the system flagged this</summary>
      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3">
        {entries.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="font-mono">{k}</dt>
            <dd className="content-text font-mono">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function consequence(d: Decision, c: MemoryReviewCandidateDetail) {
  const a = `“${c.memory_a.memory.content.slice(0, 80)}”`;
  const b = `“${c.memory_b.memory.content.slice(0, 80)}”`;
  switch (d.kind) {
    case "duplicate":
      return <p>Memory A {a} will be marked a <strong>duplicate of</strong> memory B {b}. A is kept, but hidden from answers in favour of B. Nothing is deleted.</p>;
    case "distinct":
      return <p>Both memories are kept as <strong>different</strong> memories. Nothing changes in the memory graph.</p>;
    case "supersede": {
      const [winner, loser] = d.winner === "a" ? [a, b] : [b, a];
      return <p>{winner} will <strong>replace</strong> {loser}. The replaced memory is kept as superseded history, not deleted.</p>;
    }
    case "keep_both":
      return <p>Both memories stay exactly as they are. The dispute is acknowledged and remains visible.</p>;
    case "reject":
      return <p>This is <strong>not a real conflict</strong>. Memory A {a}, which was flagged as disputed, becomes Active again.</p>;
  }
}

export function MemoryReviewDetail({ id }: { id: string }) {
  const queryClient = useQueryClient();
  const [decision, setDecision] = useState<Decision | null>(null);
  const q = useQuery({ queryKey: qk.reviewMemory(id), queryFn: ({ signal }) => getMemoryReview(id, signal) });
  const mutation = useMutation({
    mutationFn: async ({ d, reason }: { d: Decision; reason: string | null }) => {
      const c = q.data!;
      switch (d.kind) {
        case "duplicate":
          return confirmDuplicate(id, c.version, reason);
        case "distinct":
          return confirmDistinct(id, c.version, reason);
        case "supersede":
          return resolveConflict(id, {
            if_version: c.version,
            resolution: "supersede",
            winner_memory_id: d.winner === "a" ? c.memory_a.memory.id : c.memory_b.memory.id,
            reason,
          });
        case "keep_both":
          return resolveConflict(id, { if_version: c.version, resolution: "keep_both", reason });
        case "reject":
          return resolveConflict(id, { if_version: c.version, resolution: "reject", reason });
      }
    },
    onSuccess: async () => {
      // Refetch everything the decision can change -- never assume the outcome locally.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.reviewMemory(id) }),
        queryClient.invalidateQueries({ predicate: (qq) => String(qq.queryKey[0]).startsWith("review") }),
        queryClient.invalidateQueries({ queryKey: qk.memoriesAll }),
        queryClient.invalidateQueries({ predicate: (qq) => ["memory", "timeline", "provenance", "evidence", "graph-overview"].includes(String(qq.queryKey[0])) }),
      ]);
      setDecision(null);
    },
  });

  if (q.isPending) return <LoadingRows rows={3} label="Loading review item" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} notFound="This review item doesn't exist, or it isn't yours." />;
  const c = q.data;
  const actionable = c.status === "unreviewed";
  const isConflict = c.kind === "conflicting";

  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm"><Link className="underline" href="/review">← Review</Link></nav>
      <header className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">{KIND_TEXT[c.kind] ?? c.kind}</h1>
        <Tag>{actionable ? "Needs a decision" : `Decided: ${c.status.replace("_", " ")}`}</Tag>
        {c.score !== null && c.score !== undefined ? <span className="text-xs">similarity {c.score.toFixed(2)} (relative, not certainty)</span> : null}
      </header>
      <EvidenceList evidence={c.evidence as Record<string, unknown>} />
      <div className="grid gap-4 lg:grid-cols-2">
        <Side label="Memory A" d={c.memory_a} />
        <Side label="Memory B" d={c.memory_b} />
      </div>
      {actionable ? (
        <div className="flex flex-wrap gap-2 panel p-3">
          {isConflict ? (
            <>
              <Button onClick={() => setDecision({ kind: "supersede", winner: "a" })}>A replaces B</Button>
              <Button onClick={() => setDecision({ kind: "supersede", winner: "b" })}>B replaces A</Button>
              <Button onClick={() => setDecision({ kind: "keep_both" })}>Both are true</Button>
              <Button onClick={() => setDecision({ kind: "reject" })}>Not a real conflict</Button>
            </>
          ) : (
            <>
              <Button onClick={() => setDecision({ kind: "duplicate" })}>Same memory (A duplicates B)</Button>
              <Button onClick={() => setDecision({ kind: "distinct" })}>Different memories</Button>
            </>
          )}
        </div>
      ) : (
        <p className="text-sm text-[var(--color-fg-muted)]">This item has already been decided. See the decision history for details.</p>
      )}
      {decision ? (
        <DecisionDialog
          open
          title="Confirm decision"
          consequence={consequence(decision, c)}
          confirmLabel="Confirm"
          pending={mutation.isPending}
          error={mutation.error}
          onConfirm={(reason) => mutation.mutate({ d: decision, reason })}
          onClose={() => {
            setDecision(null);
            mutation.reset();
          }}
          onReload={() => {
            setDecision(null);
            mutation.reset();
            void q.refetch();
          }}
        />
      ) : null}
    </div>
  );
}
