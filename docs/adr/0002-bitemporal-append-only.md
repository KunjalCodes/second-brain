# ADR 0002 — Bi-temporal validity, half-open intervals, append-only

**Status:** Accepted (Phase 1)

## Context

`ARCHITECTURE.md` section 13 lists `created_at`, `updated_at`, `valid_from`,
`valid_until` and says "exact date semantics should be carefully defined during
implementation". `CLAUDE.md` section 15 requires that both "what do I use now?" and
"what was I using in March?" answer correctly.

## Decision

**Two independent time axes.**

* *Valid time* — `valid_from` / `valid_until`: when the fact is true in the world.
* *Transaction time* — `created_at` / `updated_at`: when the system recorded it.

**Half-open intervals.** A fact holds at `t` when
`valid_from <= t < valid_until`. `valid_until = NULL` means "still true".

**Append-only.** Nothing is ever deleted and stored text is never rewritten:

* changing a fact creates a **new** memory that `supersedes` the old one; the old
  one gets `status = superseded` and a closed window
* retracting a fact sets `status = invalid` and records a reason
* `PATCH` accepts metadata only — there is no way to edit `content`
* there is no `DELETE` endpoint

**Every fact gets a start.** A memory created without `valid_from` is stamped with
the recording time, so `as_of` queries are total — there is no fact that an
as-of query silently cannot classify.

## Consequences

* Bi-temporality is the one thing that genuinely cannot be retrofitted: if only
  `created_at` is captured, world-time is never recorded and cannot be backfilled.
  Paying for it now is cheap; paying later is impossible.
* Half-open intervals make supersession unambiguous: the replaced fact ends exactly
  where the replacement begins, with neither overlap nor gap. Verified in
  `test_adjacent_windows_neither_overlap_nor_gap`.
* Zero-length windows (`valid_from == valid_until`) are rejected: under a half-open
  interval they describe a fact that is never true.
* Status transitions are an explicit table (`app/models/lifecycle.py`). A closed
  memory cannot become active again — re-asserting a fact creates a new memory,
  which keeps the record intact.
* The storage cost grows monotonically. Acceptable at this scale; consolidation is
  listed as a future job in `ARCHITECTURE.md` section 17.
