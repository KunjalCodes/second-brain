# ADR 0033 — Graph intelligence is derived, bounded and labelled; nothing inferred is stored

**Status:** Accepted (Phase 15)

## Context

Phase 15 adds multi-hop paths, explanations, entity context, evolution views
and query-focused context subgraphs. The temptation is to store inferred edges
or causal links so later queries are cheap. That would let a guess become a
"fact" that no user or memory ever stated.

## Decision

- **Projections only.** Every new endpoint is a bounded, read-only projection
  over the existing `Memory` / `Entity` / `Source` graph. No new labels,
  relationship types, indexes or migrations.
- **Nothing inferred is stored.** A multi-step connection is returned with
  `basis: inferred_from_graph`; a one-step relationship is `directly_stated`.
- **Causality only from the stated predicate.** `relation_kind` comes from the
  stored predicate. Sequence or co-occurrence never yields a causal label.
- **Bidirectional, degree-guided, budgeted search** for paths. A one-sided
  search exhausted the node budget on hub graphs. The budget, hop cap (hard 4)
  and path cap (hard 20) are server-side and can only be lowered by a client.
- **Explainable heuristic scores.** Scores are returned with their signals and
  documented as unfitted heuristics.
- **`historical` is one predicate** inside the shared temporal scope fragments,
  so it applies on every hop before any LIMIT.

## Consequences

- No storage growth or migration risk; results are always consistent with the
  stored memories.
- Path queries cost several bounded round trips instead of one stored lookup.
- Causal labels are only as good as the stated predicates; teaching extraction
  to produce them is deferred (needs real-LLM evidence, ADR 0016).
- Score weights and the context similarity threshold are uncalibrated
  heuristics and must be revisited with real embeddings.
