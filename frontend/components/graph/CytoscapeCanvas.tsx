"use client";
/**
 * Thin, owned React wrapper around Cytoscape.js (ADR 0027). Cytoscape is
 * imported lazily so only the graph route pays for it. Labels are canvas
 * text -- never HTML -- so backend strings can't become markup.
 */
import { useEffect, useLayoutEffect, useRef } from "react";
import type { Core, ElementDefinition, StylesheetJson } from "cytoscape";
import type { CyElement } from "@/lib/graph/model";

export type Selection = { type: "node" | "edge"; id: string } | null;

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function stylesheet(): StylesheetJson {
  const fg = cssVar("--color-fg", "#14181f");
  const muted = cssVar("--color-muted", "#5b6472");
  const accent = cssVar("--color-accent", "#2b59c3");
  const warn = cssVar("--color-warn", "#9a5b00");
  const surface = cssVar("--color-surface", "#ffffff");
  const border = cssVar("--color-border", "#d9dde3");
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "font-size": 11,
        color: fg,
        "text-valign": "bottom",
        "text-margin-y": 4,
        "text-wrap": "ellipsis",
        "text-max-width": "120px",
        "background-color": surface,
        "border-width": 2,
        "border-color": accent,
        width: 22,
        height: 22,
      },
    },
    { selector: 'node[kind = "entity"]', style: { shape: "round-rectangle", width: 26, height: 20 } },
    // Phase 16: entity types differ by SHAPE (never by colour alone).
    { selector: 'node[entity_type = "file"]', style: { shape: "rectangle" } },
    { selector: 'node[entity_type = "directory"]', style: { shape: "tag" } },
    { selector: 'node[entity_type = "commit"]', style: { shape: "hexagon" } },
    { selector: 'node[entity_type = "agent_session"]', style: { shape: "octagon" } },
    { selector: 'node[entity_type = "decision"]', style: { shape: "star" } },
    { selector: 'node[entity_type = "bug"], node[entity_type = "error"]', style: { shape: "triangle" } },
    { selector: 'node[entity_type = "fix"], node[entity_type = "test_run"]', style: { shape: "vee" } },
    { selector: 'node[entity_type = "code_symbol"], node[entity_type = "dependency"]', style: { shape: "ellipse" } },
    { selector: 'node[entity_type = "repository"], node[entity_type = "branch"]', style: { shape: "barrel" } },
    { selector: 'node[kind = "memory"]', style: { shape: "ellipse", width: 12, height: 12, "border-color": muted, "font-size": 9 } },
    { selector: 'node[kind = "source"]', style: { shape: "diamond", "border-color": muted } },
    { selector: 'node[merged = "yes"]', style: { "border-style": "double", "border-width": 4 } },
    {
      selector: 'node[status = "superseded"], node[status = "historical"], node[status = "merged"], node[status = "invalid"], node[status = "archived"]',
      style: { opacity: 0.6 },
    },
    { selector: 'node[status = "conflicting"]', style: { "border-color": warn } },
    {
      selector: "edge",
      style: {
        width: "mapData(weight, 1, 6, 1.5, 5)",
        "line-color": muted,
        "target-arrow-color": muted,
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        label: "data(label)",
        "font-size": 9,
        color: muted,
        "text-rotation": "autorotate",
        "text-background-color": surface,
        "text-background-opacity": 0.85,
        "text-background-padding": "1px",
      },
    },
    { selector: 'edge[temporal_state = "historical"]', style: { "line-style": "dashed", opacity: 0.55 } },
    { selector: 'edge[temporal_state = "not_yet_valid"]', style: { "line-style": "dotted", opacity: 0.55 } },
    { selector: 'edge[status = "conflicting"]', style: { "line-color": warn, "target-arrow-color": warn, width: 3 } },
    { selector: 'edge[kind = "mentions"]', style: { width: 1, "line-color": border, "target-arrow-shape": "none" } },
    { selector: 'edge[kind = "supersedes"]', style: { "line-style": "dashed", "line-color": accent, "target-arrow-color": accent } },
    { selector: 'edge[kind = "conflicts_with"]', style: { "line-color": warn, "target-arrow-shape": "none", width: 2 } },
    { selector: 'edge[kind = "duplicate_of"]', style: { "line-style": "dotted", "line-color": muted } },
    // Phase 15: a highlighted path (drawn on top; everything else fades). Redundant
    // with the textual path in the side panel -- colour is never the only cue.
    { selector: "node.path", style: { "border-color": accent, "border-width": 4, "z-index": 9 } },
    { selector: "edge.path", style: { "line-color": accent, "target-arrow-color": accent, width: 5, opacity: 1, "z-index": 9 } },
    { selector: ".faded", style: { opacity: 0.25 } },
    { selector: ":selected", style: { "border-color": accent, "border-width": 4, "line-color": accent, "target-arrow-color": accent, "z-index": 10 } },
  ];
}

export function CytoscapeCanvas({
  elements,
  selection,
  onSelect,
  onExpand,
  onRendered,
  highlight,
}: {
  elements: CyElement[];
  selection: Selection;
  onSelect: (s: Selection) => void;
  /** Node and edge ids of a path to emphasise; everything else is faded. */
  highlight?: { nodeIds: Set<string>; edgeIds: Set<string> } | null;
  onExpand?: (nodeId: string) => void;
  onRendered?: (ms: number, count: number) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const handlers = useRef({ onSelect, onExpand, onRendered });
  const elementsRef = useRef(elements);
  useLayoutEffect(() => {
    handlers.current = { onSelect, onExpand, onRendered };
    elementsRef.current = elements;
  });

  // Create once.
  useEffect(() => {
    let destroyed = false;
    void import("cytoscape").then(({ default: cytoscape }) => {
      if (destroyed || !container.current) return;
      const cy = cytoscape({
        container: container.current,
        style: stylesheet(),
        minZoom: 0.15,
        maxZoom: 3,
        wheelSensitivity: 0.25,
        boxSelectionEnabled: false,
      });
      cy.on("tap", "node", (evt) => handlers.current.onSelect({ type: "node", id: evt.target.id() }));
      cy.on("tap", "edge", (evt) => handlers.current.onSelect({ type: "edge", id: evt.target.id() }));
      cy.on("tap", (evt) => {
        if (evt.target === cy) handlers.current.onSelect(null);
      });
      cy.on("dbltap", "node", (evt) => handlers.current.onExpand?.(evt.target.id()));
      cyRef.current = cy;
      sync(cy, elementsRef.current, handlers.current.onRendered, container.current);
    });
    return () => {
      destroyed = true;
      cyRef.current?.destroy();
      cyRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (cyRef.current) sync(cyRef.current, elements, handlers.current.onRendered, container.current);
  }, [elements]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("path faded");
    if (highlight && (highlight.nodeIds.size || highlight.edgeIds.size)) {
      cy.elements().addClass("faded");
      cy.nodes().filter((n) => highlight.nodeIds.has(n.id())).removeClass("faded").addClass("path");
      cy.edges().filter((e) => highlight.edgeIds.has(e.id())).removeClass("faded").addClass("path");
    }
  }, [highlight, elements]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().unselect();
    if (selection) {
      const el = cy.getElementById(selection.id);
      if (el.nonempty()) el.select();
    }
  }, [selection]);

  return (
    <div
      ref={container}
      role="img"
      aria-label="Graph canvas. Use the list view for a keyboard-accessible version of the same data."
      className="h-full min-h-[420px] w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]"
    />
  );
}

/** Add new / remove stale elements, then lay out (new elements around the fixed old ones). */
function sync(
  cy: Core,
  elements: CyElement[],
  onRendered?: (ms: number, count: number) => void,
  container?: HTMLElement | null,
): void {
  const start = performance.now();
  const wanted = new Set(elements.map((e) => String(e.data.id)));
  cy.elements().forEach((el) => {
    if (!wanted.has(el.id())) el.remove();
  });
  const existing = new Set(cy.elements().map((el) => el.id()));
  const added = elements.filter((e) => !existing.has(String(e.data.id))) as ElementDefinition[];
  // Update data of existing elements (e.g. temporal_state after a scope change).
  for (const e of elements) {
    const el = cy.getElementById(String(e.data.id));
    if (el.nonempty() && existing.has(el.id())) el.data(e.data);
  }
  if (added.length) {
    const firstRender = existing.size === 0;
    const newEls = cy.add(added);
    const layout = firstRender
      ? cy.layout({ name: "cose", animate: false, nodeDimensionsIncludeLabels: true, randomize: true })
      : newEls.union(newEls.connectedNodes()).layout({ name: "cose", animate: false, randomize: false, fit: false });
    layout.run();
    if (firstRender) cy.fit(undefined, 30);
  }
  const syncMs = performance.now() - start;
  const count = cy.elements().length;
  onRendered?.(syncMs, count);
  // Phase 11 (M10): stage timings for performance measurement, readable by
  // tests without any visible UI change -- layout (sync, incl. cose) and paint
  // (time until two animation frames have been produced after layout).
  if (container && added.length) {
    const painted = performance.now();
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        container.dataset.layoutMs = syncMs.toFixed(1);
        container.dataset.paintMs = (performance.now() - painted).toFixed(1);
        container.dataset.elements = String(count);
      }),
    );
  }
}
