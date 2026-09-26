import { Chip } from "./Chip";
import { MEMORY_STATUS, TEMPORAL_STATE } from "@/lib/format/labels";
import type { MemoryStatus, TemporalState } from "@/lib/api/types";

export function MemoryStatusChip({ status }: { status: MemoryStatus }) {
  const label = MEMORY_STATUS[status];
  return <Chip label={label} prefix="Status" title={label.explain} />;
}

/** Temporal state is a DIFFERENT axis from lifecycle status (ADR 0028). */
export function TemporalStateChip({ state, asOfLabel }: { state: TemporalState; asOfLabel?: string }) {
  const label = TEMPORAL_STATE[state];
  return (
    <Chip
      label={{ ...label, text: asOfLabel ? `${label.text} (${asOfLabel})` : label.text }}
      prefix="Temporal state"
      title={label.explain}
    />
  );
}
