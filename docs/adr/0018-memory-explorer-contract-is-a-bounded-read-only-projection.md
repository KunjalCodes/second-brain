# ADR 0018 — The Memory Explorer contract is a bounded, read-only graph projection

**Status:** Accepted (Phase 8)

## Context

The Memory Explorer (CLAUDE.md sections 23–25) needs to browse a user's
graph. The only graph endpoint before Phase 8, `GET /entities/{id}/graph`, had
no result bound and no temporal filter. Stage 0 measured it on synthetic hub
graphs (`eval/graph_endpoint_benchmark.py`): at 10k entities, one request at
the default `hops=2` returned the whole user graph (9,922 nodes, 218,238
edges, ~89 MB), with a 45 s median, and several deeper requests hit the 60 s
benchmark timeout. The retrieval-side bound (`RETRIEVAL_GRAPH_MAX_NODES`)
never applied to it, although the API reference implied it did.

## Decision

The explorer reads the graph only through `/graph/*`, which returns
**slices**, never the whole graph:

* One normalized, renderer-agnostic `GraphSlice` shape (nodes + edges, no
  layout, no totals).
* Every read has server-side budgets (nodes, edges, hops, memories per
  entity, evidence rows, timeline length) that a client may lower but never
  raise. Truncation flags are exact: fetch budget + 1.
* Temporal, identity and provenance semantics are computed on the server
  with the existing rules (half-open windows, `BELIEVED_STATUSES`,
  `EXCLUDED_FROM_DEFAULT_RETRIEVAL`, `MERGED_INTO` resolution,
  `ProvenanceService`). `current` is the default; history is always
  explicitly requested (decision D3).
* The API is read-only and never changes storage to suit presentation: no new
  labels, relationship types, indexes, constraints or tables. `RELATES_TO`
  edges are aggregated per `(source, predicate, target, status,
  temporal_state)` at read time only (ADR 0001's storage model is untouched).
* Traversal is breadth-first from Python, one bounded query per hop, so no
  hop bound is ever interpolated into Cypher.

The existing `/entities/{id}/graph` keeps its shape and semantics but gains
measured result caps (`GRAPH_MAX_NODES=1000`, `GRAPH_MAX_EDGES=5000`) and an
additive `truncated` flag (decision D1, made only after Stage 0 measurements).

## Consequences

* A client can never receive an unbounded response from `/graph/*`, and
  always knows whether a slice is complete.
* Incremental exploration (`exclude_ids`) is the intended way to see more.
  Totals ("N more nodes") are deliberately unavailable.
* Window comparisons in Cypher normalise both sides to UTC. Neo4j orders two
  DateTimes at the same instant by zone representation too, and
  merge-copied edges carry a named `UTC` zone. Any future Cypher temporal
  predicate must do the same; the static audit checks this for
  `graph_repository`.
* Rate limiting and authentication are still absent (Phase 9). Because
  `/graph/*` aggregates the most user data per request, it should be their
  first target.
