"use client";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { approveMerge, getEntityReview, rejectMerge, reverseMerge } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { EntityDetail } from "@/lib/api/types";
import { Tag } from "@/components/ui/Chip";
import { Button } from "@/components/ui/Button";
import { ErrorState, LoadingRows } from "@/components/ui/States";
import { DecisionDialog } from "./DecisionDialog";

type Decision = { kind: "merge"; canonical: "a" | "b" } | { kind: "reject" } | { kind: "reverse" };

function EntitySide({ label, d }: { label: string; d: EntityDetail | null | undefined }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3 text-sm">
      <p className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">{label}</p>
      {d ? (
        <>
          <p className="mt-1 text-base font-semibold">{d.entity.name}</p>
          <div className="mt-1 flex flex-wrap gap-2 text-xs">
            <Tag>{d.entity.entity_type}</Tag>
            <span>{d.memory_count ?? 0} memories · {d.relationship_count ?? 0} relationships</span>
          </div>
          {d.entity.aliases?.length ? <p className="mt-1 text-xs">Also known as: {d.entity.aliases.join(", ")}</p> : null}
          <Link className="mt-2 inline-block underline" href={`/entities/${d.entity.id}`}>Open entity →</Link>
        </>
      ) : (
        <p className="mt-1 text-[var(--color-fg-muted)]">This entity is no longer available.</p>
      )}
    </div>
  );
}

export function EntityReviewDetail({ id }: { id: string }) {
  const queryClient = useQueryClient();
  const [decision, setDecision] = useState<Decision | null>(null);
  const q = useQuery({ queryKey: qk.reviewEntity(id), queryFn: ({ signal }) => getEntityReview(id, signal) });
  const mutation = useMutation({
    mutationFn: ({ d, reason }: { d: Decision; reason: string | null }) => {
      const c = q.data!;
      if (d.kind === "merge") {
        const canonical = d.canonical === "a" ? c.entity_a!.entity.id : c.entity_b!.entity.id;
        return approveMerge(id, c.version, canonical, reason);
      }
      if (d.kind === "reject") return rejectMerge(id, c.version, reason);
      return reverseMerge(id, c.version, reason);
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.reviewEntity(id) }),
        queryClient.invalidateQueries({ predicate: (qq) => String(qq.queryKey[0]).startsWith("review") }),
        queryClient.invalidateQueries({ predicate: (qq) => ["entity", "graph", "evidence", "memory", "provenance"].some((p) => String(qq.queryKey[0]).startsWith(p)) }),
      ]);
      setDecision(null);
    },
  });
  if (q.isPending) return <LoadingRows rows={3} label="Loading review item" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} notFound="This review item doesn't exist, or it isn't yours." />;
  const c = q.data;
  const nameA = c.entity_a?.entity.name ?? "A";
  const nameB = c.entity_b?.entity.name ?? "B";
  const consequence = (d: Decision) =>
    d.kind === "merge" ? (
      <p>
        <strong>{d.canonical === "a" ? nameB : nameA}</strong> will be merged into <strong>{d.canonical === "a" ? nameA : nameB}</strong>. Its relationships are copied to the kept entity and its name becomes an alias. Its old id keeps resolving to the kept entity. This can be undone later from the decision history.
      </p>
    ) : d.kind === "reject" ? (
      <p>The two entities are kept <strong>separate</strong>. Nothing changes in the memory graph.</p>
    ) : (
      <p>The merge will be <strong>undone</strong>: the copied relationships and the redirect are removed. The alias added during the merge is not removed.</p>
    );
  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm"><Link className="underline" href="/review">← Review</Link></nav>
      <header className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Possible same entity</h1>
        <Tag>{c.status === "unreviewed" ? "Needs a decision" : `Status: ${c.status}`}</Tag>
        {c.score !== null && c.score !== undefined ? <span className="text-xs">similarity {c.score.toFixed(2)} (relative)</span> : null}
      </header>
      <div className="grid gap-4 md:grid-cols-2">
        <EntitySide label="Entity A" d={c.entity_a} />
        <EntitySide label="Entity B" d={c.entity_b} />
      </div>
      {c.status === "unreviewed" && c.entity_a && c.entity_b ? (
        <div className="flex flex-wrap gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
          <Button onClick={() => setDecision({ kind: "merge", canonical: "a" })}>Merge into {nameA}</Button>
          <Button onClick={() => setDecision({ kind: "merge", canonical: "b" })}>Merge into {nameB}</Button>
          <Button onClick={() => setDecision({ kind: "reject" })}>Keep separate</Button>
        </div>
      ) : c.status === "merged" ? (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
          <Button onClick={() => setDecision({ kind: "reverse" })}>Undo this merge</Button>
        </div>
      ) : null}
      {decision ? (
        <DecisionDialog
          open
          title="Confirm decision"
          consequence={consequence(decision)}
          confirmLabel="Confirm"
          pending={mutation.isPending}
          error={mutation.error}
          onConfirm={(reason) => mutation.mutate({ d: decision, reason })}
          onClose={() => { setDecision(null); mutation.reset(); }}
          onReload={() => { setDecision(null); mutation.reset(); void q.refetch(); }}
        />
      ) : null}
    </div>
  );
}
