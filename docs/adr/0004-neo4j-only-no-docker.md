# ADR 0004 — Neo4j only in Phase 1; managed services; no Docker

**Status:** Accepted (Phase 1)

## Context

The Phase 0 review recommended deferring Neo4j and using PostgreSQL recursive CTEs,
on the grounds that a second stateful service adds operational cost and creates a
dual-write consistency problem. The project owner overrode that and confirmed Neo4j,
targeting **managed Neo4j Aura**, with the frontend on Vercel, the backend on a
managed Python host, and an explicit instruction to avoid Docker and Kubernetes.

## Decision

* **Neo4j is the only datastore in Phase 1.** PostgreSQL/pgvector is not used at all
  until embeddings arrive in Phase 3.
* **No Docker, no compose, no Kubernetes, no Terraform.** Local development runs
  Neo4j natively (Homebrew, or a free Aura instance); the backend runs under
  `uvicorn` directly.
* **Every external dependency is configured by environment variable**, with no
  localhost default in code.

## Rationale

Aura removes the operational objection that drove the deferral recommendation:
managed Neo4j is someone else's uptime problem, and it has a free tier.

Using Neo4j *alone* removes the dual-write problem entirely for this phase. There is
one store, so a memory and its edges commit in one transaction and cannot disagree.
Introducing PostgreSQL alongside it would immediately create the split-brain risk;
that decision is deferred to Phase 3, where embeddings actually require it, and it
needs its own ADR.

Docker earns nothing here: one process plus one managed database is not a
multi-service topology, Vercel and the managed Python hosts build from source
natively, and requiring Docker locally would add a large dependency to the one
workflow that should be frictionless.

## Consequences

* Phase 1 has no relational database, so there is no migration tool yet. The graph
  schema is applied by `scripts/init_graph.py`, deliberately explicit rather than
  implicit at startup.
* The schema uses **only Neo4j Community Edition features**: node property
  uniqueness constraints and range indexes. Node key and property existence
  constraints are Enterprise-only, so required-field validation lives in the service
  layer. Verified against Neo4j 2026.08.1 Community.
* When Phase 3 adds PostgreSQL, the dual-write question must be answered before any
  code is written: which store is canonical, and how they reconcile.
