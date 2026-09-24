# ADR 0015 — Evaluation run history lives in PostgreSQL, never Neo4j

**Status:** Accepted (Phase 7)

## Context

Phase 7 needed durable, comparable history for evaluation-harness invocations:
every prior phase's evaluation results lived only in stdout and hand-transcribed
markdown tables (`PHASE5_PLAN.md`, `PHASE6_PLAN.md`), with no way to detect a
regression automatically or resume a rate-limited real-LLM run across process
invocations. Two new concepts needed a home: `EvaluationRun` (one harness
invocation) and `EvaluationTaskResult` (one task's outcome within it, the
durable checkpoint a resumed run reads).

Since this project's core thesis is a Neo4j-backed persistent memory graph, it
was worth deciding explicitly, rather than by default, which store this
belongs in.

## Decision

Evaluation run/task history is PostgreSQL-only (`evaluation_runs`,
`evaluation_task_results`), under the identical boundary ADR 0006 already drew
for conversations and extraction state: **Neo4j remains exclusively canonical
memory.** Evaluation metadata about how well the system performs is
operational/developer data, not memory, and is never written to Neo4j.

Both tables also have **no `user_id` column at all** -- unlike every other
PostgreSQL table in this project. This is deliberate, not an oversight: an
evaluation run is not owned by a real user, it is a fact about the system as a
whole. `app/repositories/evaluation_repository.py` is therefore exempt from
`UserScopedRepository`, for a different reason than `extraction_queue.py`'s
cross-user-by-design exemption (ADR-adjacent precedent) -- there, the exemption
is about a worker processing every user's backlog in one pass; here, there is
no user dimension to isolate in the first place.

Both tables' content is restricted by design to ids, counts, scores, and short
diagnostic strings (`error_detail`) -- never raw memory or document content,
never a free-text model answer. `tests/unit/test_evaluation_security.py`
statically proves the modules that write these tables have no import path to
Neo4j at all, so "evaluation cannot write canonical memory" is true by
construction.

## Consequences

* A regression comparison (`eval/report.py`) is a plain SQL read over
  historical rows -- no cross-database query, no new consistency concern
  between two stores (unlike, say, the reconciliation sweep ADR 0010 needed
  for cross-database review-decision consistency, which has no analogue here
  since evaluation state never claims to describe Neo4j's current content,
  only past measurements of it).
* A future contributor reaching for "everything about memory quality feels
  graph-adjacent, so put it in Neo4j" is wrong, and this ADR is why.
* Evaluation history can be deleted or reset (e.g. `TRUNCATE evaluation_runs`)
  with zero effect on canonical memory, by construction -- useful for local
  development hygiene.
