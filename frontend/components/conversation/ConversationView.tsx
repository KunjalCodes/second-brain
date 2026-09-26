"use client";
/** Read-only transcript: the target of message provenance links (#message-{id}). */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { getConversation } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { formatDateTime } from "@/lib/format/dates";
import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/ui/States";

export function ConversationView({ id }: { id: string }) {
  const q = useQuery({ queryKey: qk.conversation(id), queryFn: ({ signal }) => getConversation(id, signal) });
  useEffect(() => {
    if (!q.data || typeof window === "undefined" || !window.location.hash) return;
    document.getElementById(window.location.hash.slice(1))?.scrollIntoView({ block: "center" });
  }, [q.data]);
  if (q.isPending) return <LoadingRows rows={4} label="Loading conversation" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} notFound="This conversation doesn't exist, or it isn't yours." />;
  const messages = q.data.messages ?? [];
  return (
    <div className="space-y-4">
      <PageHeader title={q.data.conversation.title || "Untitled conversation"} />
      <p className="text-xs text-[var(--color-fg-muted)]">Started {formatDateTime(q.data.conversation.created_at)}</p>
      {messages.length === 0 ? (
        <EmptyState title="No messages" />
      ) : (
        <ol className="space-y-3">
          {messages.map((m) => (
            <li key={m.id} id={`message-${m.id}`} className="scroll-mt-20 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3 target:border-[var(--color-accent)]">
              <p className="text-xs text-[var(--color-fg-muted)]">
                {m.role === "user" ? "You" : m.role === "assistant" ? "Assistant" : m.role} · {formatDateTime(m.created_at)}
              </p>
              <p className="content-text mt-1 text-sm">{m.content}</p>
            </li>
          ))}
        </ol>
      )}
      <Link className="text-sm underline" href="/ask">Ask your memory →</Link>
    </div>
  );
}
