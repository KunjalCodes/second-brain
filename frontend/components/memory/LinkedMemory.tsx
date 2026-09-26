"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getMemory } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { MemoryStatusChip } from "@/components/ui/StatusChips";
import { validityText } from "@/lib/format/dates";

/** A compact card for a memory referenced only by id (supersedes, duplicate_of, …). */
export function LinkedMemory({ id }: { id: string }) {
  const q = useQuery({ queryKey: qk.memory(id), queryFn: ({ signal }) => getMemory(id, signal) });
  return (
    <Link href={`/memories/${id}`} className="block rounded-md border border-[var(--color-border)] p-2 text-sm hover:border-[var(--color-accent)]">
      {q.data ? (
        <>
          <p className="content-text line-clamp-2">{q.data.memory.content}</p>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
            <MemoryStatusChip status={q.data.memory.status} />
            {validityText(q.data.memory.valid_from, q.data.memory.valid_until)}
          </div>
        </>
      ) : q.isError ? (
        <span className="text-[var(--color-fg-muted)]">A linked memory (not available)</span>
      ) : (
        <span className="skeleton block h-8" />
      )}
    </Link>
  );
}
