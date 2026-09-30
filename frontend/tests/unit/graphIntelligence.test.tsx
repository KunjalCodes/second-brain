import "./navigation.mock";
import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setTransport } from "@/lib/api/client";
import type { EntityContext, GraphPath, MemoryEvolution, PathsResponse } from "@/lib/api/types";
import { scopeForMode, scopeFor } from "@/components/graph/GraphExplorer";
import { PathPanel } from "@/components/graph/PathPanel";
import { EntityContextPanel } from "@/components/entity/EntityContextPanel";
import { EvolutionPanel } from "@/components/memory/EvolutionPanel";
import { TimeLensProvider } from "@/lib/time/lens";
import {
  addPath,
  emptyGraph,
  filterByPredicate,
  mergeSlice,
  newNodeIds,
  pathHighlight,
  predicatesIn,
  relatesToEdgeId,
  removeNodes,
} from "@/lib/graph/model";
import { fakeTransport, json, edge, node, slice } from "./fixtures";
import { renderWithClient } from "./render";

afterEach(() => setTransport(null));

const INJECTED = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';

function stepRel(over: Record<string, unknown> = {}) {
  return {
    predicate: "uses", direction: "forward", relation_kind: "association", status: "active",
    temporal_state: "current", valid_from: null, valid_until: null, assertion_count: 1,
    confidence: 0.9, memory_ids: ["m1"], basis: ["directly_stated"], ...over,
  };
}

const PATH: GraphPath = {
  entity_ids: ["a", "b", "c"], length: 2, basis: "inferred_from_graph", score: 0.6,
  signals: { length: 2, min_support: 1, current_fraction: 1, min_confidence: 0.9 },
  steps: [
    { from_entity_id: "a", from_name: "Atlas", to_entity_id: "b", to_name: "FastAPI", relationships: [stepRel()] },
    { from_entity_id: "b", from_name: "FastAPI", to_entity_id: "c", to_name: "Pydantic", relationships: [stepRel({ predicate: "depends_on", direction: "backward", memory_ids: ["m2"] })] },
  ],
} as unknown as GraphPath;

describe("temporal mode", () => {
  it("keeps the old scopeFor contract and adds historical", () => {
    expect(scopeFor(null, false)).toEqual({ temporal_scope: "current" });
    expect(scopeFor(null, true)).toEqual({ temporal_scope: "all" });
    expect(scopeFor("2026-03-01T00:00:00Z", false)).toEqual({ temporal_scope: "as_of", as_of: "2026-03-01T00:00:00Z" });
    expect(scopeForMode(null, "historical")).toEqual({ temporal_scope: "historical" });
    expect(scopeForMode("2026-03-01T00:00:00Z", "historical")).toEqual({ temporal_scope: "historical", as_of: "2026-03-01T00:00:00Z" });
  });

  it("never sends as_of together with current (the backend answers 422)", () => {
    expect(scopeForMode(null, "current")).not.toHaveProperty("as_of");
  });
});

describe("graph helpers", () => {
  const base = mergeSlice(
    emptyGraph(),
    slice({
      nodes: [node("a"), node("b"), node("c")],
      edges: [edge("e1", "a", "b", { predicate: "uses" }), edge("e2", "b", "c", { predicate: "depends_on" }), edge("m", "a", "b", { kind: "mentions", predicate: undefined })],
    }),
    true,
  );

  it("filters relationships by predicate without dropping nodes or non-relationship links", () => {
    const f = filterByPredicate(base, "uses");
    expect([...f.edges.keys()].sort()).toEqual(["e1", "m"]);
    expect(f.nodes.size).toBe(3);
    expect(filterByPredicate(base, null)).toBe(base);
    expect(predicatesIn(base)).toEqual(["depends_on", "uses"]);
  });

  it("removes nodes and every edge touching them (collapse)", () => {
    const s = removeNodes(base, ["c"]);
    expect([...s.nodes.keys()]).toEqual(["a", "b"]);
    expect(s.edges.has("e2")).toBe(false);
    expect(s.edges.has("e1")).toBe(true);
    expect(removeNodes(base, [])).toBe(base);
  });

  it("knows which nodes an expansion would add", () => {
    expect(newNodeIds(base, slice({ nodes: [node("c"), node("d")] }))).toEqual(["d"]);
  });

  it("builds path highlight ids exactly as the server names aggregated edges", () => {
    const h = pathHighlight(PATH);
    expect([...h.nodeIds]).toEqual(["a", "b", "c"]);
    // step 1 stored a->b; step 2 was stored backward (c -> b)
    expect(h.edgeIds).toEqual(new Set([relatesToEdgeId("a", "uses", "b", "active", "current"), relatesToEdgeId("c", "depends_on", "b", "active", "current")]));
    expect(relatesToEdgeId("a", "uses", "b", "active", "current")).toBe("relates_to:a:uses:b:active:current");
  });

  it("adds only the missing parts of a path and keeps richer loaded data", () => {
    const rich = mergeSlice(emptyGraph(), slice({ nodes: [node("a", { entity_type: "project", aliases: ["A"] })], truncated_nodes: true }), true);
    const s = addPath(rich, PATH);
    expect(s.nodes.get("a")!.entity_type).toBe("project"); // not overwritten by the bare path node
    expect(s.nodes.has("c")).toBe(true);
    expect(s.edges.size).toBe(2);
    expect(s.everTruncated).toBe(true); // path merge never touches truncation history
    expect(addPath(s, PATH).edges.size).toBe(2); // idempotent
  });
});

describe("PathPanel", () => {
  const result = (over: Partial<PathsResponse>): PathsResponse =>
    ({ source_entity_id: "a", target_entity_id: "c", source_resolved_from: null, target_resolved_from: null, temporal_scope: "current", as_of: "2026-06-01T00:00:00Z", max_hops: 3, max_paths: 5, paths: [PATH], reason: null, explored_nodes: 7, truncated: false, ...over }) as PathsResponse;

  it("labels a multi-step connection as inferred and every step as stored", () => {
    renderWithClient(<PathPanel result={result({})} fromLabel="Atlas" toLabel="Pydantic" shown={0} onShow={() => {}} onClose={() => {}} />);
    expect(screen.getByText("Inferred from the graph")).toBeTruthy();
    expect(screen.getByText("Shown on canvas")).toBeTruthy();
    expect(screen.getByText("—uses→")).toBeTruthy();
    expect(screen.getByText("←depends_on—")).toBeTruthy();
    expect(screen.getByText(/heuristic/i)).toBeTruthy();
  });

  it("says why there is no path, and that a size limit is not proof of absence", () => {
    renderWithClient(<PathPanel result={result({ paths: [], reason: "node_budget_exhausted", truncated: true })} fromLabel="A" toLabel="B" shown={null} onShow={() => {}} onClose={() => {}} />);
    expect(screen.getByRole("status").textContent).toMatch(/does not mean there is none/);
  });
});

describe("EntityContextPanel", () => {
  it("separates stated causes from associations and renders backend text inertly", async () => {
    const ctx = {
      entity: { id: "e1", name: "Adopt PaddleOCR" }, resolved_from: null,
      merged_from: [{ entity_id: "x", name: "Open AI" }], as_of: "2026-06-01T00:00:00Z",
      current_relationships: [
        { other_entity_id: "e2", other_name: "Rotated-label fix", other_entity_type: "event", direction: "outgoing", predicate: "led_to", relation_kind: "causal", status: "active", temporal_state: "current", valid_from: null, valid_until: null, assertion_count: 1, confidence: 0.9, memory_ids: ["m1"] },
        { other_entity_id: "e3", other_name: "Drawing Extractor", other_entity_type: "project", direction: "outgoing", predicate: "made_for", relation_kind: "association", status: "active", temporal_state: "current", valid_from: null, valid_until: null, assertion_count: 1, confidence: 0.9, memory_ids: ["m2"] },
      ],
      historical_relationships: [],
      related_entities: [{ entity_id: "e2", name: "Rotated-label fix", entity_type: "event", predicates: ["led_to"], signals: { current: 1 }, score: 0.9 }],
      memories: [], memory_total: 1, conflicting_memory_total: 2,
      timeline: [{ at: "2026-05-01T00:00:00Z", kind: "began", memory_id: "m1", content: INJECTED, status: "active" }],
      source_type_counts: {}, truncated_relationships: false, truncated_memories: false,
    } as unknown as EntityContext;
    setTransport(fakeTransport(() => json(200, ctx)).transport);
    const { container } = renderWithClient(
      <TimeLensProvider>
        <EntityContextPanel entityId="e1" />
      </TimeLensProvider>,
    );
    expect(await screen.findByText("Stated as a cause")).toBeTruthy();
    expect(screen.getByText(/Also recorded under: Open AI/)).toBeTruthy();
    expect(screen.getByText(/2 memories about this entity are disputed/)).toBeTruthy();
    expect(screen.getByText(INJECTED)).toBeTruthy();
    expect(container.querySelector("img, script")).toBeNull();
    // "made_for" is an association, so it is NOT in the stated-causes list
    expect(screen.getByText(/never treated as cause and effect/)).toBeTruthy();
    expect(screen.queryAllByText("Stated as a cause").length).toBe(1);
  });
});

describe("EvolutionPanel", () => {
  it("shows typed events oldest first and says so when nothing ever changed", async () => {
    const ev = {
      memory_id: "m2", current_memory_ids: ["m2"], truncated: false,
      events: [
        { kind: "new_fact", at: "2026-01-01T00:00:00Z", memory_id: "m1", content: "Atlas uses EasyOCR", status: "superseded", related_memory_ids: [], detail: "first recorded", sources: [] },
        { kind: "supersession", at: "2026-05-01T00:00:00Z", memory_id: "m2", content: "Atlas uses PaddleOCR", status: "active", related_memory_ids: ["m1"], detail: "replaced an earlier memory, which was kept as history", sources: [] },
      ],
    } as unknown as MemoryEvolution;
    setTransport(fakeTransport(() => json(200, ev)).transport);
    renderWithClient(<EvolutionPanel memoryId="m2" />);
    expect(await screen.findByText("Replaced an earlier memory")).toBeTruthy();
    expect(screen.getByText("First recorded")).toBeTruthy();
    expect(screen.getByText("(this memory)")).toBeTruthy();
  });

  it("does not pad a single-event history", async () => {
    const ev = { memory_id: "m1", current_memory_ids: ["m1"], truncated: false, events: [{ kind: "new_fact", at: "2026-01-01T00:00:00Z", memory_id: "m1", content: "x", status: "active", related_memory_ids: [], detail: "first recorded", sources: [] }] } as unknown as MemoryEvolution;
    setTransport(fakeTransport(() => json(200, ev)).transport);
    renderWithClient(<EvolutionPanel memoryId="m1" />);
    expect(await screen.findByText(/Recorded once/)).toBeTruthy();
  });
});
