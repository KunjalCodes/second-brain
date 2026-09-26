# ADR 0027 — Graph visualization uses Cytoscape.js with an owned wrapper

**Status:** Accepted (Phase 10)

## Context

The Graph Explorer renders Phase 8 `GraphSlice` responses. These are
bounded server slices with exact truncation flags, a server-computed
`temporal_state` per node and edge, and merge redirects. The renderer must
never widen those budgets or infer temporal state itself, and the graph
must be usable without a pointer or a large screen.

## Decision

* **Cytoscape.js 3.34**, which ships its own types, wrapped in one
  component (`components/graph/CytoscapeCanvas.tsx`).
  * It is imported with `import()` only when the canvas mounts. Measured:
    it is the largest chunk (430 KB raw / 134 KB gzip) and is not loaded
    on any other page.
  * Styling is selector-driven from server fields (`kind`, `status`,
    `temporal_state`), mapped in `lib/graph/model.ts:toElements`.
* **Client graph state is bookkeeping only** (`lib/graph/model.ts`):
  * slices merge by server id;
  * truncation flags are kept exactly as reported, and "an earlier
    expansion was truncated" is remembered until re-rooting;
  * `exclude_ids` never exceeds the server-reported `budget.max_nodes`
    (the server returns 422 above that), sending neighbours first.
* **Truncation is always visible.** The banner cannot be dismissed.
* **The canvas cap is 600 elements.** Beyond it the UI asks the user to
  re-root rather than keep expanding.
* **A list view is always available**, as tables of entities,
  relationships and memories. It is the keyboard / screen-reader view, and
  the default below 768 px width.
* **Edge evidence** comes from `/graph/edges/evidence`. Each assertion's
  `evidence_status` is labelled (including `memory_excluded`), and an
  "include history" toggle switches to `temporal_scope=all`.
* **Read-only.** The graph offers no memory mutation.

## Consequences

Measured layout cost (not a budget):

| Measurement | Result |
|---|---|
| Browser, one server slice at the default budget (150 nodes, 149 edges) | ~390 ms median sync + layout, excluding paint |
| Headless, cose first layout, 150 elements | ~100 ms |
| Headless, cose first layout, 400 elements | ~690 ms |
| Headless, cose first layout, 600 elements | ~1.6 s |

* A full re-layout near the canvas cap blocks the main thread for over a
  second. Incremental expansions lay out only the new elements plus their
  neighbours.
* Moving layout to a web worker is deferred.
* No second graph library was added.
