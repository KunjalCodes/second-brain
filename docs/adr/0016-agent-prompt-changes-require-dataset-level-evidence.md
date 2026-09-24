# ADR 0016 — Agent system-prompt changes require dataset-level evidence, never a single-task patch

**Status:** Accepted (Phase 7)

## Context

Phase 5's real-LLM evaluation found one genuine, honestly-reported model-
judgment gap: the `ambiguous_entity` task scored 0% recall because the real
Gemini model chose to answer "What does OpenAI Labs work on?" from its own
general/parametric knowledge rather than calling `search_memory` to check the
user's seeded, distinctly-named memory. `PHASE5_PLAN.md` explicitly declined
to patch this with task-specific prompt language, and instead deferred it:
"worth watching in a future evaluation expansion."

Phase 7 was directed to inspect this properly: generalize the single anecdote
into a measured `retrieval_necessity` category (≥5 tasks, including negative
controls -- cases where the agent correctly should *not* retrieve), run it
against the real LLM, and only change `app/agent/prompts.py`'s `SYSTEM_PROMPT`
if the broader evaluation demonstrated a *systematic* problem, never to make
one test case pass.

This is worth codifying as an ADR because the temptation to "fix" a single
failing eval task with narrowly-targeted prompt wording is real, easy, and
exactly the failure mode that would make future evaluation numbers
meaningless (a system that scores well only because its prompt was
retroactively shaped around the test set is not measuring anything).

## Decision

A change to `SYSTEM_PROMPT` motivated by evaluation results is only accepted
when:

1. The evidence comes from a **named, multi-task category** in a checked-in
   dataset (`eval/datasets/agent_v1.jsonl`'s `retrieval_necessity` category is
   the first instance) -- never from a single task.
2. The category includes **negative controls** -- tasks where the correct
   agent behavior is to *not* retrieve -- so a prompt change cannot be
   validated by a category that only rewards "always search."
3. The prompt wording added is **general-purpose**, never naming a specific
   entity, company, or task from the dataset (mirrors `app/agent/prompts.py`'s
   own existing, pre-Phase-7 comment: "never task-specific").
4. The change is verified by re-running the **full existing** agent dataset
   (`agent_v1.jsonl` in its entirety, not just the new category) and shown not
   to regress any other category.
5. Both the before and after measurements are real, measured numbers from a
   real run -- never assumed, never illustrative.

If evidence for (1)-(2) cannot be obtained (e.g. real-LLM evaluation is
blocked by provider rate-limiting), the prompt is **not** changed on the
strength of the original single-task anecdote alone, and that fact is
recorded plainly rather than treated as equivalent to "evidence found no
problem."

## Consequences

* Phase 7's own attempt to gather `retrieval_necessity` real-LLM evidence was
  blocked by persistent Gemini free-tier rate-limiting (0 of 6 tasks
  completed across three attempts). Per this ADR, the system prompt was
  **not** modified -- the single Phase 5 anecdote does not meet the bar, and
  "we couldn't get real evidence" is reported as exactly that, not smoothed
  into "evidence showed no problem."
* A future session with working quota can resume the exact same run
  (`eval/persistence.py`'s checkpoint) and, if the broader category then
  shows a systematic gap, apply this ADR's process to decide on a prompt
  change with real evidence.
* This ADR generalizes beyond `retrieval_necessity` -- it is the standing
  rule for any future agent-behavior-motivated prompt change, not a one-off
  decision about this one category.
