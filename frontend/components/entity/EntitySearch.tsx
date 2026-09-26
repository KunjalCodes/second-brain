"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { searchEntities } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { useDebounced } from "@/lib/hooks/useDebounced";
import { EmptyState, ErrorState, LoadingRows, NotTimeFiltered, PageHeader } from "@/components/ui/States";
import { Tag } from "@/components/ui/Chip";

export function EntitySearch() {
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 250);
  const query = useQuery({
    queryKey: qk.entitySearch(q),
    queryFn: ({ signal }) => searchEntities(q, 50, 0, signal),
    enabled: q.length > 0,
  });
  return (
    <div>
      <PageHeader title="Entities">
        <NotTimeFiltered />
      </PageHeader>
      <label className="block text-sm">
        <span className="sr-only">Find an entity by name or alias</span>
        <input
          type="search"
          autoFocus
          placeholder="Find an entity by name or alias…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2"
        />
      </label>
      <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
        Entities merged into another are hidden; their names are still found as aliases of the entity they were merged into. To browse, open the{" "}
        <Link className="underline" href="/graph">graph overview</Link>.
      </p>
      <div className="mt-3">
        {!q ? (
          <EmptyState title="Type a name to search entities" />
        ) : query.isPending ? (
          <LoadingRows rows={3} label="Searching entities" />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : query.data.items.length === 0 ? (
          <EmptyState title="No entity matches that name" />
        ) : (
          <ul className="space-y-1">
            {query.data.items.map((e) => (
              <li key={e.id}>
                <Link href={`/entities/${e.id}`} className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-2 hover:border-[var(--color-accent)]">
                  <span className="font-medium">{e.name}</span>
                  <Tag>{e.entity_type}</Tag>
                  {e.aliases?.length ? <span className="text-xs text-[var(--color-fg-muted)]">also: {e.aliases.join(", ")}</span> : null}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
