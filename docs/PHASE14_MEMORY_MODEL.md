# Phase 14 — The Memory Model As Implemented (M0 audit, 2026-09-29)

This describes what the code **does** at `b195fff`, before any Phase 14
change. What Phase 14 changed, and the measured effect, is in
`docs/PHASE14_REPORT.md`. Planned behaviour is not described as implemented.

**Evidence labels:**

| Label | Meaning |
|---|---|
| [V] | Observed in the live trace (§0) or by a test that ran |
| [I] | Inspected in code (`path:line`, paths relative to `backend/`) |
| [L] | Limitation |

## 0. Live trace of one memory [V]

The trace ran `scratchpad/p14/trace.py` against the test PostgreSQL and
local Neo4j:
- real services and stores;
- a scripted `FakeLLMClient`;
- the deterministic fake embeddings.

The message was stated at **2026-01-10 09:00**. The worker processed it at
**2026-02-01 09:00**, simulating a quota deferral.

| Step | What the stores held |
|---|---|
| 1. Message | `messages` row, `extraction_status=queued`, `created_at=2026-01-10` |
| 2. Worker tick | 1 job → `done`, attempts 1; message → `done` |
| 3. Candidate | `extraction_candidates`: `decision=NEW`, `decision_reason="no currently active fact exists for this source and predicate"`, `confidence=0.900`, `payload.persistence="persistent"` |
| 4. Neo4j | `Memory{status: active, confidence: 0.9, valid_from: 2026-02-01, created_at: 2026-02-01}`; Entities `Drawing Extractor` (project) and `EasyOCR` (technology); `Source{message, reference=<message id>, location="4:14"}`; `RELATES_TO{predicate: uses, status: active, valid_from: 2026-02-01, memory_id=<memory>}` |
| 5. Embedding | One `memory_embeddings` row, `fake/fake-embedding-v1/768` |
| 6. Retrieval | `"Which OCR library does Drawing Extractor use?"` → found **only by the graph channel** (entity-name match, hop 0). The vector and keyword channels contributed nothing. `temporal_fit=0.0`, `final_score≈1.27` |
| 7. Agent | The scripted model called `search_memory`, then answered citing `[<memory>, "fabricated-id"]`. One citation-repair re-prompt; the bad id was stripped (`termination=citation_stripped`); 3 LLM calls |
| 8. Provenance | memory → Source → the exact `messages` row (`created_at` 2026-01-10) |
| 9. Burst | Two gated messages before any tick → **1 job**, retargeted to the second message. After the tick, the **first message is still `queued`** and its facts were never extracted |

**The trace itself shows two defects:**
- **Valid time is wrong.** `valid_from` is the processing time, 2026-02-01,
  not when the fact was stated, 2026-01-10 (step 4).
- **A burst silently drops the earlier messages** (step 9).

## 1. Memory lifecycle [I]

```text
POST /conversations/{id}/messages
  → ConversationService.append_message → maybe_trigger_extraction
      gate (heuristic) → SKIPPED(reason) | QUEUED + job (created, or an existing pending job retargeted)
  → background tick / sweeper: run_worker_tick → ExtractionRunner._run
      claim (lease) → window of ≤6 messages → LLM (extraction_v1, T=0, 1 repair)
      → for each candidate: persistence gate → grounding → entity ambiguity flags
        → per relationship: exact sibling classification → semantic evidence
        → overall_decision → _persist (MemoryService) → embed (new memories only)
        → extraction_candidates row (decision + decision_reason) → review row (some decisions)
      → job done, target message done
```

**Statuses:**
- Memory: active, historical, superseded, invalid, conflicting, archived,
  merged (`app/models/enums.py:12-36`).
- Believed: {active, conflicting}.
- Excluded from default retrieval: {invalid, archived, merged}.

**Nothing is deleted:**
- supersede and invalidate close the validity window and change the status;
- review actions append to `review_audit_log`.

## 2. Memory schema [I]

**Neo4j `Memory`** (`app/models/domain.py:68-83`):
- `id`, `user_id`, `content`;
- `memory_type` (episodic, semantic, procedural, working);
- `status`, `confidence` (0–1);
- `valid_from`, `valid_until`;
- `created_at`, `updated_at`;
- `invalidation_reason`.

**Links:**
- `MENTIONS` → Entity;
- `DERIVED_FROM` → Source;
- `SUPERSEDES`, `CONFLICTS_WITH`, `DUPLICATE_OF` → Memory.

**Content is immutable.** `PATCH` changes metadata only.

**PostgreSQL keeps the audit of how each memory came to exist:**

| Table | Key columns |
|---|---|
| `extraction_candidates` | `payload` (the full LLM candidate, including `reasoning` and temporal hints), `decision`, `decision_reason` (free text), `confidence`, `memory_id` |
| `memory_embeddings` | |
| `memory_review_candidates` | |
| `entity_merge_candidates` | |
| `review_audit_log` | |

## 3. Entity model [I]

**`Entity` properties:**
- `id`, `user_id`, `name`, `normalized_name`;
- `entity_type` (10 values);
- `aliases[]`, `created_at`, `updated_at`.

**Identity:**
- Identity is `(user_id, normalized_name)`, a uniqueness constraint.
- `normalize_name` only collapses whitespace, strips and lowercases
  (`app/models/normalization.py:31-41`).
- On a MERGE, the **first `entity_type` wins**, and aliases are unioned
  (`memory_repository.py:40-60`).

**Merges** are explicit review actions only:
- `MERGED_INTO` redirect, depth ≤ 5;
- aliases unioned;
- `RELATES_TO` copies tagged `merged_via`;
- reversible (`entity_repository.py:573-675`).

**Duplicate detection** (`app/extraction/resolution.py:23-51`):
- `canonical_key = re.sub(r"[^a-z0-9]+", "", name.lower())`;
- a pair is "plausible" when the canonical keys are equal but the
  normalized names differ.
- It also embeds names against 20 existing entities; cosine ≥ 0.85 → a
  merge *candidate*, never a merge (`runner.py:1064-1196`).

## 4. Relationship model [I]

- **Storage:** `RELATES_TO` between two entities, CREATEd once per
  assertion, never MERGEd.
- **Properties:**
  - `predicate`: free text matching `^[a-z][a-z0-9_]{0,62}[a-z0-9]$`;
  - `status`, `confidence` (default 1.0);
  - `valid_from`, `valid_until`, `memory_id` (the asserting memory, which
    is the provenance hook).
- **Vocabulary:** `KNOWN_PREDICATES` (including `related_to`) is reference
  only. Nothing guides or restricts predicates beyond the regex, and the
  extraction prompt does not mention predicates.
- **Cardinality:** none. Every predicate is treated as single-valued (§9).

## 5. Temporal model [I]

- **Two time axes:**
  - valid time `[valid_from, valid_until)`, half-open;
  - recorded time `created_at` / `updated_at` (ADR 0028).
- **Supersede** closes the old memory and its edges: `status=superseded`,
  `valid_until=coalesce(valid_until, now)` (`memory_repository.py:93-111`).
- **Write-side valid time:**
  - `valid_from = payload.valid_from or now`, where `now` is the
    **processing** time (`memory_service.py:86-88`).
  - `_persist` never sets `valid_from` (`runner.py:1198-1308`) [V, §0].
- **Temporal hints:**
  - The LLM emits `valid_from_hint`, `valid_until_hint`, `replaces_hint`
    and `is_change`.
  - **Only `is_change` and `replaces_hint` are used.** No date phrase is
    ever parsed.
- **Read-side intent** (`app/semantic/query_understanding.py:39-82`):

  | Intent | Matches |
  |---|---|
  | CURRENT | now, current, currently, today, at the moment |
  | HISTORICAL | before, previously, used to, earlier, in the past, historical(ly), formerly |
  | AS_OF | `in <month> [year]` |
  | UNSPECIFIED | otherwise |

  Not recognised: "no longer", "anymore", "as of <month>", "last year",
  "since". A month with no year resolves to the current year, even when that
  is in the future.

## 6. Provenance model [I][V]

- **Memory → Source:**
  - `source_type` is message, document, url, tool_result, user_statement,
    system_inference or conversation;
  - `reference` is a PostgreSQL id;
  - `location` is a span, or `chunk:<id>:<span>`.
- **Resolution:** `ProvenanceService` resolves to the message, document or
  chunk [V §0]. An unresolvable source returns `null` fields **without an
  explicit missing flag**; the UI infers it.
- **Duplicates** add another Source to the existing memory, which
  corroborates it.

## 7. Confidence model [I]

- **Origin:** only the LLM supplies confidence (candidate and
  relationship). It is never recalibrated.
- **Where it is stored:** memory `confidence`, `extraction_candidates.confidence`,
  and `RELATES_TO.confidence`.
- **Where it is used:**
  - ranking, with weight 0.3;
  - one decision: *UPDATE vs DUPLICATE*, which compares the candidate's
    confidence with the relationship's (`decisions.py:120-137`).
- **The UI** shows "Extraction confidence" with an explainer.

## 8. Extraction process [I]

**Gate** (`app/extraction/gate.py:63-86`). Rules, in order:

| # | Rule | Result |
|---|---|---|
| 1 | Not a user message | Skipped |
| 2 | Empty | Skipped |
| 3 | < 25 characters | Skipped |
| 4 | Bare question | Skipped |
| 5 | One of 32 filler phrases | Skipped |
| 6 | Otherwise | Pass |

The reason is stored in `messages.extraction_gate_reason`.

**Skipped messages** are re-offered to the sweeper, but through the *same*
gate, so the result never changes.

**Trigger:**
- The window is the last 6 messages.
- A pending, unclaimed job for the conversation is **retargeted** to the new
  message, the debounce (`trigger.py:51-60`). The prompt then extracts only
  from the TARGET, so the earlier message is never extracted [V §0 step 9].

**Prompt** (`extraction_v1.py`):
- Extract from the TARGET only; spans must be exact.
- Changes are expressed as `is_change` / `replaces_hint`.
- Persistence: "persistent" (durable fact) vs "temporary" (mood, one-off,
  conversation-only). "When uncertain prefer temporary."
- Content is data.

**Schema:** strict `extra="forbid"`; 20 entities and 20 relationships per
candidate at most.

## 9. Decision process [I]

In `_apply_candidate` (`runner.py:742-869`):

1. **Worthiness:**
   - `persistence != "persistent"` → **IGNORE**.
   - This is the only worthiness check, and it is decided by the LLM's own
     label.
2. **Grounding** (`grounding.py:51-98`):
   - the span must be in bounds;
   - entity names must be substrings of the mention span or the window;
   - failure → INVALID.
3. **Ambiguity flags** → `entity_merge_candidates`.
4. **Per relationship** (`decisions.py:83-183`), against the source entity's
   outgoing edges with the **same predicate** that are currently believed:
   - **Same target:**
     - `is_change` → UPDATE;
     - otherwise, if the candidate's confidence is higher → UPDATE;
     - otherwise → DUPLICATE.
   - **Different target, with `is_change`:**
     - if `replaces_hint` matches a target → SUPERSEDING that edge;
     - else if exactly **one** edge exists → SUPERSEDING it, **even if
       `replaces_hint` names something else**;
     - else → CONFLICTING.
   - **Different target, no `is_change`** → **CONFLICTING**. No predicate is
     multi-valued, so "X uses A" followed by "X uses B" is a conflict.
   - **No sibling edges** → NEW, then semantic evidence (pgvector top 5):
     - ≥ 0.90 → LIKELY_DUPLICATE;
     - ≥ 0.80 → FLAGGED.
5. **Overall decision** priority: SUPERSEDING > CONFLICTING > UPDATE >
   DUPLICATE > LIKELY_DUPLICATE > FLAGGED > NEW.
6. **Persistence:**

   | Decision | What is written |
   |---|---|
   | NEW, SUPERSEDING, LIKELY_DUPLICATE, FLAGGED | A new active memory; SUPERSEDING also closes the old one |
   | CONFLICTING | A new memory with status `conflicting`; `CONFLICTS_WITH` only when there is a single target |
   | UPDATE | The existing memory's confidence changes, and a source is attached |
   | DUPLICATE | A source is attached to the existing memory |
   | IGNORE, INVALID | Nothing |

   Every decision gets an `extraction_candidates` row with `decision_reason`.

## 10. Retrieval process [I][V]

`HybridRetrievalService.search` (`hybrid_retrieval_service.py:218-475`).
The channels run sequentially:

| Channel | How it finds candidates | Score |
|---|---|---|
| Vector | pgvector top 30, cosine ≥ 0.3 | Similarity |
| Keyword | `toLower(content) CONTAINS toLower(<whole query>)` | Binary; effectively never matches a question [V §0] |
| Graph | An entity name is a *substring* of the query (first 500 entities), resolved through redirects, then a neighbourhood walk of ≤ 2 hops | 0.5^hops |

**After the channels:**
- **Merge:** the union of candidates, each re-read from Neo4j; excluded
  statuses are dropped.
- **Temporal filter** (unless the intent is UNSPECIFIED):
  - CURRENT keeps only believed memories;
  - HISTORICAL keeps only memories that are not believed;
  - AS_OF checks the validity window.
- **Score:** `1.0·vector + 1.0·graph + 0.5·keyword + 0.2·recency(exp(−age/30 d)) + 0.5·temporal_fit + 0.3·confidence`.
- **Ties** follow Python `set` order, so they are not reproducible across
  processes.
- **Tuning:** no signal has ever been ablated beyond the three channel
  configurations in `eval/retrieval_runner.py`.

## 11. Agent interaction [I][V]

**Loop** (`agent_service.py:207-311`):
- The model returns structured `AgentStep` values.
- At most 6 tool calls; tool timeout 15 s; turn timeout 60 s.
- An exact repeated call → `loop_detected`.
- **When budget runs out, the model is not allowed a final answer** (a
  canned string is returned).

**Tools:**
- `search_memory` (with temporal scope)
- `get_entity`
- `traverse_graph`
- `get_entity_state`
- `get_memory_history`
- `get_source`
- `propose_memory_update` (off by default)

**The prompt decides "do I need memory?".** There is no code-side check.

**Citations:**
- Every cited id must have been returned by a tool during this turn.
- One repair, then strip [V §0].
- Being in that set proves the memory was *seen*, not that it *supports*
  the claim.

**Output:** `answer`, `cited_memory_ids`,
`confidence ∈ {high, medium, low, not_found}`. There is no
retrieved-vs-inferred-vs-general-knowledge field.

## 12. Known weaknesses (inputs to Phase 14)

| # | Weakness | Evidence |
|---|---|---|
| W1 | A burst of messages drops facts: the debounce retargets the job | [V] §0 step 9 |
| W2 | `valid_from` is the processing time, not when the fact was stated. A supersede closes the old fact at processing time | [V] §0 step 4 |
| W3 | Temporal hints ("since", "until May", "last year") are never parsed | [I] |
| W4 | "X uses A" then "X uses B" is CONFLICTING; no predicate is multi-valued | [I] |
| W5 | SUPERSEDING when `replaces_hint` names an entity that is not the existing target | [I] |
| W6 | Worthiness is the LLM's `persistence` label only. There is no deterministic check and no way to state *why* in structured form. An explicit "remember that …" marked "temporary" is dropped | [I] |
| W7 | Short declaratives (< 25 characters, e.g. "I use Postgres now.") are skipped forever; the re-check is a no-op | [I] |
| W8 | `canonical_key` strips every non-ASCII letter, so all non-Latin names compare equal, which gives false merge candidates | [I] |
| W9 | After a merge, new writes still match the merged-away entity's normalized name | [I] |
| W10 | Identical candidates in one LLM response are applied twice | [I] |
| W11 | The keyword channel is whole-query CONTAINS, so it is inert for questions | [V] §0 step 6 |
| W12 | Graph mention detection is a raw substring match ("ai" inside "said") | [I] |
| W13 | Retrieval ties are not reproducible | [I] |
| W14 | Query intent misses "no longer", "anymore", "as of <month>", "last year"; a month with no year can resolve to the future | [I] |
| W15 | Provenance has no explicit "missing source" flag | [I] |
| W16 | There is no API or UI answering "why is this remembered?" (decision, reason, source, history); `decision_reason` is never shown | [I] |
| W17 | The agent evaluation's scripted baseline filters citations by the gold set, so citation precision is 1.0 by construction | [I] `eval/agent_runner.py:185` |
| W18 | `PATCH` and invalidate have no version check and no audit row | [I] |
| W19 | Retrieval necessity and grounding type exist only in the prompt; nothing checks them without a real LLM | [I][L] |
| W20 | Every same-name mention is one entity, whatever its type ("Apple" the company and the fruit) | [I][L] |
