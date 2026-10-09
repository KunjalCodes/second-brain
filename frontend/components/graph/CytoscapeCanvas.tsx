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
  const fg = cssVar("--color-fg", "#231f1a");
  const muted = cssVar("--color-muted", "#6a6258");
  const accent = cssVar("--color-accent", "#2f6f6a");
  const warn = cssVar("--color-warn", "#8a5a12");
  const surface = cssVar("--color-surface", "#fdfbf8");
  const border = cssVar("--color-border", "#ddd5c8");
  const tones: Record<string, string> = {
    accent,
    accent2: cssVar("--color-accent-2", "#6b5b8a"),
    current: cssVar("--color-current", "#3f6b4a"),
    warn,
    danger: cssVar("--color-danger", "#a63d4c"),
    info: cssVar("--color-info", "#3f5f86"),
    muted,
  };
  return [
    {
      selector: "node",
      style: {
        // Label = glyph + text. Shape (below) and border carry the type; colour is a hint only.
        label: "data(display)",
        "font-size": 11,
        color: fg,
        "text-valign": "bottom",
        "text-margin-y": 5,
        "text-wrap": "ellipsis",
        "text-max-width": "130px",
        "text-background-color": surface,
        "text-background-opacity": 0.8,
        "text-background-padding": "2px",
        "text-background-shape": "roundrectangle",
        "background-color": surface,
        "border-width": 2,
        "border-color": accent,
        width: 24,
        height: 24,
        "transition-property": "opacity, border-width",
        "transition-duration": 150,
      },
    },
    ...Object.entries(tones).map(([tone, color]) => ({
      selector: `node[tone = "${tone}"]`,
      style: { "border-color": color },
    })),
    { selector: 'node[kind = "entity"]', style: { shape: "round-rectangle", width: 28, height: 22 } },
    // Entity types differ by SHAPE (never by colour alone).
    { selector: 'node[entity_type = "person"]', style: { shape: "ellipse", width: 26, height: 26 } },
    { selector: 'node[entity_type = "project"]', style: { shape: "round-diamond", width: 34, height: 34, "border-width": 3 } },
    { selector: 'node[entity_type = "technology"]', style: { shape: "round-hexagon" } },
    { selector: 'node[entity_type = "document"]', style: { shape: "round-tag" } },
    { selector: 'node[entity_type = "file"]', style: { shape: "rectangle" } },
    { selector: 'node[entity_type = "directory"]', style: { shape: "tag" } },
    { selector: 'node[entity_type = "commit"]', style: { shape: "hexagon" } },
    { selector: 'node[entity_type = "agent_session"]', style: { shape: "octagon", width: 30, height: 30 } },
    { selector: 'node[entity_type = "decision"]', style: { shape: "star", width: 30, height: 30 } },
    { selector: 'node[entity_type = "bug"], node[entity_type = "error"]', style: { shape: "triangle" } },
    { selector: 'node[entity_type = "fix"], node[entity_type = "test_run"]', style: { shape: "vee" } },
    { selector: 'node[entity_type = "code_symbol"], node[entity_type = "dependency"]', style: { shape: "ellipse" } },
    { selector: 'node[entity_type = "repository"], node[entity_type = "branch"]', style: { shape: "barrel" } },
    {
      selector: 'node[kind = "memory"]',
      style: { shape: "ellipse", width: 12, height: 12, "border-color": muted, "font-size": 9, "border-style": "dotted" },
    },
    { selector: 'node[kind = "source"]', style: { shape: "diamond", "border-color": muted } },
    { selector: 'node[merged = "yes"]', style: { "border-style": "double", "border-width": 4 } },
    {
      selector: 'node[status = "superseded"], node[status = "historical"], node[status = "merged"], node[status = "invalid"], node[status = "archived"]',
      style: { opacity: 0.6, "border-style": "dashed" },
    },
    { selector: 'node[status = "conflicting"]', style: { "border-color": warn, "border-width": 3 } },
    {
      selector: "edge",
      style: {
        width: "mapData(weight, 1, 6, 1.4, 4.5)",
        "line-color": muted,
        "line-opacity": 0.75,
        "target-arrow-color": muted,
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.8,
        "curve-style": "bezier",
        label: "data(label)",
        "font-size": 9,
        color: muted,
        "text-rotation": "autorotate",
        "text-background-color": surface,
        "text-background-opacity": 0.85,
        "text-background-padding": "1px",
        "transition-property": "opacity",
        "transition-duration": 150,
      },
    },
    { selector: 'edge[temporal_state = "historical"]', style: { "line-style": "dashed", opacity: 0.55 } },
    { selector: 'edge[temporal_state = "not_yet_valid"]', style: { "line-style": "dotted", opacity: 0.55 } },
    { selector: 'edge[status = "conflicting"]', style: { "line-color": warn, "target-arrow-color": warn, width: 3 } },
    { selector: 'edge[kind = "mentions"]', style: { width: 1, "line-color": border, "target-arrow-shape": "none" } },
    { selector: 'edge[kind = "supersedes"]', style: { "line-style": "dashed", "line-color": accent, "target-arrow-color": accent } },
    { selector: 'edge[kind = "conflicts_with"]', style: { "line-color": warn, "target-arrow-shape": "none", width: 2 } },
    { selector: 'edge[kind = "duplicate_of"]', style: { "line-style": "dotted", "line-color": muted } },
    // A highlighted path (drawn on top; everything else fades). Redundant with the textual
    // path in the side panel -- colour is never the only cue.
    { selector: "node.path", style: { "border-color": accent, "border-width": 4, "z-index": 9 } },
    { selector: "edge.path", style: { "line-color": accent, "target-arrow-color": accent, width: 5, opacity: 1, "z-index": 9 } },
    { selector: ".faded", style: { opacity: 0.25 } },
    // Focus: the selected node's neighbourhood stays prominent, the rest recedes.
    { selector: ".dim", style: { opacity: 0.22 } },
    { selector: ":selected", style: { "border-color": accent, "border-width": 4, "line-color": accent, "target-arrow-color": accent, "z-index": 10 } },
  ] as StylesheetJson;
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
    cy.elements().unselect().removeClass("dim");
    if (selection) {
      const el = cy.getElementById(selection.id);
      if (el.nonempty()) {
        el.select();
        if (selection.type === "node") cy.elements().difference(el.closedNeighborhood()).addClass("dim");
      }
    }
  }, [selection, elements]);

  // Re-read the colour tokens when the OS theme changes.
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => cyRef.current?.style(stylesheet());
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  const zoomBy = (factor: number) => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.zoom({ level: Math.min(3, Math.max(0.15, cy.zoom() * factor)), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  };

  return (
    <div className="relative h-full min-h-[420px] w-full">
      <div
        ref={container}
        role="img"
        aria-label="Graph canvas. Use the list view for a keyboard-accessible version of the same data."
        className="dotgrid h-full min-h-[420px] w-full rounded-[var(--radius-panel)] border border-[var(--color-border)]"
      />
      <div role="group" aria-label="Zoom" className="glass absolute bottom-3 left-3 flex overflow-hidden text-sm">
        <button type="button" aria-label="Zoom in" onClick={() => zoomBy(1.3)} className="px-3 py-1.5 hover:bg-[var(--color-surface-2)]">+</button>
        <button type="button" aria-label="Zoom out" onClick={() => zoomBy(1 / 1.3)} className="border-x border-[var(--color-border)] px-3 py-1.5 hover:bg-[var(--color-surface-2)]">−</button>
        <button type="button" aria-label="Fit to view" onClick={() => cyRef.current?.fit(undefined, 30)} className="px-3 py-1.5 hover:bg-[var(--color-surface-2)]">Fit</button>
      </div>
    </div>
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
