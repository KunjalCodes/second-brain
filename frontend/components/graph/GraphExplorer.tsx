"use client";
/**
 * Graph Explorer (docs/PHASE10_PLAN.md §7). Built ONLY on the Phase 8 slice
 * endpoints; read-only; progressive expansion; truncation always visible.
 */
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { graphNeighborhood, graphOverview, graphPaths, type GraphScopeParams } from "@/lib/api/endpoints";
import type { Entity, EntityType, GraphInclude, PathsResponse } from "@/lib/api/types";
import {
  addPath,
  emptyGraph,
  excludeIdsFor,
  filterByPredicate,
  isTruncated,
  mergeSlice,
  newNodeIds,
  overCanvasCap,
  pathHighlight,
  predicatesIn,
  removeNodes,
  toElements,
  CANVAS_ELEMENT_CAP,
  type GraphState,
} from "@/lib/graph/model";
import { useTimeLens } from "@/lib/time/lens";
import { formatDateTime } from "@/lib/format/dates";
import { Button } from "@/components/ui/Button";
import { ErrorState, LoadingRows, PageHeader, TruncationBanner } from "@/components/ui/States";
import type { Selection } from "./CytoscapeCanvas";
import { EdgeDetail, NodeDetail } from "./GraphDetail";
import { EntityPicker } from "./EntityPicker";
import { GraphListView } from "./GraphListView";
import { PathPanel } from "./PathPanel";
import { ENTITY_KIND } from "@/lib/format/glyphs";

const ENTITY_TYPES: EntityType[] = [
  "person", "project", "company", "technology", "concept", "document", "decision", "event", "task", "other",
  // Phase 16: coding-project knowledge.
  "repository", "branch", "directory", "file", "code_symbol", "dependency", "feature", "bug",
  "agent_session", "commit", "change", "error", "fix", "test_run",
];

const CytoscapeCanvas = dynamic(() => import("./CytoscapeCanvas").then((m) => m.CytoscapeCanvas), {
  ssr: false,
  loading: () => <div className="skeleton h-[420px]" role="status" aria-label="Loading graph renderer" />,
});

export type TemporalMode = "current" | "all" | "historical";

export function scopeForMode(asOf: string | null, mode: TemporalMode): GraphScopeParams {
  // `as_of` is only ever sent with temporal_scope=as_of|all|historical, never with current (422).
  if (mode === "historical") return asOf ? { temporal_scope: "historical", as_of: asOf } : { temporal_scope: "historical" };
  if (mode === "all") return asOf ? { temporal_scope: "all", as_of: asOf } : { temporal_scope: "all" };
  return asOf ? { temporal_scope: "as_of", as_of: asOf } : { temporal_scope: "current" };
}

export function scopeFor(asOf: string | null, showAll: boolean): GraphScopeParams {
  return scopeForMode(asOf, showAll ? "all" : "current");
}

export function GraphExplorer() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const focus = params.get("focus");
  const { asOf } = useTimeLens();
  const [showAll, setShowAll] = useState(false);
  const [onlyPast, setOnlyPast] = useState(false);
  const [entityType, setEntityType] = useState<EntityType | "">("");
  const [predicateFilter, setPredicateFilter] = useState<string | null>(null);
  const [includeMemories, setIncludeMemories] = useState(false);
  const [includeMerges, setIncludeMerges] = useState(false);
  const [hops, setHops] = useState<1 | 2>(1);
  // Default to the list view on narrow screens (the canvas remains available).
  // Rendered client-side only (behind the auth gate), so `window` is available.
  const [view, setView] = useState<"canvas" | "list">(() =>
    typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches ? "list" : "canvas",
  );
  const [state, setState] = useState<GraphState>(emptyGraph);
  const [selection, setSelection] = useState<Selection>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [expandError, setExpandError] = useState<{ nodeId: string; err: unknown } | null>(null);
  const [expanding, setExpanding] = useState(false);
  const [renderMs, setRenderMs] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Phase 15: which nodes each expansion added (to undo it), and the path finder.
  const [expanded, setExpanded] = useState<Record<string, string[]>>({});
  const [pathFrom, setPathFrom] = useState<string | null>(null);
  const [pathResult, setPathResult] = useState<{ result: PathsResponse; fromLabel: string; toLabel: string } | null>(null);
  const [pathShown, setPathShown] = useState<number | null>(null);
  const [pathError, setPathError] = useState<unknown>(null);

  const include = useMemo<GraphInclude[]>(
    () => [...(includeMemories ? (["memories"] as const) : []), ...(includeMerges ? (["merges"] as const) : [])],
    [includeMemories, includeMerges],
  );
  const mode: TemporalMode = onlyPast ? "historical" : showAll ? "all" : "current";
  const scope = useMemo(() => scopeForMode(asOf, mode), [asOf, mode]);
  const typeFilter = useMemo(() => (entityType ? { entity_type: [entityType] } : {}), [entityType]);


  const loadEntry = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setStatus("loading");
    setSelection(null);
    setExpanded({});
    setPathFrom(null);
    setPathResult(null);
    setPathShown(null);
    try {
      const slice = focus
        ? await graphNeighborhood(focus, { ...scope, include, hops, ...typeFilter }, ctrl.signal)
        : await graphOverview({ ...scope, include, ...typeFilter }, ctrl.signal);
      setState(mergeSlice(emptyGraph(), slice, true));
      setStatus("ready");
      if (slice.resolved_from && slice.root && focus !== slice.root) {
        router.replace(`${pathname}?focus=${encodeURIComponent(slice.root)}`, { scroll: false });
      }
    } catch (err) {
      if (ctrl.signal.aborted) return;
      setError(err);
      setStatus("error");
    }
  }, [focus, scope, include, hops, typeFilter, router, pathname]);

  useEffect(() => {
    // Fetch-on-change of focus/scope: resetting to "loading" before an external
    // request is the intended synchronisation, not a derived-state cascade.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadEntry();
    return () => abortRef.current?.abort();
  }, [loadEntry]);

  const expand = useCallback(
    async (nodeId: string) => {
      const node = state.nodes.get(nodeId);
      if (!node || node.kind !== "entity" || overCanvasCap(state)) return;
      setExpanding(true);
      setExpandError(null);
      try {
        const slice = await graphNeighborhood(nodeId, {
          ...scope, include, hops, ...typeFilter, exclude_ids: excludeIdsFor(state, nodeId),
        });
        setExpanded((m) => ({ ...m, [nodeId]: newNodeIds(state, slice) }));
        setState((s) => mergeSlice(s, slice));
      } catch (err) {
        // The canvas stays as it was; only this expansion failed.
        setExpandError({ nodeId, err });
      } finally {
        setExpanding(false);
      }
    },
    [state, scope, include, hops, typeFilter],
  );

  const reroot = (id: string) => router.push(`${pathname}?focus=${encodeURIComponent(id)}`);
  const collapse = (nodeId: string) => {
    const added = expanded[nodeId];
    if (!added) return;
    setExpanded((m) => Object.fromEntries(Object.entries(m).filter(([id]) => id !== nodeId)));
    // Never remove the node being collapsed or the focus; only what its expansion added.
    setState((s) => removeNodes(s, added.filter((id) => id !== nodeId && id !== focus)));
    setSelection({ type: "node", id: nodeId });
  };

  const findPathTo = async (target: Entity) => {
    if (!pathFrom) return;
    const fromLabel = state.nodes.get(pathFrom)?.label ?? "entity";
    setPathError(null);
    try {
      const result = await graphPaths({ source_entity_id: pathFrom, target_entity_id: target.id, ...scope });
      setPathResult({ result, fromLabel, toLabel: target.name });
      setPathFrom(null);
      const first = result.paths[0];
      if (first) {
        setPathShown(0);
        setState((s) => addPath(s, first));
      } else {
        setPathShown(null);
      }
    } catch (err) {
      setPathError(err);
    }
  };
  const showPath = (i: number) => {
    const path = pathResult?.result.paths[i];
    if (!path) return;
    setPathShown(i);
    setState((s) => addPath(s, path));
  };
  const closePath = () => {
    setPathFrom(null);
    setPathResult(null);
    setPathShown(null);
    setPathError(null);
  };

  const viewState = useMemo(() => filterByPredicate(state, predicateFilter), [state, predicateFilter]);
  const predicates = useMemo(() => predicatesIn(state), [state]);
  const highlight = useMemo(
    () => (pathResult && pathShown != null && pathResult.result.paths[pathShown] ? pathHighlight(pathResult.result.paths[pathShown]) : null),
    [pathResult, pathShown],
  );
  const elements = useMemo(() => toElements(viewState), [viewState]);
  const selectedNode = selection?.type === "node" ? state.nodes.get(selection.id) : undefined;
  const selectedEdge = selection?.type === "edge" ? state.edges.get(selection.id) : undefined;
  const t = state.lastTruncation;
  const capped = overCanvasCap(state);

  return (
    <div>
      <PageHeader title={focus ? "Graph — neighbourhood" : "Graph — overview"}>
        {focus ? <Button onClick={() => router.push(pathname)}>Back to overview</Button> : null}
        <Button onClick={() => setView((v) => (v === "canvas" ? "list" : "canvas"))} aria-pressed={view === "list"}>
          {view === "canvas" ? "List view" : "Canvas view"}
        </Button>
      </PageHeader>

      <div className="mb-3 max-w-md">
        <EntityPicker
          excludeId=""
          autoFocus={false}
          label="Find an entity to focus on"
          placeholder="Find an entity to focus on… (name or alias)"
          onPick={(e) => reroot(e.id)}
        />
      </div>
      <p
        role="status"
        className="mb-3 inline-flex flex-wrap items-center gap-2 rounded-full border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-1 text-sm"
      >
        <span aria-hidden="true" className="text-[var(--color-accent)]">◷</span>
        <span>
          Knowledge <strong>{asOf ? `as of ${formatDateTime(asOf)}` : "now"}</strong>
        </span>
        <span className="text-xs text-[var(--color-fg-muted)]">
          {mode === "historical"
            ? "· only facts that are no longer true (dashed)"
            : mode === "all"
              ? "· including facts that are no longer true (dashed)"
              : "· facts true at that moment"}
        </span>
      </p>
      <div className="panel mb-3 flex flex-wrap items-center gap-4 p-3 text-sm" role="group" aria-label="Graph options">
        <label className="inline-flex items-center gap-1">
          <input type="checkbox" checked={showAll} onChange={() => setShowAll((v) => !v)} />
          Include no-longer-true facts (labelled)
        </label>
        <label className="inline-flex items-center gap-1">
          <input type="checkbox" checked={includeMemories} onChange={() => setIncludeMemories((v) => !v)} /> Show memories
        </label>
        <label className="inline-flex items-center gap-1">
          <input type="checkbox" checked={includeMerges} onChange={() => setIncludeMerges((v) => !v)} /> Show merge counts
        </label>
        <label className="inline-flex items-center gap-1">
          <input type="checkbox" checked={onlyPast} onChange={() => setOnlyPast((v) => !v)} />
          Only no-longer-true facts
        </label>
        <label className="inline-flex items-center gap-1">
          Entity type
          <select value={entityType} onChange={(e) => setEntityType(e.target.value as EntityType | "")} className="rounded border border-[var(--color-border)] bg-[var(--color-surface)]">
            <option value="">any</option>
            {ENTITY_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        {predicates.length ? (
          <label className="inline-flex items-center gap-1">
            Relationship
            <select value={predicateFilter ?? ""} onChange={(e) => setPredicateFilter(e.target.value || null)} className="rounded border border-[var(--color-border)] bg-[var(--color-surface)]">
              <option value="">all</option>
              {predicates.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
        ) : null}
        <label className="inline-flex items-center gap-1">
          Expand by
          <select value={hops} onChange={(e) => setHops(Number(e.target.value) as 1 | 2)} className="rounded border border-[var(--color-border)] bg-[var(--color-surface)]">
            <option value={1}>1 hop</option>
            <option value={2}>2 hops</option>
          </select>
        </label>
      </div>

      {!focus && status === "ready" ? (
        <p className="mb-2 text-xs text-[var(--color-fg-muted)]">
          Showing <strong>recently updated</strong> entities (up to {state.budgetMaxNodes}) — not “the most important”, and not your whole graph.
        </p>
      ) : null}
      {state.resolvedFrom ? (
        <p role="status" className="mb-2 rounded-md border border-[var(--color-info)] px-3 py-2 text-sm">
          ⧉ The entity you opened was merged into another; showing the merged entity.
        </p>
      ) : null}
      {status === "ready" && (isTruncated(t) || state.everTruncated) ? (
        <div className="mb-2">
          <TruncationBanner>
            {t && isTruncated(t)
              ? `The server stopped at its budget of ${t.maxNodes} nodes${t.maxEdges ? ` / ${t.maxEdges} edges` : ""}.`
              : "An earlier expansion on this canvas was cut at the server's budget."}{" "}
            More connected {t?.memories ? "memories and entities" : "entities"} exist. Expand a specific entity to explore more.
          </TruncationBanner>
        </div>
      ) : null}
      {expandError ? (
        <div className="mb-2">
          <ErrorState error={expandError.err} onRetry={() => void expand(expandError.nodeId)} />
        </div>
      ) : null}
      {capped ? (
        <p role="status" className="mb-2 text-sm text-[var(--color-warn)]">
          This canvas holds {CANVAS_ELEMENT_CAP}+ elements. Re-root on an entity to keep exploring.
        </p>
      ) : null}

      {status === "loading" ? (
        <LoadingRows rows={4} label="Loading graph" />
      ) : status === "error" ? (
        <ErrorState error={error} onRetry={() => void loadEntry()} notFound="This entity doesn't exist, or it isn't yours." />
      ) : state.nodes.size === 0 ? (
        <p className="rounded-lg border border-dashed border-[var(--color-border)] p-6 text-center text-sm">
          Nothing to show in this time scope yet.
        </p>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
          <div className="min-h-[420px] xl:h-[calc(100vh-260px)]">
            {view === "canvas" ? (
              <CytoscapeCanvas
                elements={elements}
                selection={selection}
                onSelect={setSelection}
                onExpand={(id) => void expand(id)}
                onRendered={(ms) => setRenderMs(ms)}
                highlight={highlight}
              />
            ) : (
              <GraphListView state={viewState} onSelect={setSelection} />
            )}
          </div>
          <aside aria-label="Selection details" className="glass fade-in overflow-y-auto p-4 xl:max-h-[calc(100vh-260px)]">
            {pathFrom ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-sm font-semibold">Find a path from {state.nodes.get(pathFrom)?.label ?? "this entity"}</p>
                  <Button onClick={closePath}>Cancel</Button>
                </div>
                <EntityPicker excludeId={pathFrom} onPick={(e) => void findPathTo(e)} />
                {pathError ? <ErrorState error={pathError} /> : null}
              </div>
            ) : pathResult ? (
              <PathPanel
                result={pathResult.result}
                fromLabel={pathResult.fromLabel}
                toLabel={pathResult.toLabel}
                shown={pathShown}
                onShow={showPath}
                onClose={closePath}
              />
            ) : selectedNode ? (
              <NodeDetail
                node={selectedNode}
                onExpand={() => void expand(selectedNode.id)}
                onReroot={() => reroot(selectedNode.id)}
                expandDisabled={expanding || capped}
                onFindPath={selectedNode.kind === "entity" ? () => setPathFrom(selectedNode.id) : undefined}
                onCollapse={expanded[selectedNode.id] ? () => collapse(selectedNode.id) : undefined}
                onFocusEntity={(id) => (state.nodes.has(id) ? setSelection({ type: "node", id }) : reroot(id))}
              />
            ) : selectedEdge ? (
              <EdgeDetail edge={selectedEdge} scope={scope} nodeLabel={(id) => state.nodes.get(id)?.label ?? "entity"} />
            ) : (
              <div className="space-y-2 text-sm text-[var(--color-fg-muted)]">
                <p>Select an entity or a relationship. Double-click an entity (or use “Expand neighbours”) to load more.</p>
                <p className="font-semibold text-[var(--color-fg)]">Legend</p>
                <ul className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs">
                  {(["person", "project", "technology", "document", "decision", "file", "commit", "agent_session", "bug", "fix"] as const).map((t) => (
                    <li key={t}><span aria-hidden="true">{ENTITY_KIND[t]!.glyph}</span> {ENTITY_KIND[t]!.label}</li>
                  ))}
                  <li><span aria-hidden="true">❝</span> Memory (dotted)</li>
                </ul>
                <ul className="space-y-0.5 text-xs">
                  <li>solid line — true at the chosen time</li>
                  <li>dashed line — no longer true (kept, not deleted)</li>
                  <li>dotted line — not yet true</li>
                  <li>orange / thick — disputed</li>
                  <li>thicker line — more memories assert it</li>
                  <li>selecting a node dims everything not connected to it</li>
                </ul>
                <p className="text-xs">
                  {state.nodes.size} nodes · {state.edges.size} edges loaded
                  {renderMs !== null ? ` · last render ${Math.round(renderMs)} ms` : ""}
                </p>
              </div>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
