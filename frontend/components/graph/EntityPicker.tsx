"use client";
/** Pick another entity (by name or alias) — used to ask for a path. */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { searchEntities } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { Entity } from "@/lib/api/types";
import { useDebounced } from "@/lib/hooks/useDebounced";
import { Tag } from "@/components/ui/Chip";

export function EntityPicker({ excludeId, onPick }: { excludeId: string; onPick: (e: Entity) => void }) {
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 250);
  const query = useQuery({
    queryKey: qk.entitySearch(q),
    queryFn: ({ signal }) => searchEntities(q, 8, 0, signal),
    enabled: q.length > 0,
  });
  const items = (query.data?.items ?? []).filter((e) => e.id !== excludeId);
  return (
    <div className="space-y-1 text-sm">
      <label className="block">
        <span className="sr-only">Find the entity to connect to</span>
        <input
          type="search"
          autoFocus
          placeholder="Connect to which entity? (name or alias)"
          value={text}
          onChange={(e) => setText(e.target.value)}
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1"
        />
      </label>
      {q && query.isPending ? <p className="text-xs text-[var(--color-fg-muted)]" role="status">Searching…</p> : null}
      {q && !query.isPending && items.length === 0 ? <p className="text-xs text-[var(--color-fg-muted)]" role="status">No entity matches that name.</p> : null}
      <ul className="space-y-1">
        {items.map((e) => (
          <li key={e.id}>
            <button
              type="button"
              onClick={() => onPick(e)}
              className="flex w-full flex-wrap items-center gap-2 rounded-md border border-[var(--color-border)] p-1.5 text-left hover:border-[var(--color-accent)]"
            >
              <span className="font-medium">{e.name}</span>
              <Tag>{e.entity_type}</Tag>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
