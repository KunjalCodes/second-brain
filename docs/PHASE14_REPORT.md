# Phase 14 — Memory Intelligence: Report (2026-09-29)

**Scope.** This phase made the memory itself choose and date facts
correctly, classify change correctly, and retrieve the right memories, and it
lets a person see *why* something is remembered.
- No deployment and no infrastructure work.
- No coding-agent or MCP work (that is Phase 16).
- **Baseline:** `b195fff`. The audit of the pipeline as it was is
  `docs/PHASE14_MEMORY_MODEL.md`.

**Labels:**

| Label | Meaning |
|---|---|
| [V] | Verified by a test or check that ran in this phase |
| [M] | Measured in this phase |
| [I] | Inspected in code |
| [A] | Assumption |
| [L] | Limitation |

**Conditions and non-claims.**
- Everything is local: Apple M1, PostgreSQL 17, Neo4j Community.
- The LLM is scripted (`FakeLLMClient`) and embeddings are the
  deterministic fake.
- **No real-LLM result exists for this phase** (§15).
- The machine was shared, with load average 7–15. Latency was therefore
  measured by interleaving requests (§16).

---

## 1. Existing memory architecture

At `b195fff` the architecture was, per `docs/PHASE14_MEMORY_MODEL.md` §1–11 [I]:
- **Neo4j** holds the canonical temporal graph: Memory, Entity, Source,
  `RELATES_TO` with a free-text predicate, plus supersede, conflict,
  duplicate and merge links.
- **PostgreSQL** holds the audit, jobs, embeddings and the review queue.
- **Extraction:** a heuristic gate, then one LLM call per job with strict
  schemas, then a deterministic decision engine.
- **Retrieval:** hybrid (vector, keyword, graph) with linear ranking.
- **Agent:** a structured-output agent with citation validation.

## 2. Current lifecycle

The lifecycle is unchanged in shape; the new steps are marked **(Phase 14)**:

```text
message → gate (+ short durable declaratives)
        → one job per gated message
        → LLM candidates
        → worthiness policy (Phase 14)
        → grounding
        → entity ambiguity flags (Unicode-safe keys)
        → temporal interpretation of hints, relative to when it was said (Phase 14)
        → per-relationship decision (+ cardinality, past states, hint mismatch)
        → exact-duplicate check for bare statements (Phase 14)
        → semantic evidence
        → persist (merged-entity redirect; stated-time validity)
        → embed → audit row with [worthiness:…] [temporal: …] → review
```

## 3. Problems discovered

The audit found 20 weaknesses (`PHASE14_MEMORY_MODEL.md` §12). A live trace
of one real memory through the stores (§0 there) **observed two data defects**
[V]:
- **Burst loss (W1):** two messages sent before a worker tick produced one
  job. The first message stayed `queued` forever and was never extracted.
- **Wrong valid time (W2):** a fact stated on 2026-01-10 and processed on
  2026-02-01 was valid from 2026-02-01.

A new deterministic harness, `eval/memory_quality_runner.py` (49 tasks plus
12 retrieval queries), was run against `b195fff` **before any fix**: **21/49
passed** [M].

## 4. Changes made

| # | Change | Where | Weakness |
|---|---|---|---|
| 1 | Deterministic memory-worthiness policy, rule code stored in `decision_reason` | `app/extraction/worthiness.py`, runner | W6 |
| 2 | Gate passes short first-person durable declaratives | `app/extraction/gate.py` | W7 |
| 3 | One job per gated message; debounce retargeting removed | `app/extraction/trigger.py` | W1 |
| 4 | Valid time = when stated; supersede closes at the new fact's valid time | runner, `memory_service.py`, `memory_repository.py` | W2 |
| 5 | Deterministic temporal-hint parser, including open start for past facts | `app/extraction/temporal_hints.py` | W3 |
| 6 | Multi-valued predicates; past states don't conflict; `replaces_hint` mismatch no longer supersedes the wrong fact | `app/extraction/decisions.py` | W4, W5 |
| 7 | Exact bare-statement duplicate → DUPLICATE (corroboration) | runner | G3 |
| 8 | Unicode-aware duplicate keys, with a minimum key length | `app/extraction/resolution.py` | W8 |
| 9 | Writes naming a merged-away entity go to the canonical entity (alias kept) | `memory_service.py`, `RESOLVE_MERGED_NAMES` | W9 |
| 10 | Word-boundary entity mentions; stemmed term keyword channel (≥ 50% coverage); deterministic tie order | `hybrid_retrieval_service.py`, `SEARCH_BY_TERMS` | W11–W13 |
| 11 | Query intent: "no longer", "anymore/still", "as of <month>", "did … use to", "last year"; month without a year never resolves to the future | `query_understanding.py` | W14 |
| 12 | `SourceProvenance.resolved` | `provenance_service.py` | W15 |
| 13 | `GET /memories/{id}/explanation` plus the "How this memory was decided" UI section | `memory_explanation_service.py`, `WhyRemembered.tsx` | W16 |
| 14 | Agent-evaluation citation policy is gold-blind | `eval/agent_runner.py` | W17 |
| 15 | `eval/runner.py` cleanup foreign-key fix (the harness runs again) | `eval/runner.py` | Phase 11 known issue |

**Intentional behaviour changes** (each justified and recorded in ADR
amendments 0007, 0008 and 0028):

- **Multi-valued predicates.** A second `uses` value is now NEW. Before, every
  technology after a project's first was stored as `conflicting` and sent to
  review.
  - Four existing tests and two dataset tasks used "uses EasyOCR / uses
    Tesseract" as *the* conflict example:
    - `extraction_v1` `scenario_8`;
    - `document_v1` `doc_conflicting_documents`;
    - integration fixtures in `test_extraction_runner.py`,
      `test_review_actions.py` and `test_agent_write.py`.
  - These now use a single-valued predicate (`ocr_engine`), so they still test
    the conflict mechanism. The descriptions say why.
- **Mismatched change hint.** A change naming an unrecorded value no longer
  supersedes the one recorded fact. The unit test asserting the old fallback
  was replaced by two tests of the new rule.
- **Supersede close time.** A supersede made through `POST /memories` with an
  explicit `valid_from` now closes the old fact at that time rather than at
  the moment of the write.

## 5. Memory-worthiness behaviour [V]

Rules run in order, on the **user's own text**:

| # | Rule | Result |
|---|---|---|
| 1 | Hypothetical | IGNORE |
| 2 | Arithmetic | IGNORE |
| 3 | Activity in progress | IGNORE, unless recurring ("every Friday") |
| 4 | Explicit instruction ("remember that", "from now on", "I prefer", "don't forget") | STORE, even if the model said temporary or uncertain |
| 5 | Otherwise | The model's label, as before |

- **"Why was this stored / not stored?"** is answered by the
  `[worthiness:<code>]` prefix on every audit row, and in the explanation
  API and UI.
- **Evidence:** A_worthiness 4/10 → **10/10**; `test_worthiness.py` (18
  cases, including the "currently runs on" state, the recurring activity, and
  "always" in a mood statement).
- **[L]** The rules are English surface patterns. An unmatched message falls
  through to the model's label, never to a new guess.

## 6. Extraction behaviour [V]

- **Burst of messages:** every message is extracted (B3). The live trace
  reproduced the loss before the fix; `test_a_burst_of_messages_extracts_every_message`
  covers it now.
- **Cost:** one LLM call per gated message, the same as the non-burst path
  always paid [I].
- **Other cases:** multiple facts in one message, identical candidates in one
  response, a correction, and an ungrounded entity (INVALID) all behave as
  expected (B 4/5 → 5/5).
- **Prompts:** unchanged. No prompt change was made, so ADR 0016's
  real-LLM-evidence requirement does not apply.

## 7. Entity resolution [V]

- **Duplicate keys:**
  - Unicode-aware, with diacritics folded ("Zoë" ~ "Zoe").
  - Distinct non-Latin names no longer "match" each other. The old ASCII key
    reduced "東京" and "大阪" both to "".
  - A key shorter than 3 characters is never evidence ("C++" vs "C#").
- **Merges:** a write naming a merged-away entity lands on the canonical
  entity, with the written name kept as an alias.
- **Unchanged:** merges are still review-only and reversible; semantic
  similarity still only *flags*.
- **Evidence:** C 5/9 → **9/9**; `test_extraction_resolution.py`;
  `test_a_write_naming_a_merged_away_entity_lands_on_the_canonical_one`.
- **[L]** Identity is still `(user, normalized name)`, so "Apple" the company
  and "Apple" the fruit are one entity (W20).
- **[L]** Abbreviations such as "Postgres" / "PostgreSQL" are not detected by
  keys. That is deliberate, since false merges are worse.

## 8. Relationship quality [V][L]

- **Every relationship keeps:** source, target, predicate, `memory_id` (its
  provenance), confidence, status and validity [I].
- **Cardinality:** 31 predicates are declared multi-valued. Every other
  predicate keeps the conservative single-valued behaviour.
- **[L] Not changed: generic `related_to`.** Nothing replaces a vague
  predicate with a better one, because doing that without evidence would
  invent a relationship. Predicate synonyms ("uses" / "utilizes") are still
  distinct strings.
- **[L] First-person subjects cannot be grounded.** A "User" entity for "I
  use Java" fails grounding, because "user" is not in the text. The dataset
  task was rewritten, and a self-entity concept is an open question (§18).

## 9. Temporal behaviour [V]

- **Valid time:** from the message's (or chunk's) `created_at`, adjusted by
  hints. A supersede closes the old fact at the new fact's valid time. The
  window stays half-open `[from, until)` and is never inverted.
- **Hints parsed:**
  - ISO dates; "March 2025"; a bare month (the most recent); a year;
  - "last/this year|month", "last week", "N units ago", "today",
    "yesterday".
- **Unparsed hints** fall back to the stated time and say so.
- **Query intent:**
  - CURRENT: now, current, currently, today, at the moment, anymore, still.
  - HISTORICAL: before, previously, used to, earlier, formerly, no longer,
    last year, did … use to.
  - AS_OF: "in / as of <month> [year]".
  - UNSPECIFIED: otherwise.
- **Evidence:** E 3/14 → **14/14**; `test_temporal_hints.py` (27);
  `test_valid_time_is_when_it_was_said_not_when_it_was_processed` (stated
  January 10, processed February 1: valid from January 10; the old fact
  closed at May 5, not June 1).
- **[L]** A past fact with only an end is stored with no start. An `as_of`
  query before that end therefore returns it.

## 10. Conflict handling [V]

| Situation | Outcome |
|---|---|
| Genuine contradiction (single-valued, no change signal) | CONFLICTING, linked, review row; nothing overwritten |
| Temporal replacement ("switched from Java to Python") | SUPERSEDING, history preserved |
| Concurrent states (multi-valued) | NEW; both active |
| Change from an unrecorded value | NEW (multi-valued) or CONFLICTING (single-valued) |
| A past state | NEW (it cannot contradict the present) |

- **Evidence:** F 3/6 → **6/6**; `TestPhase14Cardinality`.
- **[L] Not modelled:** "different contexts" (e.g. uses X for tables, Y for
  text) and "insufficient evidence" as separate outcomes.

## 11. Consolidation [V]

- **Identical restatement:** DUPLICATE, which adds a second source to the
  same memory (G1).
- **Higher-confidence restatement:** UPDATE (G2).
- **Identical bare statement:** DUPLICATE. Before, it created a second active
  memory flagged LIKELY_DUPLICATE (G3; G 2/3 → **3/3**).
- **Near-identical wording** still goes to review, never merged
  (`test_bare_statement_duplicate_is_caught_by_semantic_similarity`).

## 12. Retrieval improvements [M]

The new controlled suite covers:
- exact, paraphrased, related, multi-hop, historical ×4, conflicting,
  irrelevant (substring), missing and multiple.

The scores below are k=5, deterministic across runs [V]:

| Config | Recall@5 before → after | MRR before → after | Forbidden-hit rate before → after |
|---|---|---|---|
| hybrid | 0.90 → **1.00** | 0.633 → **0.817** | 0.25 → **0.00** |
| vector_only | 0.30 → 0.30 | 0.30 → 0.30 | 0 → 0 |
| graph_only | 0.90 → 0.90 | 0.503 → 0.587 | 0.25 → 0.00 |
| keyword_only | 0.00 → 0.90 | 0.00 → 0.783 | 0 → 0 |
| hybrid without recency | 0.90 → 1.00 | 0.717 → 0.817 | 0.25 → 0.00 |
| hybrid without confidence | 0.90 → 1.00 | 0.633 → 0.817 | 0.25 → 0.00 |

**What moved the numbers:**
- **H2 (paraphrase)** was fixed by the term channel.
- **H7 ("no longer") and H8 ("as of March 2026")** were fixed by intent
  detection. Before, the superseded fact's replacement was returned too.
- **H10** was fixed by word-boundary mentions ("ai" inside "said").

**Signal effects:**
- **Recency and confidence** barely change ranking here.
- **The vector channel** is weak because the fake embeddings are a bag of
  words.

**First version of the term channel.** Without a coverage floor it cut the
Phase 3 suite's hybrid precision from 100% to 91.7%: one shared word pulled
in a different "Cobalt" entity's memory. The **≥ 50% term coverage** rule
removed that. `retrieval_v1` output is now **byte-identical** to `b195fff`
[V].

**[L]** Ranking weights were not tuned. The suite is small and synthetic, and
tuning on it would overfit.

## 13. Agent behaviour [V][L]

- **Agent loop** (tools, prompt, citation validation): unchanged in this
  phase.
- **Deterministic evaluation:**
  - A–D retrieval baselines are identical before and after.
  - The scripted E baseline now cites the top 3 retrieved memories instead of
    filtering by the gold set. Citation precision is **93.3%**, down from a
    by-construction 100%.
  - The miss is `retrieval_necessity_absent`: search returns something for a
    fact that is not in memory, and a naive agent would cite it.
- **[L] Tool selection, retrieval necessity and grounding are decided by the
  model.** Nothing measures them without a real LLM (§15).
- **[L] No grounding field.** The answer schema has no field for "retrieved
  fact vs inference vs general knowledge". Adding one changes the LLM-facing
  schema and prompt, which needs real-LLM evidence (ADR 0016).

## 14. Provenance [V]

- **Chain:** memory → source → the exact originating message (J1).
- **Lost source:** after the message is deleted, the source is reported with
  `resolved: false` (J2) instead of silently empty fields. J 0/2 → **2/2**.
- **UI:** the frontend uses the flag and falls back to inference for older
  responses.
- **Explanation API:** it includes the resolved sources and never fabricates
  one. A directly created memory says `origin: "direct"` with no decisions
  (API test and E2E).

## 15. Evaluation results

| Harness | Before (`b195fff`) | After |
|---|---|---|
| **memory_quality_v1** (new, 49 tasks) | **21/49** | **49/49** (double run identical) [M] |
| A worthiness | 4/10 | 10/10 |
| B extraction | 4/5 | 5/5 |
| C entity resolution | 5/9 | 9/9 |
| E temporal | 3/14 | 14/14 |
| F conflict | 3/6 | 6/6 |
| G consolidation | 2/3 | 3/3 |
| J provenance | 0/2 | 2/2 |
| Retrieval suite (hybrid, k=5) | R 0.90 / MRR 0.633 / forbidden 0.25 | R 1.00 / MRR 0.817 / forbidden 0.00 |
| extraction_v1 (`eval/runner.py`) | 10/10 | 10/10 (`scenario_8` now `ocr_engine`) |
| retrieval_v1 (Phase 3) | 91.7 / 68.8 / 100 | **byte-identical** |
| graph_api_v1 conformance | 39/39 | 39/39 |
| document_v1 | 10/10 | 10/10 (one task moved to `ocr_engine`) |
| agent_v1 A–D | unchanged | unchanged |
| agent_v1 E (scripted) | citation precision 100% (circular) | 93.3% (gold-blind) |
| **Real-LLM evaluation (M16)** | **Not run** | **Not run** |

**Why the real-LLM evaluation was not run.** It is explicit opt-in, and the
current Gemini key allows about 20 requests per day. No real-LLM numbers
exist for this phase.

**Honest caveats [L]:**
- **Overfitting risk:** `memory_quality_v1` was written alongside the fixes,
  and most tasks target a defect the audit found. The independent,
  pre-existing harnesses (the rows above it) are the regression evidence.
- **Scripted extraction:** the harness scripts the extraction, so it measures
  the application's decisions, not a model's extraction quality.
- **Categories not covered deterministically:** K (tool selection) and I
  (retrieval necessity) cannot be measured without a real model.

## 16. Performance [M]

**Method.** Requests were **interleaved** one at a time between a
`b195fff` server and the new server on the same database: 150 per scenario,
concurrency 1. Load drift therefore hits both sides equally. Concurrent
load tests on this machine were dominated by an unrelated process (another
project's `tsx watch` at about 97% CPU) and are not used.

| Operation | p50 before → after | Change | Cause |
|---|---|---|---|
| `GET /memories/search` | 28.9 → 27.3 ms | −1.5 (noise) | Unchanged code |
| `GET /graph/.../neighborhood` | 22.4 → 21.6 ms | −0.8 (noise) | Unchanged code |
| Agent turn (offline LLM) | 26.7 → 26.7 ms | 0.0 | Unchanged path |
| `POST /search` (hybrid) | 50.1 → 55.4 ms | **+5.3 ms (~11%)** | One extra bounded Neo4j read (term match) |
| `POST /memories` | 28.5 → 30.9 ms | **+2.4 ms (~8%)** | One extra bounded Neo4j read (merged-name redirect) |

**Extraction worker** (`eval/job_benchmark.py`, 3 interleaved runs each):
- 884 → 842 jobs/min (median);
- per-tick p50 about 65 → 73 ms;
- the run ranges overlap: consistent with a small cost of about 5% from the
  redirect read and the exact-duplicate check, but within noise.

Ingestion throughput and claim latency show no difference, and there are 0
duplicate claims.

**Per-operation effects [I]:**

| Operation | Effect |
|---|---|
| Memory creation | +1 Neo4j read |
| Hybrid search | +1 Neo4j read |
| Bare-statement extraction | +1 Neo4j read |
| Embedding calls | No new calls |
| Prompts and token use | Unchanged |
| LLM calls | +1 per extra message in a burst (previously never extracted); +1 for each short declarative the gate now admits |

**Tradeoff:** a few milliseconds per search and write, for correct valid time,
no lost messages, correct merges and the retrieval gains in §12.

## 17. Limitations

**Policy coverage:**
- The worthiness and gate rules are English-only surface patterns.
- Temporal parsing covers only the forms in §9.
- Multi-valued predicates are a fixed list. Unknown predicates default to the
  conservative single-valued rule.

**Model and data boundaries:**
- Entity identity is still name-based (W20), and first-person facts cannot
  carry a self-entity.
- There is no grounding-type field, and retrieval necessity is model-only
  (W19).
- The absent-memory query still returns candidates (§13).
- There are no real-LLM results for this phase.

**Unchanged surfaces:**
- `PATCH` and invalidate still have no version check or audit row (W18). The
  review actions do have both.
- The keyword term channel is a per-user content scan (bounded `LIMIT`, no
  full-text index). This is fine at current scale [A].

**Environment:**
- All measurements are local, on a busy shared machine, and labelled so.

## 18. Remaining architectural questions

1. **A self entity.** Should each user have a canonical "self" entity, so
   first-person facts ("I use Java") can carry relationships and be
   grounded?
2. **Typed entity identity.** Should identity include the type for known
   collisions (Apple/Apple), and at what false-split risk?
3. **Predicate vocabulary.** Should predicates be normalised to a
   vocabulary (with synonyms), so duplicate and conflict detection sees
   "uses" = "utilizes"? This needs a real-LLM evaluation of extraction
   behaviour.
4. **Grounding type in answers.** A grounding type (fact / inference /
   general) and per-claim citations in the agent answer: this is a
   prompt/schema change that requires ADR 0016 evidence.
5. **Search index.** A full-text index for the keyword channel (Neo4j
   full-text or PostgreSQL `tsvector`) once memory counts grow.
6. **Audit for manual edits.** Should `PATCH` and invalidate go through the
   same audited, version-checked path as review actions?
