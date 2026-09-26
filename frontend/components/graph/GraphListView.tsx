"use client";
/** The same graph slice as tables: the keyboard/screen-reader (and mobile) view of the canvas. */
import type { GraphState } from "@/lib/graph/model";
import type { Selection } from "./CytoscapeCanvas";
import { MEMORY_STATUS, TEMPORAL_STATE } from "@/lib/format/labels";
import type { MemoryStatus } from "@/lib/api/types";

export function GraphListView({ state, onSelect }: { state: GraphState; onSelect: (s: Selection) => void }) {
  const entities = [...state.nodes.values()].filter((n) => n.kind === "entity").sort((a, b) => (a.hops ?? 99) - (b.hops ?? 99) || a.label.localeCompare(b.label));
  const memories = [...state.nodes.values()].filter((n) => n.kind === "memory");
  const label = (id: string) => state.nodes.get(id)?.label ?? "(not loaded)";
  const rels = [...state.edges.values()].filter((e) => e.kind === "relates_to");
  const links = [...state.edges.values()].filter((e) => e.kind !== "relates_to" && e.kind !== "mentions");
  return (
    <div className="space-y-4 text-sm">
      <table className="w-full border-collapse">
        <caption className="mb-1 text-left font-semibold">Entities ({entities.length})</caption>
        <thead>
          <tr className="text-left text-xs text-[var(--color-fg-muted)]"><th className="py-1">Name</th><th>Type</th><th>Distance</th></tr>
        </thead>
        <tbody>
          {entities.map((n) => (
            <tr key={n.id} className="border-t border-[var(--color-border)]">
              <td className="py-1">
                <button type="button" className="underline" onClick={() => onSelect({ type: "node", id: n.id })}>{n.label}</button>
                {n.merged_from_count ? <span className="ml-1 text-xs">⧉ {n.merged_from_count} merged</span> : null}
              </td>
              <td>{n.entity_type ?? ""}</td>
              <td>{n.hops == null ? "—" : n.hops === 0 ? "focus" : `${n.hops} hop${n.hops === 1 ? "" : "s"}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className="w-full border-collapse">
        <caption className="mb-1 text-left font-semibold">Relationships ({rels.length})</caption>
        <thead>
          <tr className="text-left text-xs text-[var(--color-fg-muted)]"><th className="py-1">Relationship</th><th>Status</th><th>Time</th><th>Assertions</th></tr>
        </thead>
        <tbody>
          {rels.map((e) => (
            <tr key={e.id} className="border-t border-[var(--color-border)]">
              <td className="py-1">
                <button type="button" className="underline" onClick={() => onSelect({ type: "edge", id: e.id })}>
                  {label(e.source)} —{e.predicate}→ {label(e.target)}
                </button>
              </td>
              <td>{e.status ? MEMORY_STATUS[e.status as MemoryStatus].text : ""}</td>
              <td>{e.temporal_state ? TEMPORAL_STATE[e.temporal_state].text : ""}</td>
              <td>{e.assertion_count ?? 1}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {memories.length ? (
        <table className="w-full border-collapse">
          <caption className="mb-1 text-left font-semibold">Memories ({memories.length})</caption>
          <tbody>
            {memories.map((n) => (
              <tr key={n.id} className="border-t border-[var(--color-border)]">
                <td className="py-1"><button type="button" className="content-text text-left underline" onClick={() => onSelect({ type: "node", id: n.id })}>{n.label}</button></td>
                <td>{n.status ? MEMORY_STATUS[n.status as MemoryStatus].text : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {links.length ? (
        <ul>
          {links.map((e) => (
            <li key={e.id}>
              <button type="button" className="underline" onClick={() => onSelect({ type: "edge", id: e.id })}>
                {e.kind.replace("_", " ")}: {label(e.source).slice(0, 40)} → {label(e.target).slice(0, 40)}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
