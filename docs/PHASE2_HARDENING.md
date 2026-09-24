# Phase 2 Hardening Pass

**Status:** Complete. Phase 3 (embeddings, pgvector, hybrid retrieval) not started.

This is not a new phase and not a new feature set. Phase 2's approved architecture
(PostgreSQL for application/conversation/extraction state, Neo4j as the sole
canonical memory store, `extraction_candidates` for auditability, the conservative
memory-decision policy, the exact-triple duplicate limitation) is unchanged. This
document records an end-to-end audit of the Phase 2 pipeline performed before
starting Phase 3, the real bugs it found, the fixes applied, and what is
deliberately left as a known, documented limitation rather than "fixed" by adding
infrastructure the current scale does not need.

## 1. Audit scope

Traced one message through the complete pipeline end to end — `POST message →
PostgreSQL → heuristic gate → extraction window → extraction job → worker → LLM →
grounding → candidate → decision engine → Neo4j → provenance` — reading the actual
implementation at every transition (not just the happy-path tests), specifically
for: ownership/user isolation, error handling, transaction boundaries, idempotency,
retry behavior, and failure recovery. Also audited: the Gemini provider boundary,
the background-execution architecture (`BackgroundTasks` + PostgreSQL row locking +
sweep) against realistic failure modes, the bi-temporal model's Cypher/Python
consistency, the evaluation harness's ability to localize a failure, CI, and every
Phase 2 dependency.

Verified empirically, not just by inspection: a full local Neo4j + PostgreSQL
instance was stood up (see section 6, "how this was verified") and the entire test
suite plus the evaluation harness were run against it repeatedly, including a
deliberate, deterministic simulation of a mid-job worker crash.

## 2. Findings and fixes

### 2.1 `ExtractionRunner.run()` did not actually honor its "never raises" contract [Fixed]

**Finding.** The method's docstring promised: "every failure path records itself on
the job and returns, so one bad job cannot crash a worker tick processing several."
In the actual code, the broad exception handling only wrapped the LLM-extraction
step. An unexpected exception raised while persisting or recording an individual
candidate — a transient PostgreSQL error, for instance — propagated out of `run()`
uncaught, which (a) aborted `run_worker_tick`'s whole batch loop, so every other
already-claimed job in that tick was silently skipped until the next tick, and (b)
left the failing job stuck `in_progress` until the sweep's lease-expiry recovery
(up to `EXTRACTION_LEASE_SECONDS`, 600s by default) instead of being marked
`failed` with immediate, bounded backoff.

**Fix.** The per-candidate loop is now wrapped in the same broad exception handling
as the extraction step, marking the job `failed`/retryable on any unexpected error.
`app/extraction/runner.py`.

### 2.2 A mid-job crash could duplicate a relationship-less candidate's memory forever [Fixed]

**Finding.** Every candidate's PostgreSQL bookkeeping (`record_candidate`,
`mark_applied`) was batched into one whole-job transaction, committed only once at
the very end of `run()`. Neo4j writes, by contrast, commit immediately per
statement. Tracing what a crash between those two commit points does:

- For any candidate that asserts a **relationship**, a retry naturally converges to
  a safe `DUPLICATE`/`UPDATE` classification, because the decision engine's
  sibling-edge lookup (`_sibling_edges`) sees the just-committed Neo4j edge as
  already-active and reclassifies accordingly. This is the "decision-engine-level
  natural idempotency" the Phase 2 report described, and it does hold — for this
  case.
- For a **relationship-less candidate** ("bare statement" — a real, documented
  candidate shape: `overall_decision([])` always returns `NEW`, since there is no
  existing edge to compare against), there is nothing for a retry to converge on.
  A crash between the Neo4j write and the Postgres commit meant the next attempt
  reclassified the same candidate `NEW` again and created a second Memory node —
  and would keep doing so, unboundedly, on every subsequent retry. This is a
  genuine, deterministic gap in the idempotency story, not a timing-dependent one:
  `tests/integration/test_extraction_runner.py::TestIdempotency::test_crash_after_first_candidate_survives_and_is_not_duplicated`
  reproduces it exactly (verified failing before the fix, passing after).

**Fix.** Each candidate's PostgreSQL bookkeeping now commits in its own short
session immediately after that candidate's Neo4j write, rather than being batched
into the whole job. This bounds the damage of a crash: every candidate processed
before the crash is durably recorded and will never be reprocessed, so at most the
one candidate in flight when the crash happened can possibly be duplicated —
never repeatedly, and never any candidate before it. `app/extraction/runner.py`
(`ExtractionRunner.run`, `run_worker_tick`).

**Residual, accepted limitation.** A crash landing in the narrow window between a
single candidate's Neo4j write and that same candidate's Postgres commit can still
duplicate *that one* candidate once. Closing this completely would need a
distributed transaction or an outbox/reconciliation pattern across two different
databases — explicitly out of scope ("do not over-engineer"). The fix converts an
unbounded, repeatable duplication into, at most, a single extra memory from an
already-rare event; that is the honest claim, not exactly-once semantics.

### 2.3 `claim_batch` could claim more jobs than `limit` under real conditions [Fixed — genuine correctness bug, not test flakiness]

**Finding.** This is the most significant result of the audit. A test failure
previously on record as "one transient full-suite-only failure... concluded to be
environmental/timing flakiness" (`test_claim_respects_the_limit`) was re-investigated
under section 4's instruction not to dismiss a failure without root-causing it.
Standing up a real local Neo4j + PostgreSQL and running the suite repeatedly
reproduced it, and `EXPLAIN ANALYZE` on the actual SQL showed the mechanism
directly: `claim_batch`'s claim query was shaped as

```sql
UPDATE extraction_jobs
SET status = 'in_progress', ...
WHERE id IN (
  SELECT id FROM extraction_jobs WHERE <claimable> ORDER BY created_at
  LIMIT :limit FOR UPDATE SKIP LOCKED
)
```

PostgreSQL's planner is not obligated to evaluate the `IN (...)` subquery exactly
once. Under certain cost/statistics conditions it chose a nested-loop semi join
that **re-executes** the `LIMIT ... FOR UPDATE SKIP LOCKED` subquery once per
candidate row on the outer table — confirmed directly:
`Subquery Scan on "ANY_subquery" (actual rows=1 loops=5)` against 5 pending jobs
with `limit=2`, and the `UPDATE` affected all 5 rows, not 2. This is exactly why it
looked "flaky": which physical plan the optimizer picks for a logically-ambiguous
`IN (subquery-with-side-effects)` is a function of live table statistics, which
vary between an isolated test run (small table, one plan) and a long full-suite
run (much more churn through the same table, sometimes a different plan) — not of
timing or concurrency. It was a real, deterministic-given-the-chosen-plan SQL
correctness bug the whole time, only made to *look* like non-reproducible noise by
being reported for a single full-suite run and passing every time it happened to be
tested in isolation.

Practical impact: this only affects how many jobs one `claim_batch` call picks up
beyond its stated `limit` (bounded by however many jobs are actually claimable at
that moment) — it does not break the `SKIP LOCKED` cross-worker exclusivity
guarantee (still verified by
`test_concurrent_claims_never_return_the_same_job_twice`, which passed throughout),
so no double-claiming or duplicate processing followed from this specific bug. But
it meant the live path's deliberately small `_LIVE_TICK_LIMIT = 3` (chosen so a
chat response is never blocked on draining a backlog) could not be relied on to
actually stay small, and a claim's `ORDER BY created_at` alone was already a
latent bug in its own right: no tiebreaker for same-timestamp rows (the same class
of bug the codebase's own `list_recent` method was already fixed for and documented,
in `extraction_repository.py`).

**Fix.** `claim_batch`'s claimable set is now a proper CTE (`WITH claimable AS
(...)  UPDATE ... FROM claimable WHERE ...`), which PostgreSQL is guaranteed to
evaluate exactly once, and `ORDER BY created_at, id` adds the missing deterministic
tiebreaker. Verified directly (`EXPLAIN ANALYZE` and repeated live pytest runs)
that this claims exactly `limit` rows regardless of how many same-timestamp jobs
are pending. `app/repositories/extraction_queue.py`.

### 2.4 Internal-endpoint token comparison was not constant-time [Fixed, low severity]

**Finding.** `require_internal_token` compared the supplied `X-Internal-Token`
header with `!=`, which leaks the length of the matching prefix through response
timing. Low severity (this guards an internal operational endpoint, not a
customer-facing one), but free to close.

**Fix.** `hmac.compare_digest`. `app/api/deps.py`.

### 2.5 Evaluation harness collapsed every mismatch into "N candidate(s) recorded" [Fixed, small]

**Finding.** Section 11 of the hardening scope asked whether the evaluation
dataset/harness distinguishes an LLM extraction failure from a decision-engine
failure from a persistence failure. The deterministic harness (by design) cannot
exercise "LLM extraction failure" at all — it scripts `FakeLLMClient` with
gold-standard candidates precisely to isolate decision/persistence correctness from
extraction quality, and a live-provider harness for the latter is explicitly a
later, opt-in, cost-incurring addition. But within what it *does* measure, a
mismatch's `detail` field only reported how many candidates were recorded, not
*why* each one was classified the way it was — discarding information
(`decision_reason`) the pipeline already computes and stores, which is precisely
what distinguishes a grounding rejection from a domain-validation persistence
rejection from an ordinary decision-engine classification difference.

**Fix.** `eval/runner.py` now surfaces each candidate's `decision_reason` in the
failure detail. No new taxonomy invented; the information already existed and was
being thrown away.

## 3. Findings with no code change (confirmed correct, or already covered)

- **Grounding / prompt injection (audit section 7).** `CandidateMemory` has no
  field through which a model could express an action — no ids, no imperatives,
  only typed proposed facts. The worst a compromised or successfully-tricked LLM
  can do is produce grounded, ordinary candidate data, which then goes through the
  normal, reviewable decision path. Added
  `TestGrounding::test_prompt_injection_text_is_treated_as_inert_candidate_data` to
  prove this directly rather than leave it as an architectural claim.
- **Cross-user provenance (audit section 8).** The existing
  `test_cross_user_provenance_is_not_found` already proved the end-to-end path is
  safe (a cross-user memory fetch is rejected before any message lookup happens).
  Added `test_message_lookup_itself_is_user_scoped_defense_in_depth`, which isolates
  the second gate on its own — `MessageRepository.get` itself refuses to resolve a
  message under a different user's scope, independent of the memory-ownership
  check above it, so a future provenance-adjacent code path that skipped that check
  still could not leak a message across users.
- **Gemini boundary (audit section 6).** `google.genai` is imported nowhere outside
  `app/llm/providers/gemini.py` (confirmed by search). The API key and prompt
  content are never logged. The model id is env-configured
  (`LLM_MODEL`), never hardcoded. Malformed structured output is rejected through
  the same Pydantic validation path a fake client's output would go through — the
  SDK's own auto-parsed value is never trusted directly. Timeout is bounded
  (`asyncio.wait_for`); retries are bounded (`LLM_MAX_RETRIES`) and only cover
  transient network/rate-limit/server errors — auth/config failures raise
  immediately, not retried. No change needed.
- **User isolation, both databases (audit section 8).** Every Cypher statement in
  `memory_repository.py`/`entity_repository.py` filters on `$user_id` (statically
  audited by `test_cypher_safety.py`). Every SQL repository goes through
  `UserScopedRepository`'s `_select`/`_update`/`_delete`, which inject the
  `user_id` filter unconditionally (statically audited by `test_sql_safety.py`).
  `MessageRepository.get`/`ConversationRepository.get` fetch by raw primary key
  (bypassing the `_select` filter, since SQLAlchemy's `session.get` doesn't accept
  extra predicates) but explicitly re-check `row.user_id != self._user_id` before
  returning — verified by direct test, not just by reading the code.
- **Temporal semantics (audit section 9).** The bi-temporal model is unchanged
  (no correctness bug found). The previously-identified duplication between
  Python's `is_valid_at`/`is_currently_believed` and the Cypher `as_of` filter
  fragment in `MemoryRepository.FILTER_FRAGMENTS` is real and is **deliberately
  kept, not unified**: the two are necessarily separate implementations (Cypher and
  Python cannot share code), and moving `as_of` filtering to application-side
  Python would require fetching unbounded rows before pagination, which is a
  real regression, not a simplification. What was missing was a live proof that
  the two definitions still agree at the exact boundary instant, not just at a
  comfortable midpoint — added
  `test_as_of_boundary_is_half_open_in_cypher_too`, which asserts the Cypher side
  is inclusive-start/exclusive-end at the *exact* supersession timestamp. This is
  what would catch the two definitions drifting apart in the future.
- **CI (audit section 12).** Confirmed directly: default CI (`ci.yml`) runs Ruff,
  the unit suite, and both the Cypher and SQL safety audits, with no database and
  no Docker, and a dedicated step (`Verify integration tests skip without
  services`) fails the build if integration tests ever stop skipping cleanly.
  `integration.yml` is a separate, opt-in workflow requiring `NEO4J_URI`/
  `DATABASE_URL` repository secrets (managed Aura + managed Postgres), runs
  weekly or on manual dispatch, and independently no-ops per store if only one
  secret is configured. No Gemini credentials are required anywhere in CI. No
  change needed.
- **Dependencies (audit section 13).** `sqlalchemy[asyncio]`, `asyncpg`, `alembic`
  (PostgreSQL), `google-genai` (isolated to one file). No LangChain/LangGraph, no
  Celery/Redis, no vector database. Every dependency is used and none is
  redundant with the standard library for what it does. No change needed.

## 4. Background-execution architecture: still sufficient, with one caveat

The question the hardening scope asked directly: is `BackgroundTasks` +
PostgreSQL row locking + sweep still appropriate, or does this demonstrate a
concrete need for Redis/Celery?

- **Process restart / mid-request death.** A `BackgroundTask` is scheduled after
  the response commits (the message and its job/debounce state are already
  durably in PostgreSQL by then) but runs in-process; if Railway restarts the
  process before that task runs, the job is simply never picked up by that
  now-dead task. Nothing is lost: the job sits `pending` in PostgreSQL, and the
  next live message on any conversation, or the next scheduled sweep tick,
  claims it normally. This is exactly what the sweep exists for.
- **A worker dies mid-job.** Covered by the lease mechanism
  (`EXTRACTION_LEASE_SECONDS`, default 600s) and `reclaim_expired_leases`, and —
  after section 2.2's fix — bounded to at most one duplicated candidate rather
  than unbounded duplication.
- **Multiple backend instances.** `claim_batch`'s `FOR UPDATE SKIP LOCKED` (now
  correctly limited, section 2.3) is what actually makes this safe: two
  instances' worker ticks, or the live path and the sweep firing at once, can
  never claim the same job. Horizontal scaling does not require any queue
  beyond what is already there.
- **The one caveat, not fixed here because it is not yet a real problem:** the
  lease has no renewal/heartbeat. If a single job's processing ever legitimately
  took longer than `EXTRACTION_LEASE_SECONDS`, a second worker could claim and
  reprocess it as if the first had died. At today's actual costs — one LLM call
  bounded by `LLM_TIMEOUT_SECONDS` (30s default) plus at most one repair retry,
  well under the 600s lease — this is not reachable in practice, so a
  heartbeat/lease-renewal mechanism is deliberately **not** added now; it is
  listed as a Phase 3+ prerequisite only if `EXTRACTION_WINDOW_SIZE` or LLM
  latency ever grow enough to make it plausible.

**Conclusion: `BackgroundTasks` + PostgreSQL row locking + sweep remains
sufficient for the current scope and scale.** No queue system is warranted by
anything this audit found.

## 5. Tests added

- `tests/integration/test_extraction_runner.py`:
  `TestIdempotency::test_crash_after_first_candidate_survives_and_is_not_duplicated`
  (deterministic mid-job-crash simulation via a testing-only `_after_candidate`
  hook — mirrors the existing `now_fn`/`id_fn` injection pattern),
  `TestGrounding::test_prompt_injection_text_is_treated_as_inert_candidate_data`.
- `tests/integration/test_provenance.py`:
  `test_message_lookup_itself_is_user_scoped_defense_in_depth`.
- `tests/integration/test_memory_lifecycle.py`:
  `test_as_of_boundary_is_half_open_in_cypher_too`.
- `tests/integration/test_extraction_queue.py`: strengthened
  `test_claim_respects_the_limit` with a docstring explaining it is the regression
  test for section 2.3 (the test itself was already correctly shaped; the bug was
  in the code it exercises, not in test coverage).

No test was skipped or weakened to make a failure go away — `test_claim_respects_the_limit`
and `TestLeaseReclaim::test_expired_lease_is_reclaimed_and_becomes_claimable` were
both actually failing during this pass; both were root-caused (sections 2.3 and 6,
respectively) rather than accepted as flaky.

## 6. How this was verified

A throwaway local Neo4j (fresh data directory, isolated port, disposed afterward)
and a fresh PostgreSQL database were used to run the full suite, the evaluation
harness, and a direct `EXPLAIN ANALYZE` reproduction of the claim-query bug,
repeatedly, rather than reasoning about the fixes from code alone. All temporary
infrastructure and test data were torn down afterward; no leftover rows were found
in either store after the final run (verified directly, both databases empty of
`itest-`/`eval-` prefixed data).

## 7. Final verification

Against live Neo4j + PostgreSQL, five consecutive full-suite runs after all fixes:

```text
383 unit tests
111 integration tests (107 existing + 4 new)
494 total
494 passed, 0 failed, 0 skipped
```

Evaluation harness: 10/10 scenarios, 100% decision accuracy, 0 wrong merges —
rerun live after the fixes, not carried over from the Phase 2 report.

Ruff: clean across `app`, `tests`, `scripts`, `eval`. Cypher safety audit
(`test_cypher_safety.py`) and SQL safety audit (`test_sql_safety.py`): both pass,
unchanged in scope.

Without live services configured (the CI-default profile): unit tests plus every
non-integration test pass, and integration tests skip cleanly, matching the
`ci.yml` guard exactly.

## 8. Unresolved issues / deliberate limitations carried forward

- **Exact-triple duplicate detection only** (unchanged from Phase 2, by design —
  see docs/PHASE2_PLAN.md). Not touched in this pass; embeddings/semantic
  similarity remain explicitly Phase 3's job.
- **The narrow single-candidate crash window** described in section 2.2 — reduced
  from "unbounded repeated duplication" to "at most one duplicate, only in an
  already-rare crash window." Closing it fully needs a distributed-transaction or
  outbox pattern, which is not justified at current scale.
- **No lease renewal/heartbeat** (section 4) — not a problem at current LLM-call
  latencies; a real prerequisite only if per-job processing time grows materially.
- **`google-genai`'s own retry/backoff behavior underneath the adapter's explicit
  bounds** was not independently audited beyond confirming the adapter's own
  timeout and retry bounds are respected; this is a third-party SDK internal, not
  application code.

## 9. Deployment implications

None of the fixes change the deployment story from the Phase 2 report: still no
Docker, still Railway-targetable as-is, still `$PORT`-agnostic. The `claim_batch`
fix (section 2.3) makes horizontal scaling to multiple Railway instances *safer*
than it was — the claim-exclusivity guarantee was already correct, but the
claimed-batch-size guarantee was not, and the latter matters more as soon as more
than one process is polling the same queue.

## 10. Phase 3 prerequisites

Nothing here blocks starting Phase 3 (embeddings, pgvector, hybrid retrieval) as
already planned. The one item worth carrying into that work rather than treating
as closed: if Phase 3's extraction calls become slower or more numerous (larger
windows, an added embedding step per candidate), re-check whether
`EXTRACTION_LEASE_SECONDS` still comfortably exceeds realistic processing time —
that is the one place this pass identified a scale-dependent assumption rather
than a present-day bug.
