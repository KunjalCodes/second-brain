# ADR 0017 — Real-LLM evaluation runs are checkpointed and resumable, never blindly retried

**Status:** Accepted (Phase 7)

## Context

Free-tier LLM rate limits have now independently interrupted real-LLM
evaluation twice: Phase 5's 9-task agent evaluation had to be split across two
separate runs on different days ("this API key's free-tier rate limit could
not sustain all 9 tasks... in one run"), and Phase 6's document evaluation
completed only 1 of 5 attempted real-Gemini tasks before hitting `HTTP 429`.
Both times, the fix was manual: re-run the whole harness later and manually
reconcile which results were already good.

Phase 7 needed this to stop being a manual, ad hoc reconciliation each time it
happens (and it has now happened a third time, during Phase 7's own
`retrieval_necessity` evaluation attempt -- see the Phase 7 status report).

## Decision

Every real-LLM evaluation harness invocation (`eval/agent_runner.py`,
`eval/document_runner.py`) is tracked through `eval/persistence.py`'s
`EvaluationRunTracker`:

* `start_or_resume` finds the most recent not-yet-finished `evaluation_runs`
  row for the exact `(harness, dataset_version, mode, llm_provider,
  llm_model)` configuration and continues it, rather than starting over.
* Each task's outcome is persisted immediately after it completes
  (`record_task`), idempotently (`ON CONFLICT (run_id, task_id) DO UPDATE`) --
  a re-attempt overwrites, never duplicates.
* A rate-limited task is recorded with a distinct `RATE_LIMITED` status,
  never conflated with a genuine model failure (`COMPLETED` + `passed=False`)
  or an infrastructure error (`ERRORED`).
* On hitting a rate limit, the harness stops making further real-LLM calls
  for the remainder of that invocation (deterministic baselines for
  remaining tasks still run -- they cost nothing and involve no LLM) rather
  than continuing to retry into the same limit.
* No new retry/backoff logic was added beyond what `LLMClient`'s existing
  provider adapter already bounds -- this ADR is about **not re-attempting
  already-settled work across separate invocations**, not about retrying
  harder within one.

## Consequences

* A later invocation of the same harness against the same dataset and model
  automatically skips every task already `COMPLETED`, spending quota only on
  what is actually still needed.
* The three-times-repeated "manually figure out which results are still
  good" workflow from Phases 5/6 is replaced by a durable, queryable record
  (`eval/report.py --run-id <id>`).
* A partial run's summary (recall, precision, completion counts) is
  persisted and inspectable even while the run remains unfinished --
  `mean_recall`/`mean_precision` etc. are computed only over tasks that
  actually completed, explicit `None`/N/A otherwise, never a fabricated
  zero for an incomplete task.
* This does not, and cannot, guarantee eventual completion -- if a provider's
  free tier is persistently exhausted (as observed during Phase 7's own
  `retrieval_necessity` attempt), the run simply stays resumable indefinitely
  until quota recovers. That is the correct, honest behavior, not a defect
  of this design.
