/**
 * Client-side graph state for the Graph Explorer (docs/PHASE10_PLAN.md §7,
 * ADR 0027). This is ONLY bookkeeping of what the server has returned:
 *   - slices are merged by the server's stable node/edge ids;
 *   - truncation flags and budgets are kept exactly as the server reported;
 *   - request construction never exceeds the server-reported budget.
 * No temporal logic, identity resolution or budget policy lives here.
 */
import type { GraphPath, GraphSlice, GraphSliceEdge, GraphSliceNode } from "@/lib/api/types";

/** Beyond this many canvas elements the UI asks the user to re-root (plan §21). */
export const CANVAS_ELEMENT_CAP = 600;

export interface Truncation {
  nodes: boolean;
  edges: boolean;
  memories: boolean;
  maxNodes: number;
  maxEdges: number | null;
}

export interface GraphState {
  nodes: Map<string, GraphSliceNode>;
  edges: Map<string, GraphSliceEdge>;
  /** Truncation as reported by the MOST RECENT slice (never inferred). */
  lastTruncation: Truncation | null;
  /** True if ANY slice merged into this canvas was truncated. */
  everTruncated: boolean;
  /** The last budget the server reported: requests never exceed it. */
  budgetMaxNodes: number;
  /** Set when the last response followed a MERGED_INTO redirect. */
  resolvedFrom: { requested: string; canonical: string } | null;
  asOf: string | null;
}

export function emptyGraph(): GraphState {
  return {
    nodes: new Map(),
    edges: new Map(),
    lastTruncation: null,
    everTruncated: false,
    budgetMaxNodes: 150,
    resolvedFrom: null,
    asOf: null,
  };
}

export function truncationOf(slice: GraphSlice): Truncation {
  return {
    nodes: Boolean(slice.truncated_nodes),
    edges: Boolean(slice.truncated_edges),
    memories: Boolean(slice.truncated_memories),
    maxNodes: slice.budget.max_nodes,
    maxEdges: slice.budget.max_edges ?? null,
  };
}

export function isTruncated(t: Truncation | null): boolean {
  return Boolean(t && (t.nodes || t.edges || t.memories));
}

/** Merge a server slice into the canvas state (by id; the server's data wins). */
export function mergeSlice(state: GraphState, slice: GraphSlice, replace = false): GraphState {
  const nodes = replace ? new Map<string, GraphSliceNode>() : new Map(state.nodes);
  const edges = replace ? new Map<string, GraphSliceEdge>() : new Map(state.edges);
  for (const n of slice.nodes ?? []) {
    const prev = nodes.get(n.id);
    // Keep the smallest known hop distance when the same node arrives again.
    const hops = prev?.hops != null && n.hops != null ? Math.min(prev.hops, n.hops) : (n.hops ?? prev?.hops ?? null);
    nodes.set(n.id, { ...n, hops });
  }
  for (const e of slice.edges ?? []) edges.set(e.id, e);
  const t = truncationOf(slice);
  return {
    nodes,
    edges,
    lastTruncation: t,
    everTruncated: (replace ? false : state.everTruncated) || isTruncated(t),
    budgetMaxNodes: slice.budget.max_nodes,
    resolvedFrom:
      slice.resolved_from && slice.root ? { requested: slice.resolved_from, canonical: slice.root } : null,
    asOf: slice.as_of,
  };
}

export function elementCount(state: GraphState): number {
  return state.nodes.size + state.edges.size;
}

export function overCanvasCap(state: GraphState): boolean {
  return elementCount(state) >= CANVAS_ELEMENT_CAP;
}

/**
 * The server accepts at most `budget.max_nodes` exclude_ids (it rejects more
 * with 422). When more nodes are loaded, send the ones nearest the expanded
 * node (its direct neighbours first) -- anything else that comes back is
 * simply de-duplicated by id on merge.
 */
export function excludeIdsFor(state: GraphState, focusId: string): string[] {
  const cap = state.budgetMaxNodes;
  const entityIds = [...state.nodes.values()].filter((n) => n.kind === "entity").map((n) => n.id);
  if (entityIds.length <= cap) return entityIds.filter((id) => id !== focusId);
  const neighbours = new Set<string>();
  for (const e of state.edges.values()) {
    if (e.source === focusId) neighbours.add(e.target);
    if (e.target === focusId) neighbours.add(e.source);
  }
  const ordered = [...entityIds.filter((id) => neighbours.has(id)), ...entityIds.filter((id) => !neighbours.has(id))];
  return ordered.filter((id) => id !== focusId).slice(0, cap);
}

export interface CyElement {
  group: "nodes" | "edges";
  data: Record<string, unknown>;
  classes?: string;
}

/** Map server fields onto Cytoscape element data; styling is selector-driven from these fields. */
export function toElements(state: GraphState): CyElement[] {
  const out: CyElement[] = [];
  for (const n of state.nodes.values()) {
    out.push({
      group: "nodes",
      data: {
        id: n.id,
        label: n.kind === "memory" ? truncateLabel(n.label, 40) : n.label,
        kind: n.kind,
        entity_type: n.entity_type ?? "",
        status: n.status ?? "",
        temporal_state: n.temporal_state ?? "",
        merged: n.merged_from_count ? "yes" : "no",
      },
    });
  }
  for (const e of state.edges.values()) {
    // Edges may reference a node from an earlier slice that is still present;
    // skip dangling ends rather than letting the renderer throw.
    if (!state.nodes.has(e.source) || !state.nodes.has(e.target)) continue;
    out.push({
      group: "edges",
      data: {
        id: e.id,
        source: e.source,
        target: e.target,
        kind: e.kind,
        label: e.kind === "relates_to" ? (e.predicate ?? "") : "",
        status: e.status ?? "",
        temporal_state: e.temporal_state ?? "",
        weight: Math.min(e.assertion_count ?? 1, 6),
      },
    });
  }
  return out;
}

function truncateLabel(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// -- Phase 15: filters, collapse, paths ---------------------------------------------------

/** Client-side relationship filter: hide `relates_to` edges of other predicates.
 *  Non-relationship links (mentions, supersedes, ...) and all nodes are kept, so
 *  nothing is dropped from what the server returned -- only what is drawn. */
export function filterByPredicate(state: GraphState, predicate: string | null): GraphState {
  if (!predicate) return state;
  const edges = new Map<string, GraphSliceEdge>();
  for (const [id, e] of state.edges) {
    if (e.kind !== "relates_to" || e.predicate === predicate) edges.set(id, e);
  }
  return { ...state, edges };
}

/** The distinct predicates among loaded `relates_to` edges, sorted. */
export function predicatesIn(state: GraphState): string[] {
  const out = new Set<string>();
  for (const e of state.edges.values()) if (e.kind === "relates_to" && e.predicate) out.add(e.predicate);
  return [...out].sort();
}

/** Ids a slice would ADD to the canvas (used to undo one expansion). */
export function newNodeIds(state: GraphState, slice: GraphSlice): string[] {
  return (slice.nodes ?? []).filter((n) => !state.nodes.has(n.id)).map((n) => n.id);
}

/** Remove nodes and every edge touching them. */
export function removeNodes(state: GraphState, ids: Iterable<string>): GraphState {
  const drop = new Set(ids);
  if (!drop.size) return state;
  const nodes = new Map([...state.nodes].filter(([id]) => !drop.has(id)));
  const edges = new Map([...state.edges].filter(([, e]) => !drop.has(e.source) && !drop.has(e.target)));
  return { ...state, nodes, edges };
}

/** The canvas ids a path highlights: its entities and the stored edges on it. */
export function pathHighlight(path: GraphPath): { nodeIds: Set<string>; edgeIds: Set<string> } {
  const nodeIds = new Set(path.entity_ids);
  const edgeIds = new Set<string>();
  for (const step of path.steps) {
    for (const r of step.relationships) {
      const [source, target] =
        r.direction === "forward"
          ? [step.from_entity_id, step.to_entity_id]
          : [step.to_entity_id, step.from_entity_id];
      edgeIds.add(relatesToEdgeId(source, r.predicate, target, r.status, r.temporal_state));
    }
  }
  return { nodeIds, edgeIds };
}

/** Same id the server gives an aggregated relates_to slice edge. */
export function relatesToEdgeId(source: string, predicate: string, target: string, status: string, state: string): string {
  return `relates_to:${source}:${predicate}:${target}:${status}:${state}`;
}

/** Put a path's entities and stored edges on the canvas. Only what is MISSING is
 *  added: entities already loaded keep their richer data, and truncation info
 *  (which describes the server's slices, not this path) is left untouched. */
export function addPath(state: GraphState, path: GraphPath): GraphState {
  const nodes = new Map(state.nodes);
  const edges = new Map(state.edges);
  for (const step of path.steps) {
    for (const [id, label] of [[step.from_entity_id, step.from_name], [step.to_entity_id, step.to_name]] as const) {
      if (!nodes.has(id)) nodes.set(id, { id, kind: "entity", label } as GraphSliceNode);
    }
    for (const r of step.relationships) {
      const [source, target] =
        r.direction === "forward" ? [step.from_entity_id, step.to_entity_id] : [step.to_entity_id, step.from_entity_id];
      const id = relatesToEdgeId(source, r.predicate, target, r.status, r.temporal_state);
      if (!edges.has(id)) {
        edges.set(id, {
          id, kind: "relates_to", source, target, predicate: r.predicate, status: r.status,
          temporal_state: r.temporal_state, valid_from: r.valid_from, valid_until: r.valid_until,
          memory_ids: r.memory_ids, assertion_count: r.assertion_count,
        } as GraphSliceEdge);
      }
    }
  }
  return { ...state, nodes, edges };
}
