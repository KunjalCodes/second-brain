# ADR 0011 — The agent's memory write reuses the extraction pipeline verbatim

**Status:** Accepted (Phase 5)

## Context

Phase 5 gives the agent one write-capable tool, `propose_memory_update`. This is the
single highest-risk addition in the phase: every invariant ADR 0007 established for
passive extraction (the LLM proposes, deterministic code decides, Neo4j is canonical,
duplicates/conflicts are never silently resolved, provenance is always recorded) must
hold for agent-authored writes too, or the agent becomes a second, weaker path to the
same memory store.

Two designs were possible: reimplement grounding/resolution/decision logic for the
agent's own write path, or reuse the existing one. Reimplementing it — even carefully
— would create two decision engines that could drift apart over time, each requiring
its own test suite to prove it still satisfies ADR 0007.

## Decision

`propose_memory_update` builds a real `CandidateMemory` (the identical Pydantic model
passive extraction's LLM output produces) from the agent's proposal, with
`source_message_id`/`source_span` always server-supplied from the turn's real,
already-persisted user message — never taken from the model, the same
server-supplied-context pattern every other tool uses for `user_id`. It then calls
`ExtractionRunner._apply_candidate` directly: the same grounding check, the same
entity-ambiguity flagging, the same duplicate/conflict/supersession classification,
the same Neo4j write, the same embedding step. **There is no second decision engine.**

Because `_apply_candidate` writes its own audit row (an `extraction_candidates` record
keyed to an `extraction_jobs` row), the tool first creates a synthetic one-candidate
`ExtractionJob` via the existing `ExtractionJobRepository.create_job` — the same
idempotency key (`target_message_id`, `extractor_version`) passive extraction uses,
with a distinct `extractor_version` prefix (`agent:...`) so an agent-authored job for
a message never collides with that message's own passive-extraction job. The tool then
performs the same post-`_apply_candidate` bookkeeping `ExtractionRunner.run` does:
`record_candidate`, `mark_applied`, a `MemoryReviewCandidateRepository` row for
`LIKELY_DUPLICATE`/`FLAGGED`/`CONFLICTING` outcomes, and `mark_done` on the job.

The whole write path is feature-gated by `AGENT_WRITE_ENABLED` (default `false`).
While disabled, `propose_memory_update` touches neither database and returns a
structured `REJECTED` result.

## Consequences

* **Every existing invariant is inherited, not reimplemented.** Grounding, entity
  resolution, duplicate/conflict detection, temporal supersession, and provenance for
  agent-authored memories are exercised by the exact same code passive extraction's own
  test suite already covers — `tests/integration/test_agent_write.py` proves the wiring
  (successful/duplicate/conflicting/superseding proposals, provenance preservation,
  entity resolution, cross-user isolation, rejection, and idempotency), not the
  decision logic itself.
* **The audit trail is uniform.** A reviewer inspecting `extraction_candidates` cannot
  tell, from the decision logic alone, whether a row came from passive extraction or
  the agent — only `extractor_version`'s `agent:` prefix and the job's own metadata
  distinguish them. This is deliberate: it is the same memory-write path with a
  different trigger, not a parallel one.
* **The agent cannot bypass any conservative policy in ADR 0007.** It cannot merge
  entities, overwrite an active fact with no change signal (that is `CONFLICTING`, not
  a silent overwrite), or promote a non-persistent statement (`persistence != "persistent"`
  is still `IGNORE`).
* A minor, narrowly-scoped refactor followed from this reuse: `ExtractionRunner`'s
  `_apply_candidate` gained a second legitimate caller. It remains a private method
  (leading underscore) called from within the same package family
  (`app.agent.tools`); it was not renamed, since both call sites already live inside
  the application's own trusted code, not a public API surface.
