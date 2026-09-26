"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { getDocument, getDocumentChunks } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { DOCUMENT_STATUS } from "@/lib/format/labels";
import { formatBytes, pollInterval } from "@/lib/format/documents";
import { formatDateTime } from "@/lib/format/dates";
import { Tag } from "@/components/ui/Chip";
import { ErrorState, LoadingRows } from "@/components/ui/States";
import { DocumentPipeline, DocumentStatusChip } from "./DocumentStatusView";

export function DocumentDetail({ id }: { id: string }) {
  const [startedAt] = useState(() => Date.now());
  // Rendered client-side only (behind the auth gate), so `window` is available.
  const [anchor] = useState<string | null>(() =>
    typeof window === "undefined" ? null : window.location.hash.replace(/^#chunk-/, "") || null,
  );
  const doc = useQuery({
    queryKey: qk.document(id),
    queryFn: ({ signal }) => getDocument(id, signal),
    refetchInterval: (q) =>
      q.state.data && !DOCUMENT_STATUS[q.state.data.status].terminal ? pollInterval(Date.now() - startedAt) : false,
  });
  const chunks = useQuery({
    queryKey: qk.chunks(id),
    queryFn: ({ signal }) => getDocumentChunks(id, signal),
    enabled: Boolean(doc.data),
    refetchInterval: doc.data && !DOCUMENT_STATUS[doc.data.status].terminal ? 15_000 : false,
  });
  if (doc.isPending) return <LoadingRows rows={3} label="Loading document" />;
  if (doc.isError) return <ErrorState error={doc.error} onRetry={() => void doc.refetch()} notFound="This document doesn't exist, or it isn't yours." />;
  const d = doc.data;
  const list = chunks.data?.chunks ?? [];
  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm"><Link className="underline" href="/documents">← Documents</Link></nav>
      <header className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <h1 className="text-lg font-semibold">{d.filename}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
          <DocumentStatusChip doc={d} />
          {d.content_type} · {formatBytes(d.size_bytes)}{d.page_count ? ` · ${d.page_count} pages` : ""} · uploaded {formatDateTime(d.uploaded_at)}
          {d.processed_at ? ` · processed ${formatDateTime(d.processed_at)}` : ""}
        </div>
        <div className="mt-3"><DocumentPipeline doc={d} /></div>
        <p className="mt-3 text-xs text-[var(--color-fg-muted)]">
          The list of memories learned from this document isn’t available (the backend exposes provenance from a memory to its source, not the reverse). Open a memory to see which document passage it came from.
        </p>
      </header>
      <section aria-labelledby="chunks-title">
        <h2 id="chunks-title" className="mb-2 font-semibold">Passages (chunks) {list.length ? `· ${list.length}` : ""}</h2>
        {chunks.isPending ? (
          <LoadingRows rows={3} label="Loading passages" />
        ) : chunks.isError ? (
          <ErrorState error={chunks.error} onRetry={() => void chunks.refetch()} />
        ) : list.length === 0 ? (
          <p className="text-sm text-[var(--color-fg-muted)]">No passages yet{DOCUMENT_STATUS[d.status].terminal ? "." : " — still processing."}</p>
        ) : (
          <ol className="chunk-list space-y-2">
            {list.map((c) => (
              <li key={c.id} id={`chunk-${c.id}`} className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
                <details open={anchor === c.id}>
                  <summary className="cursor-pointer text-sm">
                    <span className="font-medium">#{c.ordinal + 1}</span>
                    {c.page_start ? ` · page ${c.page_start}${c.page_end && c.page_end !== c.page_start ? `–${c.page_end}` : ""}` : ""}
                    {c.heading_context ? ` · ${c.heading_context}` : ""} <Tag>extraction: {c.extraction_status}</Tag>
                    <span className="mt-1 block truncate text-[var(--color-fg-muted)]">{c.content.slice(0, 160)}</span>
                  </summary>
                  <p className="content-text mt-2 text-sm">{c.content}</p>
                </details>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
