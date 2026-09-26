"use client";
/**
 * "Why the AI remembers this" (docs/PHASE10_PLAN.md §9). Shows exactly what
 * `GET /memories/{id}/provenance` resolved -- never more. The memory and the
 * source it was derived from are visually distinct tiers.
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getProvenance } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { SourceProvenance } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format/dates";
import { ErrorState, LoadingRows } from "@/components/ui/States";
import { Tag } from "@/components/ui/Chip";

const SOURCE_TYPE_LABEL: Record<string, string> = {
  message: "Conversation message",
  document: "Document",
  url: "Web address",
  conversation: "Conversation",
  user_statement: "Your statement",
  tool_result: "Tool result",
  system_inference: "System inference",
};

/** Only http(s) URLs get an "open" affordance; the URL itself is shown as text. */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

export function isResolved(sp: SourceProvenance): boolean {
  return Boolean(sp.message || sp.document || sp.document_chunk);
}

function SourceCard({ sp }: { sp: SourceProvenance }) {
  const { source } = sp;
  const typeLabel = SOURCE_TYPE_LABEL[source.source_type] ?? source.source_type;
  const url = source.source_type === "url" ? safeHttpUrl(source.reference) : null;
  const expectsResolution = source.source_type === "message" || source.source_type === "document";
  return (
    <li className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Tag>{typeLabel}</Tag>
        <span className="text-xs text-[var(--color-fg-muted)]">Recorded {formatDateTime(source.created_at)}</span>
      </div>

      {sp.message ? (
        <div className="mt-2">
          <p className="text-xs text-[var(--color-fg-muted)]">
            {sp.message.role} message · {formatDateTime(sp.message.created_at)}
          </p>
          <blockquote className="content-text mt-1 border-l-2 border-[var(--color-border)] pl-3">{sp.message.content}</blockquote>
          <Link className="mt-1 inline-block underline" href={`/conversations/${sp.message.conversation_id}#message-${sp.message.id}`}>
            Open conversation →
          </Link>
        </div>
      ) : null}

      {sp.document ? (
        <div className="mt-2">
          <p>
            <span className="font-medium">{sp.document.filename}</span>
            {sp.document_chunk?.page_start
              ? ` · page ${sp.document_chunk.page_start}${
                  sp.document_chunk.page_end && sp.document_chunk.page_end !== sp.document_chunk.page_start
                    ? `–${sp.document_chunk.page_end}`
                    : ""
                }`
              : ""}
            {sp.document_chunk?.heading_context ? ` · “${sp.document_chunk.heading_context}”` : ""}
          </p>
          {sp.document_chunk ? (
            <blockquote className="content-text mt-1 max-h-48 overflow-auto border-l-2 border-[var(--color-border)] pl-3">
              {sp.document_chunk.content}
            </blockquote>
          ) : (
            <p className="mt-1 text-xs text-[var(--color-warn)]">The exact passage (chunk) is no longer available.</p>
          )}
          <Link
            className="mt-1 inline-block underline"
            href={`/documents/${sp.document.id}${sp.document_chunk ? `#chunk-${sp.document_chunk.id}` : ""}`}
          >
            Open document at this passage →
          </Link>
        </div>
      ) : null}

      {source.source_type === "url" && source.reference ? (
        <div className="mt-2">
          <p className="content-text font-mono text-xs">{source.reference}</p>
          {url ? (
            <a className="mt-1 inline-block underline" href={url} target="_blank" rel="noopener noreferrer">
              Open link in a new tab ↗
            </a>
          ) : (
            <p className="text-xs text-[var(--color-fg-muted)]">Not a web (http/https) address; not opened.</p>
          )}
        </div>
      ) : null}

      {expectsResolution && !isResolved(sp) ? (
        <p className="mt-2 text-[var(--color-warn)]">
          A source was recorded, but its content is no longer available.
        </p>
      ) : null}

      {!expectsResolution && source.source_type !== "url" ? (
        <p className="mt-2 text-xs text-[var(--color-fg-muted)]">
          Reference as recorded: <span className="font-mono">{source.reference ?? "—"}</span>
          {source.location ? <> · location <span className="font-mono">{source.location}</span></> : null}
        </p>
      ) : null}
    </li>
  );
}

export function ProvenancePanel({ memoryId, conflictsWith = [] }: { memoryId: string; conflictsWith?: string[] }) {
  const query = useQuery({
    queryKey: qk.provenance(memoryId),
    queryFn: ({ signal }) => getProvenance(memoryId, signal),
    staleTime: 5 * 60_000,
  });
  return (
    <section aria-labelledby="provenance-title" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 id="provenance-title" className="mb-2 font-semibold">
        Why the AI remembers this
      </h2>
      {query.isPending ? (
        <LoadingRows rows={2} label="Loading provenance" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.sources.length === 0 ? (
        <p className="text-sm">
          <strong>No source recorded for this memory.</strong>{" "}
          <span className="text-[var(--color-fg-muted)]">
            It may have been created directly (for example through the API) rather than learned from a conversation or document.
          </span>
        </p>
      ) : (
        <>
          <p className="mb-2 text-xs text-[var(--color-fg-muted)]">
            Derived from {query.data.sources.length === 1 ? "this source" : `${query.data.sources.length} sources (corroborated)`}:
          </p>
          <ul className="space-y-2">
            {query.data.sources.map((sp) => (
              <SourceCard key={sp.source.id} sp={sp} />
            ))}
          </ul>
        </>
      )}
      {conflictsWith.length ? (
        <p className="mt-3 text-sm">
          <span aria-hidden="true">⚠ </span>
          Other memories dispute this one. Sources themselves don’t conflict — memories do; compare their provenance:{" "}
          {conflictsWith.map((id, i) => (
            <span key={id}>
              {i ? ", " : ""}
              <Link className="underline" href={`/memories/${id}`}>disputing memory {i + 1}</Link>
            </span>
          ))}
        </p>
      ) : null}
    </section>
  );
}
