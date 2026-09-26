"use client";
/**
 * The Memory Explorer list (docs/PHASE10_PLAN.md §6), backed ONLY by
 * `GET /memories/search`. Filters and page live in the URL; the free-text query
 * deliberately does NOT (Q7: search text never goes into the browser URL).
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { searchMemories } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { MemoryStatus, MemoryType } from "@/lib/api/types";
import { useDebounced } from "@/lib/hooks/useDebounced";
import { useTimeLens } from "@/lib/time/lens";
import { formatDateTime } from "@/lib/format/dates";
import { MemoryFilters, type MemoryFilterState } from "./MemoryFilters";
import { MemoryRow } from "./MemoryRow";
import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/ui/States";
import { Pagination } from "@/components/ui/Pagination";

const PAGE_SIZES = [25, 50, 100];

export function filtersFromParams(params: URLSearchParams): MemoryFilterState & { page: number; pageSize: number } {
  const size = Number(params.get("size"));
  return {
    status: params.getAll("status") as MemoryStatus[],
    memoryType: params.getAll("type") as MemoryType[],
    entity: params.get("entity") ?? "",
    includeExcluded: params.get("all") === "1",
    page: Math.max(1, Number(params.get("page")) || 1),
    pageSize: PAGE_SIZES.includes(size) ? size : 25,
  };
}

export function paramsFromFilters(f: MemoryFilterState & { page: number; pageSize: number }): URLSearchParams {
  const p = new URLSearchParams();
  f.status.forEach((s) => p.append("status", s));
  f.memoryType.forEach((t) => p.append("type", t));
  if (f.entity) p.set("entity", f.entity);
  if (f.includeExcluded) p.set("all", "1");
  if (f.page > 1) p.set("page", String(f.page));
  if (f.pageSize !== 25) p.set("size", String(f.pageSize));
  return p;
}

export function MemoryExplorer() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { asOf } = useTimeLens();
  const state = filtersFromParams(new URLSearchParams(params.toString()));
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 300);

  const setState = (next: Partial<typeof state>) => {
    const merged = { ...state, ...next };
    const qs = paramsFromFilters(merged).toString();
    router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
  };

  const apiParams = {
    q: q || undefined,
    status: state.status.length ? state.status : undefined,
    memory_type: state.memoryType.length ? state.memoryType : undefined,
    entity: state.entity || undefined,
    as_of: asOf ?? undefined,
    include_excluded: state.includeExcluded || undefined,
    limit: state.pageSize,
    offset: (state.page - 1) * state.pageSize,
  };
  const query = useQuery({
    queryKey: qk.memories(apiParams),
    queryFn: ({ signal }) => searchMemories(apiParams, signal),
    placeholderData: keepPreviousData,
  });
  const filtered = Boolean(q || state.status.length || state.memoryType.length || state.entity || asOf);

  return (
    <div>
      <PageHeader title="Memories">
        <span className="text-xs text-[var(--color-fg-muted)]">Newest recorded first</span>
      </PageHeader>
      <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
        <aside aria-label="Filters" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
          <MemoryFilters value={state} onChange={(v) => setState({ ...v, page: 1 })} />
        </aside>
        <section aria-label="Memory list">
          <label className="mb-2 block text-sm">
            <span className="sr-only">Find memories containing</span>
            <input
              type="search"
              placeholder="Find memories containing… (exact substring)"
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                if (state.page !== 1) setState({ page: 1 });
              }}
              className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2"
            />
          </label>
          {asOf ? (
            <p className="mb-2 text-xs text-[var(--color-warn)]">
              Showing memories whose validity window contains {formatDateTime(asOf)}.
            </p>
          ) : null}
          {query.isPending ? (
            <LoadingRows label="Loading memories" />
          ) : query.isError ? (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          ) : query.data.items.length === 0 ? (
            filtered ? (
              <EmptyState title="No memories match these filters">Try clearing filters or the time lens.</EmptyState>
            ) : (
              <EmptyState title="The AI hasn't stored any memories for you yet">
                Memories appear after conversations are processed or documents are ingested.
              </EmptyState>
            )
          ) : (
            <>
              <p className="mb-2 text-xs text-[var(--color-fg-muted)]" aria-live="polite">
                {query.data.total} memor{query.data.total === 1 ? "y" : "ies"}
                {query.isFetching ? " · updating…" : ""}
              </p>
              <ul className="space-y-2">
                {query.data.items.map((m) => (
                  <MemoryRow key={m.id} memory={m} />
                ))}
              </ul>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Pagination page={state.page} pageSize={state.pageSize} total={query.data.total} onPage={(page) => setState({ page })} />
                <label className="pt-3 text-xs">
                  Per page{" "}
                  <select
                    value={state.pageSize}
                    onChange={(e) => setState({ pageSize: Number(e.target.value), page: 1 })}
                    className="rounded border border-[var(--color-border)] bg-[var(--color-surface)]"
                  >
                    {PAGE_SIZES.map((s) => (
                      <option key={s}>{s}</option>
                    ))}
                  </select>
                </label>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
