"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useSyncExternalStore } from "react";
import { listProjects } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { getMemorySpace, setMemorySpace, subscribeMemorySpace } from "@/lib/api/space";

/**
 * Choose which MEMORY SPACE every page shows: the account's own memory, or one of its
 * projects (Phase 16). A project is a separate, isolated memory; switching clears the
 * cache so nothing from the previous space can remain on screen. The server verifies
 * ownership on every request, so this is a selection, not a permission.
 */
export function ProjectSwitcher() {
  const client = useQueryClient();
  const selected = useSyncExternalStore(subscribeMemorySpace, getMemorySpace, () => null);
  const projects = useQuery({ queryKey: qk.projects, queryFn: ({ signal }) => listProjects(signal) });
  const items = projects.data?.items ?? [];
  // A stored project that no longer exists for this account falls back to personal memory.
  const valid = selected && items.some((p) => p.id === selected) ? selected : "";
  const stale = projects.isSuccess && !!selected && !valid;
  useEffect(() => {
    if (stale) setMemorySpace(null);
  }, [stale]);
  if (projects.isSuccess && items.length === 0) return null;

  return (
    <label className="inline-flex items-center gap-2 text-sm">
      <span className="text-[var(--color-fg-muted)]">Memory space</span>
      <select
        value={valid}
        disabled={projects.isPending}
        onChange={(e) => {
          setMemorySpace(e.target.value || null);
          client.clear(); // nothing from the previous space may stay cached
        }}
        className="rounded-full border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-1"
      >
        <option value="">Personal memory</option>
        {items.map((p) => (
          <option key={p.id} value={p.id}>
            Project: {p.name}
          </option>
        ))}
      </select>
    </label>
  );
}
