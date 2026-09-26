"use client";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError } from "@/lib/api/client";
import { listDocuments, uploadDocument } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { DOCUMENT_STATUS } from "@/lib/format/labels";
import { ACCEPT, MAX_UPLOAD_BYTES, documentErrorText, formatBytes, pollInterval } from "@/lib/format/documents";
import { formatDateTime } from "@/lib/format/dates";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, LoadingRows, NotTimeFiltered, PageHeader } from "@/components/ui/States";
import { DocumentPipeline, DocumentStatusChip } from "./DocumentStatusView";

export function uploadErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    const code = (err.details as { error_code?: string } | undefined)?.error_code;
    if (code) return documentErrorText(code, err.message);
    if (err.status === 429) return `Upload limit reached. Try again in ${err.retryAfter ?? "a while"} seconds.`;
    if (err.status === 413) return "The file is too large for this deployment.";
    return err.message;
  }
  return "Upload failed.";
}

function Uploader() {
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const upload = useMutation({
    mutationFn: uploadDocument,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: qk.documentsAll }),
  });
  function onPick(file: File | undefined) {
    setLocalError(null);
    upload.reset();
    if (!file) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      setLocalError(`That file is ${formatBytes(file.size)}; the limit is ${formatBytes(MAX_UPLOAD_BYTES)}.`);
      return;
    }
    upload.mutate(file);
  }
  return (
    <section aria-labelledby="upload-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 id="upload-title" className="font-semibold">Upload a document</h2>
      <p className="mt-1 text-sm text-[var(--color-fg-muted)]">
        Plain text, Markdown, or PDF with selectable text. Scanned/image-only PDFs, DOCX and web pages aren’t supported.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <input ref={input} type="file" accept={ACCEPT} aria-label="Choose a document" onChange={(e) => onPick(e.target.files?.[0])} className="text-sm" />
        {upload.isPending ? <span role="status" className="text-sm">Uploading…</span> : null}
      </div>
      {localError ? <p role="alert" className="mt-2 text-sm text-[var(--color-danger)]">{localError}</p> : null}
      {upload.isError ? <p role="alert" className="mt-2 text-sm text-[var(--color-danger)]">{uploadErrorText(upload.error)}</p> : null}
      {upload.isSuccess ? (
        <p role="status" className="mt-2 text-sm">
          Uploaded <Link className="underline" href={`/documents/${upload.data.id}`}>{upload.data.filename}</Link>. Processing continues in the background — you can leave this page.
        </p>
      ) : null}
    </section>
  );
}

export function DocumentList() {
  const [page, setPage] = useState(1);
  const [startedAt, setStartedAt] = useState(() => Date.now());
  const query = useQuery({
    queryKey: qk.documents(page),
    queryFn: ({ signal }) => listDocuments(25, (page - 1) * 25, signal),
    refetchInterval: (q) => {
      const docs = q.state.data?.documents ?? [];
      if (!docs.some((d) => !DOCUMENT_STATUS[d.status].terminal)) return false;
      return pollInterval(Date.now() - startedAt);
    },
  });
  const docs = query.data?.documents ?? [];
  const stillProcessing = docs.some((d) => !DOCUMENT_STATUS[d.status].terminal);
  const pollingStopped = stillProcessing && pollInterval(query.dataUpdatedAt - startedAt) === false;
  return (
    <div className="space-y-4">
      <PageHeader title="Documents"><NotTimeFiltered /></PageHeader>
      <Uploader />
      {query.isPending ? (
        <LoadingRows rows={3} label="Loading documents" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : docs.length === 0 ? (
        <EmptyState title={page === 1 ? "No documents yet" : "No more documents"}>Upload a file above to teach the AI from it.</EmptyState>
      ) : (
        <ul className="space-y-2">
          {docs.map((d) => (
            <li key={d.id} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
              <div className="flex flex-wrap items-center gap-2">
                <Link className="font-medium underline" href={`/documents/${d.id}`}>{d.filename}</Link>
                <DocumentStatusChip doc={d} />
                <span className="text-xs text-[var(--color-fg-muted)]">
                  {d.content_type} · {formatBytes(d.size_bytes)}
                  {d.page_count ? ` · ${d.page_count} pages` : ""} · uploaded {formatDateTime(d.uploaded_at)}
                </span>
              </div>
              <div className="mt-2"><DocumentPipeline doc={d} /></div>
            </li>
          ))}
        </ul>
      )}
      {pollingStopped ? (
        <p className="text-sm text-[var(--color-fg-muted)]">Still processing — stopped checking automatically. <Button onClick={() => { setStartedAt(Date.now()); void query.refetch(); }}>Check again</Button></p>
      ) : null}
      <div className="flex gap-2">
        <Button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>← Newer</Button>
        <Button disabled={docs.length < 25} onClick={() => setPage((p) => p + 1)}>Older →</Button>
      </div>
    </div>
  );
}
