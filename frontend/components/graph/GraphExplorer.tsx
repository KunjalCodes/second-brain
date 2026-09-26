"use client";
/**
 * Graph Explorer (docs/PHASE10_PLAN.md §7). Built ONLY on the Phase 8 slice
 * endpoints; read-only; progressive expansion; truncation always visible.
 */
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { graphNeighborhood, graphOverview, type GraphScopeParams } from "@/lib/api/endpoints";
import type { GraphInclude } from "@/lib/api/types";
import {
  emptyGraph,
  excludeIdsFor,
  isTruncated,
  mergeSlice,
  overCanvasCap,
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
import { GraphListView } from "./GraphListView";

const CytoscapeCanvas = dynamic(() => import("./CytoscapeCanvas").then((m) => m.CytoscapeCanvas), {
  ssr: false,
  loading: () => <div className="skeleton h-[420px]" role="status" aria-label="Loading graph renderer" />,
});

export function scopeFor(asOf: string | null, showAll: boolean): GraphScopeParams {
  // `as_of` is only ever sent with temporal_scope=as_of|all, never with current (422).
  if (asOf) return { temporal_scope: showAll ? "all" : "as_of", as_of: asOf };
  return { temporal_scope: showAll ? "all" : "current" };
}

export function GraphExplorer() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const focus = params.get("focus");
  const { asOf } = useTimeLens();
  const [showAll, setShowAll] = useState(false);
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

  const include = useMemo<GraphInclude[]>(
    () => [...(includeMemories ? (["memories"] as const) : []), ...(includeMerges ? (["merges"] as const) : [])],
    [includeMemories, includeMerges],
  );
  const scope = useMemo(() => scopeFor(asOf, showAll), [asOf, showAll]);


  const loadEntry = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setStatus("loading");
    setSelection(null);
    try {
      const slice = focus
        ? await graphNeighborhood(focus, { ...scope, include, hops }, ctrl.signal)
        : await graphOverview({ ...scope, include }, ctrl.signal);
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
  }, [focus, scope, include, hops, router, pathname]);

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
        const slice = await graphNeighborhood(nodeId, { ...scope, include, hops, exclude_ids: excludeIdsFor(state, nodeId) });
        setState((s) => mergeSlice(s, slice));
      } catch (err) {
        // The canvas stays as it was; only this expansion failed.
        setExpandError({ nodeId, err });
      } finally {
        setExpanding(false);
      }
    },
    [state, scope, include, hops],
  );

  const reroot = (id: string) => router.push(`${pathname}?focus=${encodeURIComponent(id)}`);
  const elements = useMemo(() => toElements(state), [state]);
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

      <div className="mb-3 flex flex-wrap items-center gap-4 text-sm" role="group" aria-label="Graph options">
        <span>
          Time: <strong>{asOf ? `as of ${formatDateTime(asOf)}` : "now"}</strong>
        </span>
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
              />
            ) : (
              <GraphListView state={state} onSelect={setSelection} />
            )}
          </div>
          <aside aria-label="Selection details" className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
            {selectedNode ? (
              <NodeDetail
                node={selectedNode}
                onExpand={() => void expand(selectedNode.id)}
                onReroot={() => reroot(selectedNode.id)}
                expandDisabled={expanding || capped}
              />
            ) : selectedEdge ? (
              <EdgeDetail edge={selectedEdge} scope={scope} nodeLabel={(id) => state.nodes.get(id)?.label ?? "entity"} />
            ) : (
              <div className="space-y-2 text-sm text-[var(--color-fg-muted)]">
                <p>Select an entity or a relationship. Double-click an entity (or use “Expand neighbours”) to load more.</p>
                <p className="font-semibold text-[var(--color-fg)]">Legend</p>
                <ul className="space-y-0.5 text-xs">
                  <li>▭ entity · ● memory</li>
                  <li>solid line — true at the chosen time</li>
                  <li>dashed line — no longer true (kept, not deleted)</li>
                  <li>dotted line — not yet true</li>
                  <li>orange / thick — disputed</li>
                  <li>thicker line — more memories assert it</li>
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
