"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { listConversations, listDocuments, listEntityReview, listMemoryReview, listProjects, searchMemories } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { DOCUMENT_STATUS } from "@/lib/format/labels";
import { formatDate } from "@/lib/format/dates";
import { MemoryRow } from "@/components/memory/MemoryRow";
import { Section } from "@/components/ui/Surface";
import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/ui/States";

/** "What is happening in my second brain?" -- composed only from existing endpoints. */
export function Overview() {
  // The server's default search view: active and disputed memories, newest recorded first.
  const recent = useQuery({ queryKey: qk.memories({ overview: true }), queryFn: ({ signal }) => searchMemories({ limit: 6, offset: 0 }, signal) });
  const memReview = useQuery({ queryKey: qk.reviewMemories(1), queryFn: ({ signal }) => listMemoryReview(25, 0, signal) });
  const entReview = useQuery({ queryKey: qk.reviewEntities(1), queryFn: ({ signal }) => listEntityReview(25, 0, signal) });
  const docs = useQuery({ queryKey: qk.documents(1), queryFn: ({ signal }) => listDocuments(25, 0, signal) });
  const convs = useQuery({ queryKey: [...qk.conversations, "latest"], queryFn: ({ signal }) => listConversations(1, 0, signal) });
  const projects = useQuery({ queryKey: qk.projects, queryFn: ({ signal }) => listProjects(signal) });

  const processing = docs.data?.documents.filter((d) => !DOCUMENT_STATUS[d.status].terminal) ?? [];
  const reviewTotal = memReview.data && entReview.data ? memReview.data.total + entReview.data.total : undefined;
  const latest = convs.data?.items?.[0];

  return (
    <div className="space-y-5">
      <PageHeader title="Your second brain" />
      <p className="max-w-prose text-sm text-[var(--color-fg-muted)]">
        What the AI knows, how it connects, and where it came from. Start with a memory, follow its entities in the graph, and trace each fact back to its source.
      </p>

      <div className="grid gap-4 lg:grid-cols-[1.6fr_1fr]">
        <Section
          id="recent-h"
          title="Recently recorded"
          action={<Link className="text-sm underline" href="/memories">All memories →</Link>}
        >
          {recent.isPending ? (
            <LoadingRows rows={4} label="Loading recent memories" />
          ) : recent.isError ? (
            <ErrorState error={recent.error} onRetry={() => void recent.refetch()} />
          ) : recent.data.items.length === 0 ? (
            <EmptyState title="No memories yet">Memories are created from conversations and documents. Try asking something in “Ask”.</EmptyState>
          ) : (
            <ul className="space-y-2">
              {recent.data.items.map((m) => (
                <MemoryRow key={m.id} memory={m} />
              ))}
            </ul>
          )}
        </Section>

        <div className="space-y-4">
          <Section id="continue-h" title="Continue where you left off">
            {latest ? (
              <p className="text-sm">
                <Link className="underline" href={`/conversations/${latest.id}`}>{latest.title ?? "Latest conversation"}</Link>
                <span className="block text-xs text-[var(--color-fg-muted)]">{formatDate(latest.created_at)}</span>
              </p>
            ) : (
              <p className="text-sm text-[var(--color-fg-muted)]">No conversations yet.</p>
            )}
            <Link className="mt-2 inline-block text-sm underline" href="/ask">Ask your memory →</Link>
          </Section>

          <Section id="projects-h" title="Projects" action={<Link className="text-sm underline" href="/projects">All →</Link>}>
            {projects.isPending ? (
              <LoadingRows rows={2} label="Loading projects" />
            ) : projects.isError ? (
              <p className="text-sm text-[var(--color-fg-muted)]">Projects could not be loaded.</p>
            ) : projects.data.items.length === 0 ? (
              <p className="text-sm text-[var(--color-fg-muted)]">No projects yet. A project holds what a coding agent did in one codebase.</p>
            ) : (
              <ul className="space-y-1 text-sm">
                {projects.data.items.slice(0, 5).map((p) => (
                  <li key={p.id}>◆ <Link className="underline" href={`/projects/${p.id}`}>{p.name}</Link></li>
                ))}
              </ul>
            )}
          </Section>

          <Section id="attention-h" title="Needs attention">
            <ul className="space-y-1 text-sm">
              <li>
                <Link className="underline" href="/review">
                  {reviewTotal === undefined ? "Review queue" : `${reviewTotal} item${reviewTotal === 1 ? "" : "s"} waiting for review`}
                </Link>
              </li>
              <li>
                <Link className="underline" href="/documents">
                  {docs.isPending ? "Documents" : processing.length ? `${processing.length} document${processing.length === 1 ? "" : "s"} still processing` : "No documents processing"}
                </Link>
              </li>
            </ul>
          </Section>
        </div>
      </div>
    </div>
  );
}
