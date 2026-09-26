import Link from "next/link";
import type { Memory } from "@/lib/api/types";
import { MEMORY_TYPE } from "@/lib/format/labels";
import { formatDate, validityText } from "@/lib/format/dates";
import { MemoryStatusChip } from "@/components/ui/StatusChips";
import { Tag } from "@/components/ui/Chip";

/** One memory in a list. Text is rendered as inert data (ADR 0029). */
export function MemoryRow({ memory }: { memory: Memory }) {
  return (
    <li className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-accent)]">
      <Link href={`/memories/${memory.id}`} className="block p-3">
        <p className="content-text line-clamp-2">{memory.content}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
          <MemoryStatusChip status={memory.status} />
          <Tag>{MEMORY_TYPE[memory.memory_type].text}</Tag>
          <span>{validityText(memory.valid_from, memory.valid_until)}</span>
          <span aria-hidden="true">·</span>
          <span>Recorded {formatDate(memory.created_at)}</span>
          <span aria-hidden="true">·</span>
          <span title="Extraction confidence — metadata, not a probability of truth">
            Confidence {memory.confidence.toFixed(2)}
          </span>
        </div>
      </Link>
    </li>
  );
}
