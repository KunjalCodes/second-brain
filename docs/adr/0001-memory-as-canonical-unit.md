# ADR 0001 — A memory is the canonical unit; relationships are derived from it

**Status:** Accepted (Phase 1)

## Context

`CLAUDE.md` describes memory two ways that do not obviously agree. Section 9 gives a
memory free-text `content` plus a status and validity window. Sections 7–8 and 15
describe entities joined by typed relationships that themselves carry
`valid_from`/`valid_until`. Storing text and storing triples have different schemas:
embedding a bare triple retrieves poorly, while free text has no edge on which to put
a validity window.

## Decision

Both, with one designated as canonical:

* A **Memory** node holds the atomic natural-language statement. It is the unit the
  user wrote, the unit that carries provenance, and the unit that gets superseded.
* Zero or more **`RELATES_TO` relationships** are *derived from* a memory and carry
  `memory_id` pointing back at it.

A relationship never exists without a memory that asserted it. Deriving edges from
memories rather than the reverse means provenance is structural: every edge can name
the statement it came from.

## Consequences

* Provenance and temporal reasoning have a single home, so they cannot disagree.
* Embeddings (Phase 3) attach to `Memory.content`, which is real prose, not a triple.
* Two memories asserting the same fact produce two edges. That is intentional in
  Phase 1 — each assertion keeps its own provenance and window. Recognising them as
  duplicates is Phase 4's duplicate detection, not a storage concern.
