"use client";
import type { MemoryStatus, MemoryType } from "@/lib/api/types";
import { MEMORY_STATUS, MEMORY_TYPE } from "@/lib/format/labels";

export interface MemoryFilterState {
  status: MemoryStatus[];
  memoryType: MemoryType[];
  entity: string;
  includeExcluded: boolean;
}

const STATUSES = Object.keys(MEMORY_STATUS) as MemoryStatus[];
const TYPES = Object.keys(MEMORY_TYPE) as MemoryType[];

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

/** Every control maps 1:1 to a `/memories/search` query parameter. */
export function MemoryFilters({ value, onChange }: { value: MemoryFilterState; onChange: (v: MemoryFilterState) => void }) {
  return (
    <div className="space-y-3 text-sm">
      <fieldset>
        <legend className="mb-1 font-medium">Status</legend>
        <p className="mb-1 text-xs text-[var(--color-fg-muted)]">
          {value.status.length === 0
            ? value.includeExcluded
              ? "All statuses."
              : "Server default: retracted, archived and merged duplicates hidden."
            : "Only the selected statuses."}
        </p>
        <div className="flex flex-wrap gap-2">
          {STATUSES.map((s) => (
            <label key={s} className="inline-flex items-center gap-1">
              <input type="checkbox" checked={value.status.includes(s)} onChange={() => onChange({ ...value, status: toggle(value.status, s) })} />
              {MEMORY_STATUS[s].text}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="inline-flex items-center gap-1">
        <input type="checkbox" checked={value.includeExcluded} onChange={() => onChange({ ...value, includeExcluded: !value.includeExcluded })} />
        Include retracted / archived / merged
      </label>
      <fieldset>
        <legend className="mb-1 font-medium">Type</legend>
        <div className="flex flex-wrap gap-2">
          {TYPES.map((t) => (
            <label key={t} className="inline-flex items-center gap-1">
              <input type="checkbox" checked={value.memoryType.includes(t)} onChange={() => onChange({ ...value, memoryType: toggle(value.memoryType, t) })} />
              {MEMORY_TYPE[t].text}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="block">
        <span className="font-medium">Mentions entity (exact name)</span>
        <input
          value={value.entity}
          onChange={(e) => onChange({ ...value, entity: e.target.value })}
          className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1"
        />
      </label>
    </div>
  );
}
