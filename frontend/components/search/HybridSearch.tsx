"use client";
/**
 * Ranked search over memories (`POST /search`, docs/PHASE10_PLAN.md §12).
 * The query text lives only in component state -- never in the URL (Q7) --
 * and is sent in the POST body. Scores are ranking signals, not truth.
 */
import Link from "next/link";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { hybridSearch } from "@/lib/api/endpoints";
import type { SearchResultItem, TemporalState } from "@/lib/api/types";
import { MEMORY_TYPE, RELEVANCE_EXPLAIN, RETRIEVAL_CHANNEL, TEMPORAL_STATE } from "@/lib/format/labels";
import { validityText } from "@/lib/format/dates";
import { MemoryStatusChip, TemporalStateChip } from "@/components/ui/StatusChips";
import { Tag } from "@/components/ui/Chip";
import { Button } from "@/components/ui/Button";
import { Explain } from "@/components/ui/Explain";
import { EmptyState, ErrorState, LoadingRows, NotTimeFiltered, PageHeader } from "@/components/ui/States";

export function channelsText(channels: string[]): string {
  const words = channels.map((c) => RETRIEVAL_CHANNEL[c] ?? c);
  return words.length ? `Matched by ${words.join(" + ")}` : "Matched";
}

function fmt(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(3);
}

function ResultCard({ item, topScore }: { item: SearchResultItem; topScore: number }) {
  const { memory: d, explanation: x } = item;
  const relative = topScore > 0 ? Math.max(0, Math.min(1, x.final_score / topScore)) : 0;
  const state = x.temporal_state as TemporalState;
  return (
    <li className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
      <Link href={`/memories/${d.memory.id}`} className="content-text block hover:underline">
        {d.memory.content}
      </Link>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        <MemoryStatusChip status={d.memory.status} />
        {TEMPORAL_STATE[state] ? <TemporalStateChip state={state} /> : null}
        <Tag>{MEMORY_TYPE[d.memory.memory_type].text}</Tag>
        <span className="text-[var(--color-fg-muted)]">{validityText(d.memory.valid_from, d.memory.valid_until)}</span>
        <span className="text-[var(--color-fg-muted)]">
          · {(d.sources ?? []).length ? `${(d.sources ?? []).length} source(s)` : "no recorded source"}
        </span>
      </div>
      {(d.entities ?? []).length ? (
        <p className="mt-1 text-xs">
          About:{" "}
          {(d.entities ?? []).map((e, i) => (
            <span key={e.id}>
              {i ? ", " : ""}
              <Link className="underline" href={`/entities/${e.id}`}>{e.name}</Link>
            </span>
          ))}
        </p>
      ) : null}
      <div className="mt-2 flex items-center gap-2 text-xs">
        <span className="text-[var(--color-fg-muted)]">Relevance (relative)</span>
        <span className="h-1.5 w-32 rounded bg-[var(--color-surface-2)]" aria-hidden="true">
          <span className="block h-1.5 rounded bg-[var(--color-accent)]" style={{ width: `${relative * 100}%` }} />
        </span>
        <span className="sr-only">{Math.round(relative * 100)}% of the top result</span>
      </div>
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer">Why this result?</summary>
        <p className="mt-1">{channelsText(x.retrieved_via ?? [])}.</p>
        <p className="mt-1 text-[var(--color-fg-muted)]">
          Time fit reflects how the memory’s validity relates to now ({TEMPORAL_STATE[state]?.text ?? x.temporal_state}).
        </p>
        <details className="mt-1">
          <summary className="cursor-pointer">Advanced: raw ranking signals</summary>
          <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 font-mono">
            <dt>meaning (vector)</dt><dd>{fmt(x.vector_similarity)}</dd>
            <dt>graph</dt><dd>{fmt(x.graph_relevance)}</dd>
            <dt>keyword</dt><dd>{fmt(x.keyword_match)}</dd>
            <dt>time fit</dt><dd>{fmt(x.temporal_fit)}</dd>
            <dt>extraction confidence</dt><dd>{fmt(x.confidence)}</dd>
            <dt>final score</dt><dd>{fmt(x.final_score)}</dd>
          </dl>
          <p className="mt-1">{RELEVANCE_EXPLAIN}</p>
        </details>
      </details>
    </li>
  );
}

export function HybridSearch() {
  const [text, setText] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const search = useMutation({ mutationFn: (q: string) => hybridSearch(q, 50) });

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = text.trim();
    if (!q) return;
    setSubmitted(q);
    search.mutate(q);
  }
  const items = search.data?.items ?? [];
  const top = items.reduce((m, i) => Math.max(m, i.explanation.final_score), 0);

  return (
    <div>
      <PageHeader title="Search">
        <NotTimeFiltered />
      </PageHeader>
      <form onSubmit={onSubmit} role="search" className="flex gap-2">
        <label className="flex-1">
          <span className="sr-only">Search your memories</span>
          <input
            type="search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={2000}
            placeholder="Ask about something the AI might remember…"
            className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2"
          />
        </label>
        <Button type="submit" variant="primary" disabled={search.isPending}>Search</Button>
      </form>
      <div className="mt-1 text-xs text-[var(--color-fg-muted)]">
        {/* A <div>, not a <p>: <Explain> renders <details>/<div>, invalid inside <p> (hydration error). */}
        Ranked by meaning, connected entities, exact words and time fit. Up to 50 results; no filters.{" "}
        <Explain summary="How is this different from the Memories page?">
          The Memories page filters by an exact substring and supports status, type and time filters with pagination. This page ranks results by relevance.
        </Explain>
      </div>
      <div className="mt-4">
        {!submitted ? (
          <EmptyState title="Search your memories" />
        ) : search.isPending ? (
          <LoadingRows rows={4} label="Searching" />
        ) : search.isError ? (
          <ErrorState error={search.error} onRetry={() => search.mutate(submitted)} />
        ) : items.length === 0 ? (
          <EmptyState title="No memories match">Try different words.</EmptyState>
        ) : (
          <>
            <p className="mb-2 text-xs text-[var(--color-fg-muted)]" aria-live="polite">{items.length} result(s)</p>
            <ol className="space-y-2">{items.map((it) => <ResultCard key={it.memory.memory.id} item={it} topScore={top} />)}</ol>
          </>
        )}
      </div>
    </div>
  );
}
