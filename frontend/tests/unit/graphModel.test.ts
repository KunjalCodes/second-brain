import { describe, expect, it } from "vitest";
import { CANVAS_ELEMENT_CAP, emptyGraph, excludeIdsFor, isTruncated, mergeSlice, overCanvasCap, toElements } from "@/lib/graph/model";
import { scopeFor } from "@/components/graph/GraphExplorer";
import { edge, node, slice } from "./fixtures";

describe("mergeSlice", () => {
  it("de-duplicates by server id and keeps the smallest hop distance", () => {
    let s = mergeSlice(emptyGraph(), slice({ nodes: [node("a", { hops: 0 }), node("b", { hops: 2 })], edges: [edge("e1", "a", "b")] }), true);
    s = mergeSlice(s, slice({ nodes: [node("b", { hops: 1 }), node("c", { hops: 2 })], edges: [edge("e1", "a", "b")] }));
    expect(s.nodes.size).toBe(3);
    expect(s.edges.size).toBe(1);
    expect(s.nodes.get("b")!.hops).toBe(1);
  });

  it("reports truncation exactly as the server did and remembers it across expansions", () => {
    let s = mergeSlice(emptyGraph(), slice({ truncated_nodes: true }), true);
    expect(isTruncated(s.lastTruncation)).toBe(true);
    s = mergeSlice(s, slice());
    expect(isTruncated(s.lastTruncation)).toBe(false);
    expect(s.everTruncated).toBe(true);
    s = mergeSlice(s, slice(), true); // a fresh root resets the history
    expect(s.everTruncated).toBe(false);
  });

  it("adopts the server-reported budget (never a client constant)", () => {
    const s = mergeSlice(emptyGraph(), slice({ budget: { max_nodes: 40 } }), true);
    expect(s.budgetMaxNodes).toBe(40);
  });

  it("records a merge redirect only when the server reports one", () => {
    const s = mergeSlice(emptyGraph(), slice({ resolved_from: "old", root: "new" }), true);
    expect(s.resolvedFrom).toEqual({ requested: "old", canonical: "new" });
  });
});

describe("excludeIdsFor", () => {
  it("never sends more exclude ids than the server budget, neighbours first", () => {
    const nodes = Array.from({ length: 10 }, (_, i) => node(`n${i}`));
    const s0 = mergeSlice(emptyGraph(), slice({ budget: { max_nodes: 3 }, nodes, edges: [edge("e", "n0", "n9")] }), true);
    const ids = excludeIdsFor(s0, "n0");
    expect(ids).toHaveLength(3);
    expect(ids[0]).toBe("n9");
    expect(ids).not.toContain("n0");
  });

  it("excludes only entity nodes", () => {
    const s0 = mergeSlice(emptyGraph(), slice({ nodes: [node("a"), node("m", { kind: "memory" })] }), true);
    expect(excludeIdsFor(s0, "x")).toEqual(["a"]);
  });
});

describe("toElements", () => {
  it("skips dangling edges and truncates long memory labels", () => {
    const s = mergeSlice(emptyGraph(), slice({
      nodes: [node("a"), node("m", { kind: "memory", label: "x".repeat(100) })],
      edges: [edge("ok", "a", "m", { kind: "mentions" }), edge("dangling", "a", "gone")],
    }), true);
    const els = toElements(s);
    expect(els.filter((e) => e.group === "edges").map((e) => e.data.id)).toEqual(["ok"]);
    expect(String(els.find((e) => e.data.id === "m")!.data.label).length).toBe(40);
  });
});

describe("canvas cap", () => {
  it("flags when the canvas reaches the element cap", () => {
    const nodes = Array.from({ length: CANVAS_ELEMENT_CAP }, (_, i) => node(`n${i}`));
    expect(overCanvasCap(mergeSlice(emptyGraph(), slice({ nodes }), true))).toBe(true);
  });
});

describe("scopeFor", () => {
  it("never sends as_of with temporal_scope=current", () => {
    expect(scopeFor(null, false)).toEqual({ temporal_scope: "current" });
    expect(scopeFor(null, true)).toEqual({ temporal_scope: "all" });
    expect(scopeFor("2026-03-01T00:00:00.000Z", false)).toEqual({ temporal_scope: "as_of", as_of: "2026-03-01T00:00:00.000Z" });
    expect(scopeFor("2026-03-01T00:00:00.000Z", true)).toEqual({ temporal_scope: "all", as_of: "2026-03-01T00:00:00.000Z" });
  });
});
