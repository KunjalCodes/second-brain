# Phase 16 — Agent continuity and resume context

Labels: [V] verified, [M] measured, [I] inspected, [A] assumption, [L] limitation.

## 1. The behaviour

```text
DAY 1  agent works → meaningful events recorded (decisions, errors, fixes with outcomes, tests, commit, unfinished work)
DAY 2  "Continue where we left off" → get_resume_context() → the agent knows where it stopped and why
```

`GET /projects/{id}/resume` (`get_resume_context`) is a **dedicated continuity operation, not a search**.
Deterministic, bounded, LLM-free; it works with no model configured [V].

## 2. What is returned

`project`, `last_session` and `previous_sessions` (the continuation chain, depth 3), `active_task`,
`unfinished_work`, `recent_decisions` (with the rationale **only if the agent stated it**), `files_inspected`,
`files_modified`, `recent_commits`, `known_bugs` (status from stated outcomes), `previous_errors`
(`fixed_by` = stated `succeeded`; `attempted_fixes` = the rest), `attempts` ("what did I already try?"),
`tests_run`, `important_memories` (hybrid retrieval, optional), `relevant_sources` (event/commit ids for `get_source`),
`ranking`, `limits` (`returned` vs `available` per section) and the untrusted-text `notice`.

## 3. Explicit evidence only (the rule that matters)

| Output | Derived from | Never derived from |
|---|---|---|
| `unfinished_work` `explicit_unfinished` | `unfinished` entries on `SESSION_ENDED` | graph structure, file changes |
| `unfinished_work` `task_blocked` | a `TASK_BLOCKED` event not followed by a `TASK_COMPLETED` for the same feature | anything else |
| `unfinished_work` `unclosed_user_request` | a `USER_REQUEST` with no later `TASK_COMPLETED`/`TASK_BLOCKED` in its session (session not ended `completed`) | |
| `active_task` | newest such unclosed request | |
| attempts / failures | an `approach` label with a fix's stated `outcome`, a test result, or `TASK_BLOCKED` | file changes alone [V] |
| decision reason | the event's `rationale` | inference; `INFERRED_REASON` is reserved and never emitted |
| decision "outcome" | later same-session events touching the files the decision named, labelled `OBSERVED_OUTCOME` with its basis | causation |
| bug status | newest fix event's stated outcome (and any later error naming the bug) | file changes |

A session that was interrupted (never ended) has no `unfinished` list: absence means *not stated*, not *nothing* [V].

## 4. Ranking and bounds (M16, heuristic)

Section order **is** the priority order (heuristic; not learned; not claimed optimal):
unfinished work → active task → last session → recent decisions → unresolved bugs → recent errors → files
modified → files inspected → recent commits → attempts → tests → supporting memories.
Within a section: status first (active decisions before superseded; unresolved bugs before fixed), then count of
relevance reasons (`in_recent_session_chain`, `touches_a_file_this_work_touched`), then recency (valid time).
By default only items **tied to the recent session chain or to files that work touched** are returned
(`include_unrelated=true` opts in to the rest). Caps per section (10/5/15/15/5/5/5/5/5/5/20), lowerable with
`max_items` (1-10), never raisable; text is trimmed to 300 chars; the response is a bounded document
(< 40 KB for a project with hundreds of events [V]; ~5-8 KB for the evaluation scenario [M]).

## 5. Measured (deterministic evaluation, `eval/project_memory_runner.py`) [M]

- Resume recall of the scenario checklist (decisions, attempts, changed files, previous session, explicit unfinished
  items): 100% (H, R, S).
- **Unnecessary context:** before the relevance gate, 3 of 9 returned items (33.3%) came from an unrelated older
  session; after it, 0 of 6 (0.0%). One scenario with one distractor session — a regression guard, not a general
  accuracy claim [L].

## 6. Failure modes and limits [L]

- Quality depends on what the agent records. If it never calls `record_agent_event`, there is nothing to resume.
- "Unclosed request" can mislabel a task the agent finished but did not mark; the basis label says why it is listed.
- `unfinished` items are never auto-resolved by a later session (only blocked tasks resolved by a same-feature completion).
- Relevance reasons are simple (chain membership, file overlap); no semantic relevance is used.
- `important_memories` need the graph; when Neo4j is down they are empty and everything else still works [V].
