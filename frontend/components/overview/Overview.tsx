"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { listDocuments, listEntityReview, listMemoryReview, searchMemories } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { DOCUMENT_STATUS } from "@/lib/format/labels";
import { MemoryRow } from "@/components/memory/MemoryRow";
import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/ui/States";

function Stat({ label, value, href, pending }: { label: string; value: number | string | undefined; href: string; pending: boolean }) {
  return (
    <Link href={href} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3 hover:border-[var(--color-accent)]">
      <p className="text-xs text-[var(--color-fg-muted)]">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{pending ? "…" : (value ?? "—")}</p>
    </Link>
  );
}

export function Overview() {
  // The server's default search view: active and disputed memories, newest recorded first.
  const recent = useQuery({ queryKey: qk.memories({ overview: true }), queryFn: ({ signal }) => searchMemories({ limit: 8, offset: 0 }, signal) });
  const memReview = useQuery({ queryKey: qk.reviewMemories(1), queryFn: ({ signal }) => listMemoryReview(25, 0, signal) });
  const entReview = useQuery({ queryKey: qk.reviewEntities(1), queryFn: ({ signal }) => listEntityReview(25, 0, signal) });
  const docs = useQuery({ queryKey: qk.documents(1), queryFn: ({ signal }) => listDocuments(25, 0, signal) });
  const processing = docs.data?.documents.filter((d) => !DOCUMENT_STATUS[d.status].terminal).length;
  const reviewTotal = memReview.data && entReview.data ? memReview.data.total + entReview.data.total : undefined;

  return (
    <div className="space-y-6">
      <PageHeader title="Overview" />
      <section aria-label="Summary" className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Stat label="Memories in the default view (active + disputed)" value={recent.data?.total} href="/memories" pending={recent.isPending} />
        <Stat label="Waiting for review" value={reviewTotal} href="/review" pending={memReview.isPending || entReview.isPending} />
        <Stat label="Documents still processing (latest 25)" value={processing} href="/documents" pending={docs.isPending} />
      </section>
      <section aria-labelledby="recent-h" className="space-y-2">
        <h2 id="recent-h" className="text-base font-semibold">Recently recorded</h2>
        {recent.isPending ? (
          <LoadingRows rows={4} label="Loading recent memories" />
        ) : recent.isError ? (
          <ErrorState error={recent.error} onRetry={() => void recent.refetch()} />
        ) : recent.data.items.length === 0 ? (
          <EmptyState title="No memories yet">Memories are created from conversations and documents.</EmptyState>
        ) : (
          <ul className="space-y-2">
            {recent.data.items.map((m) => (
              <MemoryRow key={m.id} memory={m} />
            ))}
          </ul>
        )}
        <Link className="text-sm underline" href="/memories">All memories →</Link>
      </section>
    </div>
  );
}
