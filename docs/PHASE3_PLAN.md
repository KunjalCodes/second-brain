# Phase 3 Plan — Semantic Memory + Hybrid Graph/Vector Retrieval

**Status:** Proposed. Awaiting approval. No application code has been written for
this phase.

This document is the design only, per the approved process: architecture, data
model, algorithms, consistency model, evaluation design, and implementation order —
implementation starts only after explicit sign-off.

---

## 0. What Phase 3 extends, not replaces

Phase 3 is one more layer on the same pipeline, not a parallel system. Tracing the
two paths this plan extends:

```text
message → PostgreSQL → heuristic gate → extraction window → extraction job
    → worker → LLM → grounding → candidate → decision engine → Neo4j → provenance
```

```text
query → RetrievalService → EntityRepository (Cypher) → graph result
```

Phase 3 adds one new step to the first path (embed a memory right after it is
persisted) and one new parallel branch to the second (a vector path, merged with
the existing graph path before ranking). Everything already built — the decision
engine, the conservative policy, `extraction_candidates`, the exact-triple
detector, `EntityMergeCandidate`, the bi-temporal model, the `LLMClient` provider
abstraction — is kept exactly as is. Nothing here duplicates or reroutes around
the Phase 1/2 boundary between "the LLM proposes, the application decides, Neo4j
persists" (ADR 0007): semantic similarity is a new *signal* feeding that same
decision engine, never a new decision-maker.

**Kept unchanged, explicitly:** PostgreSQL for application state / Neo4j as sole
canonical memory (ADR 0006), `extraction_candidates` (ADR — Phase 2 report §3),
`users` table scope, Railway as deployment target, `FakeLLMClient`, the Gemini
`LLMClient` abstraction, the conservative memory policy (ADR 0007), the exact-triple
duplicate limitation as the *default* behavior (Phase 3 adds a second, softer
signal alongside it — it does not remove or replace the exact check).

---

## 1. Architecture

```text
                         PostgreSQL                         Neo4j
                    ┌─────────────────┐              ┌─────────────────┐
                    │ conversations    │              │ Memory (canon.) │
                    │ messages         │              │ Entity          │
                    │ extraction_jobs  │   memory_id  │ Source          │
                    │ extraction_      │◄────────────►│ RELATES_TO      │
                    │   candidates     │  (opaque id, │ SUPERSEDES      │
                    │ entity_merge_    │  no FK)      │ CONFLICTS_WITH  │
                    │   candidates     │              │                 │
                    │ memory_embeddings│              │                 │  <- NEW
                    │   (NEW, pgvector)│              │                 │
                    └────────┬─────────┘              └────────┬────────┘
                             │                                  │
                             │        EmbeddingClient           │
                             │     (Gemini | Fake)   NEW        │
                             ▼                                  │
                    ┌──────────────────────────────────────────▼───────┐
                    │              HybridRetrievalService    NEW        │
                    │  VectorRetrieval (pgvector) + GraphRetrieval       │
                    │  (existing RetrievalService/MemoryService) → merge │
                    │  → temporal filter (Neo4j-authoritative) → rank    │
                    └────────────────────────────────────────────────────┘
```

New components, each behind the same kind of boundary Phase 1/2 already
established:

- `app/embeddings/base.py` — `EmbeddingClient` Protocol + `EmbeddingResult`,
  structurally identical in shape to `app/llm/base.py`'s `LLMClient`/`LLMResult`.
- `app/embeddings/fake.py` — `FakeEmbeddingClient`, deterministic, no network.
- `app/embeddings/providers/gemini.py` — the only file importing the embedding
  SDK, mirroring `app/llm/providers/gemini.py`'s isolation.
- `app/embeddings/factory.py` — `build_embedding_client()`/`get_embedding_client()`,
  mirroring `app/llm/factory.py`.
- `app/db/models.py` — new `MemoryEmbedding` table.
- `app/repositories/embedding_repository.py` — `UserScopedRepository` subclass;
  the only file that writes/queries `memory_embeddings`.
- `app/semantic/similarity.py` — pure functions: cosine-similarity-to-evidence
  mapping, semantic duplicate/conflict classification. Mirrors
  `app/extraction/decisions.py`'s "pure, no I/O, exhaustively testable" shape.
- `app/semantic/embedding_pipeline.py` — the post-persist embedding step
  (mirrors `app/extraction/runner.py`'s job-processing shape) plus the backfill
  driver.
- `app/services/hybrid_retrieval_service.py` — merges `RetrievalService`'s graph
  results with the new vector path; owns ranking.
- `app/api/routes/search.py` — one new endpoint, `POST /search`.

No microservice, no separate vector database, no Redis, no Celery. PostgreSQL
gains one extension (`pgvector`) and one table.

---

## 2. Data model

### 2.1 New PostgreSQL table

```python
class MemoryEmbedding(Base):
    """A vector representation of one canonical Neo4j Memory's content.

    NOT canonical memory -- an opaque `memory_id` pointer into Neo4j, exactly the
    pattern `ExtractionCandidate.memory_id` and `EntityMergeCandidate.entity_id_*`
    already use (docs/adr/0006, docs/adr/0009). PostgreSQL never stores a memory's
    content, status, or validity window here -- only its vector and the metadata
    needed to know which model produced it.
    """
    __tablename__ = "memory_embeddings"

    id: Mapped[str]                    # uuid, primary key
    user_id: Mapped[str]                # FK users.id -- isolation key
    memory_id: Mapped[str]              # opaque Neo4j Memory.id, NOT a FK
    content_hash: Mapped[str]           # sha256 of the exact text embedded
    embedding: Mapped[Vector]           # pgvector column, dimension from settings
    embedding_provider: Mapped[str]     # "fake" | "gemini" | ...
    embedding_model: Mapped[str]        # e.g. "gemini-embedding-001"
    embedding_dimension: Mapped[int]    # redundant with the column's declared
                                         # dimension, stored for defense-in-depth
                                         # validation (section 12) and so a
                                         # dimension mismatch is a clear app-level
                                         # error, not an opaque pgvector one
    created_at: Mapped[datetime]

    __table_args__ = (
        UniqueConstraint(
            "memory_id", "embedding_provider", "embedding_model",
            name="memory_embeddings_one_per_model",
        ),
        Index("memory_embeddings_user", "user_id"),
        # The vector similarity index itself -- see section 15.
    )
```

**Why a `(memory_id, provider, model)` unique key, not `memory_id` alone:** a
memory gets a *new* row when the configured embedding model changes (section 12),
rather than overwriting the old vector in place. This is what makes re-embedding
resumable, comparable side-by-side during a migration, and rollback-safe — the
same "never silently discard" instinct behind keeping superseded memories in
Neo4j, applied to embeddings.

**Why `content_hash`:** Memory `content` is immutable once created (confirmed:
`MemoryUpdate` deliberately excludes `content` — a changed fact is always a new
memory, never a rewrite). So a memory needs embedding *exactly once* per model —
`content_hash` is not for detecting drift (content cannot drift) but is a cheap
guard the backfill/pipeline can assert against: if a `memory_id`'s stored content
hash ever doesn't match what a re-embed attempt is about to embed, that indicates
a Neo4j id was somehow reused or corrupted upstream, and the pipeline should flag
it rather than silently overwrite.

### 2.2 Neo4j — no schema change

Neo4j gains **no new node type, no new property**. The Memory node remains exactly
what Phase 1/2 built. This is the whole point of the boundary: PostgreSQL holds a
*derived, disposable* representation (an embedding can always be regenerated from
the canonical content); Neo4j holds the one irreplaceable copy.

### 2.3 `EntityMergeCandidate` — reused, not replaced

Phase 2 already created this table with a nullable `score: float` column,
specifically anticipating this: "Potentially store uncertain entity merge
candidates for later review" was already the design. Phase 3's entity-resolution
enhancement (section 7) writes into this *same* table with `score` populated from
embedding cosine similarity, `evidence` extended with `{"signal": "embedding",
"similarity": 0.94, ...}` alongside the existing name-based evidence. No new table
needed here.

---

## 3. Embedding lifecycle

### New memory

```text
Memory created in Neo4j (existing MemoryService.create, unchanged)
        ↓  (response already returned to the caller)
BackgroundTask: embed_memory(memory_id, content)
        ↓
EmbeddingClient.embed(content) -> vector
        ↓
INSERT memory_embeddings (idempotent: ON CONFLICT (memory_id, provider, model) DO NOTHING)
```

Mirrors the extraction trigger's own principle exactly (ADR 0008): embedding is an
asynchronous enhancement scheduled *after* the response commits, never a
dependency of memory creation. If `EMBEDDING_PROVIDER` is unset or embedding fails,
memory creation is completely unaffected — this is enforced structurally by
running it as a `BackgroundTask` off `MemoryService.create`'s existing call sites
(the API route and `ExtractionRunner._persist`), not inside `MemoryService.create`
itself.

### Memory superseded

The embedding of the **superseded** memory is left exactly as it is — not deleted,
not marked inactive, not re-embedded. Historical vector search ("what did I use in
March") must still be able to find it. What changes is not the embedding but which
memories are *eligible* to be returned for a given query's temporal intent, and
that filtering happens against Neo4j's authoritative status/validity window at
retrieval time (section 6), never by mutating the embedding row.

### Memory invalidated

Same principle: the embedding row is untouched. `HybridRetrievalService` excludes
invalidated memories from default retrieval the same way `MemoryService.search`
already does — by checking the authoritative Neo4j status, not a PostgreSQL flag.
There is deliberately no `is_active`/`is_valid` column on `memory_embeddings`: that
would be a second, cacheable copy of state Neo4j already owns, exactly the
duplication docs/adr/0006 forbids. A stale cached flag is a worse failure mode
than one extra Neo4j lookup per candidate.

### Content changes

Do not occur — `content` is immutable (confirmed in `MemoryUpdate`, section 2.1).
A memory is embedded exactly once, ever, per embedding model.

---

## 4. Consistency model

PostgreSQL and Neo4j cannot share one ACID transaction (unchanged conclusion from
ADR 0006, restated here because it now applies to a new data flow). The direction
of dependency is fixed and one-way:

```text
Neo4j Memory.id  (created first, always)
        │
        ▼
PostgreSQL memory_embeddings.memory_id  (created after, asynchronously, at-least-once)
```

**A `memory_embeddings` row can lag or be briefly missing; it must never be able to
imply that a memory does not exist, and Neo4j is never blocked on it.**

Failure cases, matching the same rigor as the Phase 2 hardening pass (section 3 of
`docs/PHASE2_HARDENING.md`):

| Case | Outcome |
|---|---|
| Neo4j write succeeds, embedding job never runs (process died before the `BackgroundTask` fired) | Memory exists, fully usable via graph retrieval. Absent from vector retrieval until a backfill sweep (section 13) finds it — same "sweep catches what the live path missed" pattern as extraction. |
| Embedding call succeeds, PostgreSQL insert fails (DB blip) | `ON CONFLICT DO NOTHING` on `(memory_id, provider, model)` makes a retry safe; the backfill sweep's `find memories without embedding` query naturally re-attempts it. |
| PostgreSQL insert succeeds, process crashes before any bookkeeping | There is no separate bookkeeping step here (unlike extraction's job/candidate split) — the insert itself is the whole unit of work, so this case does not apply. |
| Embedding model changes mid-flight (config updated while a backfill is running) | The backfill run started under the old model completes writing rows tagged with *that* model; a new backfill run for the new model is a separate, independently resumable pass (section 13). Retrieval always queries by the *currently configured* `(provider, model)`, so a partially-completed migration never serves a mixed result set — it serves exactly what has been embedded under the active config, nothing else. |
| Vector index/PostgreSQL unavailable at query time | `HybridRetrievalService` catches this as a `DatabaseUnavailableError` (existing error taxonomy) around the vector branch specifically, logs it, and falls back to graph-only retrieval for that request rather than failing the whole query. **Vector index missing is never treated as "memory does not exist."** |

This is documented as **ADR 0009** (see section 22) since it is a genuine
consequential decision, in the same family as ADR 0006.

---

## 5. Semantic duplicate detection

Extends `app/extraction/decisions.py`, does not replace it. The exact-triple check
runs first, unchanged, exactly as today (`classify_relationship`). Semantic
similarity is consulted **only when the exact check already found no active
same-target edge** — i.e., it is a second-pass signal for the cases exact matching
structurally cannot catch, not a competing decision path.

```text
classify_relationship (exact triple, unchanged, existing code)
        │
        ├── active_same_target found ──────────► DUPLICATE/UPDATE (unchanged)
        │
        └── no active same-target edge
                 │
                 ▼
        classify_semantic_evidence   <- NEW, this section
                 │
        embedding similarity(candidate.statement, existing memory.content)
                 +
        same source entity (already resolved, exact match)
                 +
        predicate compatibility (same predicate, or a configured equivalent set)
                 +
        temporal/change signal (candidate.temporal.is_change, same as today)
                 ↓
        one of: LIKELY_DUPLICATE | FLAGGED | (fall through to existing NEW/CONFLICTING)
```

**New `DecisionType` members: `LIKELY_DUPLICATE`, `FLAGGED`.** Neither is
`DUPLICATE`. This is the load-bearing rule the user specified:

```text
similarity > threshold  ⇏  automatic merge
```

- `DUPLICATE` (existing) stays reserved for what today already produces it: exact
  triple match. It is the only outcome that silently attaches a source to an
  existing memory without any new node.
- `LIKELY_DUPLICATE` (new) is a **persisted memory** — created as `NEW` in Neo4j
  (so nothing is silently dropped) but recorded with a link to the candidate
  match and a similarity score, specifically so a human or later phase can review
  and, if confirmed, mark it `DUPLICATE`/merge provenance after the fact. It is
  never auto-collapsed into the existing memory.
- `FLAGGED` (new) is for evidence too weak to even confidently call
  `LIKELY_DUPLICATE` (below a second, lower threshold) — persisted as `NEW`, no
  special linkage, but recorded with its raw similarity score for evaluation and
  future threshold tuning.

Evidence structure (pure, testable, no I/O — mirrors `RelationshipDecision`):

```python
@dataclass(frozen=True)
class SemanticEvidence:
    similarity: float                    # cosine, [0, 1] after normalization
    same_source_entity: bool
    predicate_compatible: bool
    signals_change: bool
    candidate_memory_id: str             # the existing memory being compared

def classify_semantic_evidence(
    evidence: SemanticEvidence,
    *,
    duplicate_threshold: float,          # config, e.g. SEMANTIC_DUPLICATE_THRESHOLD
    flag_threshold: float,               # config, e.g. SEMANTIC_FLAG_THRESHOLD
) -> SemanticDecision:
    ...
```

Both thresholds are `Settings` fields (`SEMANTIC_DUPLICATE_THRESHOLD`,
`SEMANTIC_FLAG_THRESHOLD`), never inline magic numbers — matching
`EXTRACTION_MIN_MESSAGE_CHARS`'s existing precedent for a tunable heuristic
constant. Defaults are chosen conservatively high (favor `FLAGGED` over
`LIKELY_DUPLICATE`, favor `NEW` over either) and are expected to be tuned against
the evaluation harness (section 16), not guessed once and left alone.

**Same-predicate-different-object stays exactly as it is today** —
`classify_relationship`'s existing CONFLICTING/SUPERSEDING logic for "same subject,
same predicate, different object" is **not** touched by semantic similarity at
all. Semantic evidence only ever applies when the exact-triple check found
*nothing* to compare against (a different phrasing of the same fact, e.g. "uses
Gemini for classification" vs. "classifies with Gemini"), never when it found a
different-object edge for the same predicate (that is the temporal-change
question, section 6, deliberately kept independent of semantic similarity per the
user's explicit instruction).

---

## 6. Semantic conflict detection

The user's example is the exact case Phase 2 already handles correctly and
Phase 3 must not regress:

```text
Existing: Drawing Extractor → uses → EasyOCR
New:      Drawing Extractor → uses → PaddleOCR
```

This is a same-subject, same-predicate, different-object case — it goes through
`classify_relationship`'s **existing, unchanged** temporal-change branch
(`is_change`/`replaces_hint` → SUPERSEDING; no change signal → CONFLICTING).
Semantic similarity plays **no role** in this decision, by design: embedding
similarity between "uses EasyOCR" and "uses PaddleOCR" is not evidence about
replacement-vs-conflict-vs-concurrent-use; only the temporal/change signal is
(exactly as the user specified: "the temporal/change signal should remain
important").

What semantic similarity *does* add here is narrower and different: detecting
that two memories asserting the **same predicate with semantically equivalent,
not identical, phrasing of the same object** might be the same fact restated
differently — e.g. "uses Gemini for classification" vs. "the classifier is
Gemini-based." That is a duplicate-detection question (section 5), not a conflict
question, and is handled entirely by `classify_semantic_evidence` above.
`classify_relationship` is not modified to accept a similarity score at all —
keeping the two functions' responsibilities disjoint is what keeps the
already-approved, already-tested exact-triple logic provably unaffected by this
phase.

---

## 7. Entity resolution

Extends `app/extraction/resolution.py`'s existing "detect, never merge" pattern
with one new signal, written into the *existing* `EntityMergeCandidate` table
(section 2.3) rather than a new one.

```text
is_plausible_duplicate (existing, exact/canonical-key match) -- unchanged, runs first
        │
        └── no match found
                 │
                 ▼
        embedding similarity(entity_a.name, entity_b.name)   <- NEW
                 +
        graph neighborhood overlap (shared incident predicates/targets)   <- NEW
                 ↓
        similarity ≥ SEMANTIC_ENTITY_FLAG_THRESHOLD?
                 │
                YES ──► record_candidate(..., score=similarity, evidence={"signal": "embedding", ...})
                 │
                NO ───► no record (an unbounded low-similarity comparison would be pure noise)
```

**Still never merges — this is the one rule from Phase 2 explicitly preserved.**
The evidence is entity **name** similarity plus graph context (do the two
candidate entities share incident relationships to the same other entities? — a
purely structural, deterministic signal, no embedding needed for that part), not
a similarity of the entities' associated memory content (that would conflate
"these two things are semantically related" with "these two things are the same
thing," a different and much riskier claim). `EntityMergeCandidate.status` stays
`unreviewed` until a human (or, later, an explicit review workflow — not part of
Phase 3) marks it `merged`/`rejected`.

---

## 8. Hybrid retrieval

```text
User query (text)
    │
    ├─────────────────────────┬─────────────────────────┐
    ▼                         ▼                          ▼
VectorRetrieval          GraphRetrieval             KeywordRetrieval
(NEW: embed query,       (existing:                 (existing:
 pgvector cosine          RetrievalService /          MemoryService.search's
 search, top-N)           MemoryService.search's      CONTAINS query --
                          entity/relationship path)   already "keyword")
    │                         │                          │
    └─────────────┬───────────┴──────────────────────────┘
                  ▼
         candidate_ids: set[memory_id], each tagged with which
         retriever(s) found it and that retriever's raw score
                  ▼
         Batch-fetch authoritative state from Neo4j
         (MemoryRepository.get, or a new bounded batch read)
                  ▼
         Temporal filter (section 9) -- drop what the query's
         intent excludes, using Neo4j's status/valid_from/valid_until,
         never a PostgreSQL copy
                  ▼
         Rank (section 8.1)
                  ▼
         Top-K MemoryDetail + provenance (section 11)
```

`MemoryService.search`'s existing CONTAINS-based `query` filter is *kept and
reframed* as the keyword retriever rather than replaced — Phase 1's deterministic
substring search remains available and contributes its own candidate set and
score (exact substring match = score 1.0) into the same merge step. This is the
"do not simply concatenate, design a proper merge" requirement: candidates found
by more than one retriever are not scored twice and re-ranked from raw
concatenation — they are deduplicated by `memory_id` first, and every score they
carry is available to the ranking formula, not just whichever retriever ran
first.

### 8.1 Ranking formula — explicit, configurable, not learned

```python
def rank_score(candidate: RankingInput, weights: RankingWeights) -> float:
    return (
        weights.vector_similarity * candidate.vector_similarity      # 0 if not found by vector
        + weights.graph_relevance * candidate.graph_relevance        # 0 if not found by graph
        + weights.keyword_match   * candidate.keyword_match          # 0 if not found by keyword
        + weights.recency         * candidate.recency_score          # decays with age
        + weights.temporal_fit    * candidate.temporal_fit           # 1.0 if matches query's
                                                                      # current/historical intent,
                                                                      # 0 otherwise (never negative --
                                                                      # a query with no explicit
                                                                      # temporal intent should not
                                                                      # penalize anything)
        + weights.confidence      * candidate.confidence             # from the memory itself
    )
```

`RankingWeights` is a `Settings`-backed dataclass (`RETRIEVAL_WEIGHT_VECTOR`,
`RETRIEVAL_WEIGHT_GRAPH`, etc.), not hardcoded — explicitly so the ablation studies
in section 17 can zero out one term and re-run the exact same evaluation dataset.
This is a linear weighted sum deliberately, not a learned reranker: introducing a
trained model is a much larger commitment (training data, drift, an additional
artifact to version) that nothing in this codebase's scale justifies yet, and a
transparent formula is directly explainable per-candidate (section 11), which a
black-box reranker is not. If evaluation (section 16) shows the linear formula is
insufficient, that is itself a documented, evidence-based Phase 4 case for a
learned reranker — not a default assumption now.

### 8.2 Graph relevance score

`graph_relevance` for a candidate found via the graph path is derived from
existing, already-computed structure — no new graph algorithm: 1.0 for a direct
entity/relationship keyword hit, decaying by hop distance for anything surfaced
via `get_neighbourhood`'s N-hop traversal (already bounded by `GRAPH_MAX_HOPS`).
A candidate found *only* by vector or keyword search gets `graph_relevance = 0`,
not penalized further — absence of a graph signal is neutral, not negative,
consistent with `temporal_fit`'s same design choice above.

---

## 9. Query understanding — deterministic first, no LLM dependency

Per the explicit instruction: no LLM query planner yet. Temporal intent is
detected with the same kind of small, testable, deterministic heuristic that
`app/extraction/gate.py` already uses for a different purpose:

```python
def detect_temporal_intent(query: str) -> TemporalIntent:
    """CURRENT | HISTORICAL | AS_OF(datetime) | UNSPECIFIED.

    Pure pattern matching against a fixed, tested vocabulary -- mirrors
    app/extraction/gate.py's "cheap deterministic gate before anything expensive"
    shape. "now", "current", "currently" -> CURRENT. "before", "used to",
    "previously", "used to be" -> HISTORICAL. An explicit date/month name
    ("in March", "in 2026") -> AS_OF, parsed narrowly (month + optional year;
    NOT general natural-language date parsing -- same scoping call already made
    for CandidateTemporal hints in docs/PHASE2_PLAN.md). Anything else ->
    UNSPECIFIED, which applies no temporal filter or ranking boost at all.
    """
```

`UNSPECIFIED` is the safe default and is expected to be the common case — this
heuristic is deliberately narrow and will under-detect intent for many real
queries. That is an accepted, measured limitation (section 16 measures exactly
how often `AS_OF`/`HISTORICAL`/`CURRENT` are correctly detected), not a hidden
one. An LLM-based query decomposition layer is named as an explicit **optional**
future addition (section 23's "not now" list) — retrieval must work correctly
end-to-end with zero LLM involvement before one is ever added on top.

---

## 10. Temporal retrieval

The vector index has and can have **no concept of validity windows or status** —
pgvector returns nearest neighbors by embedding distance alone, full stop. All
temporal correctness is enforced in one place, after the merge, against Neo4j's
authoritative state (section 4's consistency model in action):

```text
vector search returns: [(memory_id_1, 0.94), (memory_id_2, 0.91), ...]
                                    │
                                    ▼
                batch-fetch each memory's authoritative status/valid_from/
                valid_until/created_at from Neo4j (never trust a PostgreSQL copy)
                                    │
                                    ▼
        detect_temporal_intent(query) == CURRENT
                    → drop any candidate where is_currently_believed(...) is False
        == HISTORICAL
                    → drop any candidate that IS currently believed (superseded/
                      historical only)
        == AS_OF(t)
                    → drop any candidate where is_valid_at(..., t) is False
        == UNSPECIFIED
                    → no drop; temporal_fit contributes 0 either way (section 8.1)
```

This directly targets the user's named risk: "the vector layer must not
accidentally return an old memory as current simply because it has a high
semantic similarity score." A high-similarity superseded memory can still be
*returned* (for a `HISTORICAL`/`AS_OF` query, or when intent is `UNSPECIFIED`) but
can never be labeled or ranked as if it were current — that label always comes
from the live Neo4j read in this step, never from anything cached in
`memory_embeddings`.

Regression test committed to the plan up front (not deferred to "we'll add tests
later"): the exact March/EasyOCR-vs-current/PaddleOCR scenario from the prompt,
run through the full hybrid path, asserting the `CURRENT` query returns PaddleOCR
and the `AS_OF(March)` query returns EasyOCR, even when EasyOCR's embedding is
the closer vector match (constructed deliberately in the fixture, so the test
actually exercises the temporal filter rather than passing by coincidence).

---

## 11. Provenance

`HybridRetrievalService` returns, per result, a `RetrievalExplanation` alongside
the existing `MemoryDetail`/`ProvenanceView` (both unchanged, reused as-is):

```python
@dataclass(frozen=True)
class RetrievalExplanation:
    memory_id: str
    retrieved_via: list[str]          # e.g. ["vector", "graph"] -- exactly which
                                       # retrievers actually found this candidate
    vector_similarity: float | None   # None if not found via vector
    graph_relevance: float | None     # None if not found via graph
    keyword_match: float | None
    temporal_fit: float
    final_score: float
    temporal_state: str               # "current" | "historical" | "superseded" | ...
```

Every field here is a value the ranking step already computed for real — nothing
is synthesized after the fact for display. If a component contributed nothing to
a given candidate, its field is `None`/0, not a fabricated plausible-looking
number. This directly satisfies "do not fabricate explanations if the ranking
system does not actually record them" — the explanation *is* the ranking
system's own working, exposed, not a separate narrative layer.

---

## 12. Embedding model/version management

Already anticipated by section 2.1's schema. Concretely:

- `Settings.embedding_provider`, `embedding_model`, `embedding_dimension` — the
  active configuration; `.env.example` already carries `EMBEDDING_PROVIDER`/
  `EMBEDDING_MODEL` as unused Phase 3 placeholders (from Phase 2's `.env.example`),
  which this phase fills in.
- **Startup validation:** on `Database.connect()`, if `EMBEDDING_PROVIDER` is
  configured, assert that `Settings.embedding_dimension` matches the pgvector
  column's declared dimension (queryable via `pg_catalog` or simply asserted
  against the value the active Alembic migration declared). A mismatch is a
  `ConfigurationError` at startup — the same "fail loud at boot, not silently
  corrupt later" principle `Settings`'s existing production guard already applies
  to `llm_provider`.
- **Migrating to a new model:** update `EMBEDDING_MODEL`/`EMBEDDING_PROVIDER` in a
  *staging* config first, run the backfill (section 13) under that config against
  a database that still serves the *old* config in production, verify via the
  evaluation harness, then cut the production config over. Old rows are never
  deleted automatically — a separate, explicit `scripts/prune_embeddings.py`
  (Phase 3+, not built until actually needed) would be the deliberate cleanup
  step, matching "we should eventually be able to migrate... without corrupting
  the old index" without adding automatic deletion logic nothing has asked for
  yet.

---

## 13. Backfill strategy

Mirrors the extraction sweep's shape almost exactly (`run_sweep_tick` in
`app/extraction/runner.py`) — same bounded-batch, resumable, idempotent pattern,
applied to a different query:

```python
async def run_embedding_backfill_tick(
    *, db: Database, graph: GraphClient, embedding_client: EmbeddingClient,
    settings: Settings, batch_size: int = 50,
) -> dict[str, int]:
    """One bounded batch: find memories with no row for the *current* (provider,
    model), embed them, insert. Safe to call repeatedly (cron, manual, or the
    same internal-sweep endpoint pattern as extraction) until it reports 0 done.
    """
```

- **Resumable:** progress is the `memory_embeddings` table itself — there is no
  separate cursor to lose; a killed backfill run simply leaves fewer rows than
  target, and the next run picks up exactly where it left off by re-querying
  "memories without a row for this (provider, model)."
- **Idempotent:** the `(memory_id, provider, model)` unique constraint plus `ON
  CONFLICT DO NOTHING` makes a concurrent or repeated attempt at the same memory
  a no-op, identical in spirit to `extraction_candidates`' idempotency layer.
- **Bounded:** `batch_size`, same role as `EXTRACTION_SWEEP_BATCH_SIZE`.
- **Observable:** returns counts (`embedded`, `skipped_already_present`,
  `failed`) the same shape as `run_sweep_tick`'s return dict, logged by whatever
  calls it (a new `POST /internal/embeddings/backfill`, token-gated exactly like
  `POST /internal/extraction/sweep`, plus optionally a scheduled cron the same
  way the sweep is scheduled).
- **Finding "memories without an embedding" requires reading Neo4j, not just
  Postgres:** the source of truth for "which memories exist" is Neo4j. The
  backfill queries Neo4j for memory ids (a new, narrow, bounded, user-scoped
  Cypher read — `LIST_MEMORIES_FOR_BACKFILL`, paginated by id, no different in
  shape from the existing `LIST_ALL_ENTITIES` added during the Phase 2 hardening
  pass), then anti-joins against `memory_embeddings` in the backfill driver
  (application-side set difference over one page at a time — avoids a
  cross-database `JOIN`, which does not exist, while staying bounded).

---

## 14. Failure consistency

Restating section 4's table in terms of the specific scenarios asked for:

- **Neo4j succeeds, embedding fails:** memory fully exists and is fully usable
  (graph retrieval, provenance, all Phase 1/2 behavior unaffected). It is simply
  invisible to the vector retriever until backfill/retry catches it. Never an
  error surfaced to the user who created the memory.
- **Embedding succeeds, PostgreSQL write fails:** the embedding is discarded (no
  side effect survives); the memory remains embedding-less until the next
  backfill tick regenerates it. No partial/corrupt row is possible — the insert
  is a single atomic statement.
- **PostgreSQL vector write succeeds, process crashes before "bookkeeping":**
  does not apply here (section 4) — unlike extraction, there is no separate
  bookkeeping step after the embedding insert.
- **Embedding model changes:** covered by section 12 — old and new model's rows
  coexist; retrieval only ever reads the currently-configured model's rows.
- **Vector index/PostgreSQL unavailable at query time:** `HybridRetrievalService`
  degrades to graph-only retrieval for that request (section 4's table) — this is
  tested explicitly (section 18) by simulating `DatabaseUnavailableError` from
  the embedding repository and asserting the graph branch's results still return
  correctly and the request does not 500.

---

## 15. pgvector design

- **Extension:** `CREATE EXTENSION IF NOT EXISTS vector;` in the Phase 3 Alembic
  migration. Supported by every target managed host named in the deployment
  architecture (Supabase, Neon, Railway's own Postgres, and self-hosted
  PostgreSQL 13+) — confirmed as a standard, non-superuser-restricted extension
  on all of them; this is worth a one-line explicit check against whichever host
  is actually chosen at deploy time (section 21), not assumed silently.
- **Column type:** `vector(:dimension)`, dimension from `Settings.embedding_dimension`
  at migration-authoring time (a schema property, necessarily fixed per
  deployment — this is exactly what section 12's startup validation guards).
- **Similarity metric: cosine.** Standard choice for text embeddings from a
  general-purpose provider (Gemini's embedding models are documented as
  cosine/dot-product appropriate); pgvector's `vector_cosine_ops` operator class.
- **Index: HNSW, not IVFFlat, and not built until section 20 shows it is needed.**
  IVFFlat requires a representative sample to train cluster centroids *before* it
  is useful, which is actively wrong for an early, small, and initially-empty
  table — a freshly-built IVFFlat index on little data performs worse than a
  sequential scan and needs manual retraining as data grows. HNSW builds
  incrementally with no training step and degrades gracefully at small scale,
  which is the right default given "correctness first, do not optimize
  prematurely" (both the user's instruction here and ARCHITECTURE.md §29). The
  index is created in the migration (`CREATE INDEX ... USING hnsw (embedding
  vector_cosine_ops)`) with pgvector's default `m`/`ef_construction` — tuning
  those is explicitly deferred to section 20's benchmarking, not guessed now.
- **Query strategy:** `ORDER BY embedding <=> :query_vector LIMIT :k`, scoped by
  `WHERE user_id = :user_id AND embedding_provider = :provider AND
  embedding_model = :model` — the isolation filter is **structural**, not
  optional, exactly like every other `UserScopedRepository` query (section 19).

---

## 16. Evaluation

Extends the existing deterministic harness (`eval/runner.py`, `eval/metrics.py`)
rather than building a second one — same `FakeLLMClient`-driven, real-Neo4j,
real-PostgreSQL pattern, now paired with `FakeEmbeddingClient` so the *default*
Phase 3 evaluation stays free, offline, and deterministic, matching Phase 2's
explicit precedent.

**Scoped down from EVALUATION.md's full aspirational framework, deliberately.**
EVALUATION.md's baselines A–E and its agent/citation/answer-correctness metrics
assume an agent that generates natural-language answers from retrieved context —
that agent does not exist yet (it is Phase 5). Building a full LLM-answer-quality
harness now would mean fabricating an agent just to evaluate it, which is exactly
the kind of premature, illustrative-number risk EVALUATION.md §20 itself warns
against ("do not use illustrative values"). Phase 3's evaluation is honestly
scoped to what Phase 3 actually builds:

| In scope for Phase 3 | Deferred to Phase 5 (needs an agent) |
|---|---|
| Recall@K / Precision@K / MRR / NDCG for **retrieval** (graph-only vs. vector-only vs. hybrid, deterministic gold set) | Answer correctness, citation precision/recall in a generated *answer* (Baselines A/D/E's final "→ LLM → Answer" step) |
| Semantic duplicate/conflict decision accuracy (extends `eval/metrics.py`'s existing decision-accuracy machinery) | Agent tool-selection accuracy |
| Entity-resolution flag precision/recall against a labeled fixture set | LLM-as-judge anything |
| Temporal accuracy for hybrid results (section 10's regression case, generalized into a small gold set) | |

This is a scoping decision, stated plainly, not a silent shrinking of
EVALUATION.md's ambition — Baselines B/C/D (Vector RAG / Graph RAG / Hybrid,
*minus* their "→ LLM → Answer" step) are exactly what Phase 3's retrieval
evaluation measures; Baselines A and E's LLM-answer step is what is deferred.

**Dataset:** `eval/datasets/retrieval_v1.jsonl`, same JSONL shape discipline as
`extraction_v1.jsonl` — each row a query, its temporal intent (if any), and a set
of expected `memory_id`-equivalent gold facts (constructed via the same
conversation-replay setup `eval/runner.py` already uses, so gold memory ids are
real ids from a real pipeline run, never hand-typed). Explicitly includes
constructed cases where graph retrieval should win (multi-hop, exact predicate
match) and cases where vector retrieval should win (paraphrased/synonym queries
with no exact keyword overlap) — the user's explicit ask in section 16.

**Failure localization (carrying forward the Phase 2 hardening fix to
`eval/runner.py`):** a retrieval-evaluation failure's detail records which stage
diverged — no candidates surfaced by any retriever (embedding/generation
problem), candidates surfaced but ranked below K (ranking-formula problem), or
candidates ranked correctly but temporally filtered out incorrectly (temporal
logic problem) — three distinguishable categories, not one generic "wrong
result."

---

## 17. Ablation studies

`RankingWeights` (section 8.1) being config-driven is what makes this cheap: the
harness runs the *same* `retrieval_v1.jsonl` dataset once per weight
configuration:

```text
Full system            (all weights as configured)
- vector                (RETRIEVAL_WEIGHT_VECTOR = 0)
- graph                 (RETRIEVAL_WEIGHT_GRAPH = 0)
- keyword               (RETRIEVAL_WEIGHT_KEYWORD = 0)
- temporal_fit          (RETRIEVAL_WEIGHT_TEMPORAL = 0, filter still applied --
                          this ablates the *ranking boost*, not correctness)
- entity/graph context in semantic duplicate detection
```

Reported the same way `eval/metrics.py`'s `EvaluationReport.category_accuracy`
already reports per-category numbers — extended with one row per ablation
configuration, never hand-written.

---

## 18. Testing

**Unit (no database, no network — `FakeEmbeddingClient` everywhere):**
`app/semantic/similarity.py`'s pure functions (`classify_semantic_evidence`,
exhaustive threshold/boundary cases, mirroring `test_extraction_decisions.py`'s
30-test shape), `detect_temporal_intent` (section 9, mirroring
`test_extraction_gate.py`'s shape), the ranking formula (`rank_score` with
crafted `RankingInput`s, including all-zero and all-max edge cases),
`FakeEmbeddingClient` itself (mirroring `test_llm_fake.py`), dimension-validation
guards, `MemoryEmbedding` model/constraint shape (mirroring `test_sql_safety.py`'s
coverage), the embedding-provider factory (mirroring `test_llm_gemini.py`'s
construction-guard tests for the Gemini embedding adapter, no network call).

**Integration (real Neo4j + PostgreSQL, `FakeEmbeddingClient`, skips cleanly
without services — same guard as every existing integration test):** embedding
generation → storage round-trip, dimension mismatch at startup, vector similarity
search correctness (crafted vectors with known distances), semantic duplicate
detection through the full pipeline (extends `test_extraction_runner.py`'s
scenario-battery pattern with new semantic-duplicate/semantic-conflict
scenarios), entity-similarity flagging (extends the existing
`test_open_ai_vs_openai_are_flagged_never_merged` pattern with an embedding-based
case), temporal filtering in hybrid retrieval (section 10's March/current
regression case), hybrid candidate merging and ranking, provenance/explanation
correctness, backfill (resumability: kill it mid-batch, confirm the next run
finishes; idempotency: run it twice, confirm no duplicate rows), failure recovery
(section 14's four scenarios, each as an explicit test — matching the Phase 2
hardening pass's standard of proving failure handling rather than asserting it),
and model/version change handling (embed under model A, change config to model
B, backfill, confirm both rows exist and retrieval only returns model B's).

**User isolation (section 19) as its own explicit test class**, not folded into
functional tests: `test_vector_search_never_returns_another_users_memory` in the
same spirit as the Phase 2 hardening pass's
`test_message_lookup_itself_is_user_scoped_defense_in_depth` — a defense-in-depth
proof, not just an inference from reading the code.

**Real-provider tests** (actual Gemini embedding calls) live under the existing
`llm` pytest marker's sibling scope — never in default CI, never required for the
unit or integration suites to pass, exactly as `test_llm_gemini.py`'s
construction-only tests already establish for the LLM side.

---

## 19. Security

Every new PostgreSQL query goes through `UserScopedRepository` (unchanged base
class, `app/repositories/embedding_repository.py` extends it exactly like every
other Phase 2 repository) — statically covered by the existing
`test_sql_safety.py` AST audit with zero changes to that audit needed, since it
already walks every file under `app/repositories/`.

The specific risk named — "a similarity search must never become a side-channel
that bypasses Neo4j's existing isolation" — is closed structurally, not just by
convention: the pgvector query's `WHERE user_id = :user_id` clause is part of
`_select()`'s automatic injection (section 15's query strategy), so a similarity
search literally cannot be expressed without it in this codebase's repository
pattern, the same guarantee Cypher's `$user_id` audit already provides on the
graph side. The explicit regression test (section 18): create memories for two
distinct users with deliberately near-identical content (near-identical
embeddings almost guaranteed), run a vector search as each user, assert neither
ever sees the other's result.

---

## 20. Performance

No index tuning, no caching layer, and no benchmarking-driven architecture
changes before there is a measurement to justify them (ARCHITECTURE.md §29,
restated in this phase's own instructions). What Phase 3 does establish, cheaply,
as part of the eval harness's own run rather than a separate benchmarking
project:

- Embedding generation latency (`FakeEmbeddingClient` reports near-zero, a real
  provider's latency is measured only under the opt-in `llm`-marker tests,
  matching how LLM latency is already only measured that way).
- Vector query latency at the dataset's actual small scale (tens to low
  hundreds of rows in the evaluation dataset) — HNSW's own behavior at that
  scale, no synthetic scale testing invented for this phase.
- Graph query latency (already implicitly measured by every existing integration
  test's wall time; no new instrumentation invented).
- Hybrid end-to-end latency, as one more number `eval/runner.py` prints, the same
  way it already reports `latency_ms` per extraction job.

All of this is reported as *measured numbers from actual runs* (EVALUATION.md
§20's rule, inherited directly), explicitly not extrapolated to production scale
—if and when real usage numbers make load-testing worthwhile, that is a Phase 4+
question this plan does not attempt to answer in advance.

---

## 21. Deployment target

Unchanged from the Phase 2 report, with one addition:

```text
Vercel  →  Railway FastAPI  →  PostgreSQL (+ pgvector) + Neo4j Aura + Embedding provider
```

No Docker, no Kubernetes, no separate vector database — pgvector is a PostgreSQL
extension, enabled via a migration, on the same managed Postgres already in use.
The one deployment-relevant check named in section 15: whichever managed
PostgreSQL host is actually used (Railway's own, or Supabase/Neon if that changes)
must have `pgvector` available as an installable extension — this is standard on
all of them today, but is worth confirming against the actual chosen host before
the Phase 3 migration ships, not assumed.

---

## 22. Documentation

- `docs/PHASE3_PLAN.md` — this document.
- **`docs/adr/0009-pgvector-for-embeddings-neo4j-remains-canonical.md`** — the one
  genuine new architectural decision (the consistency model in section 4, and the
  choice of pgvector-in-existing-Postgres over a dedicated vector database),
  directly extending ADR 0006's precedent. Drafted alongside this plan for
  review, not yet needed as a separate approval step since it documents the same
  decision this plan proposes.
- No other new ADRs. The ranking formula, the HNSW-vs-IVFFlat choice, the
  `LIKELY_DUPLICATE`/`FLAGGED` decision types, and the entity-resolution
  extension are implementation details of an already-decided architecture
  (semantic evidence augments, never replaces, the Phase 2 conservative
  decision engine) — each is justified inline in this plan, not elevated to its
  own ADR, per the explicit instruction not to create ADRs for implementation
  details.
- `docs/API.md` gets a new section for `POST /search` and the embedding/backfill
  internal endpoint, matching how Phase 2's endpoints were documented — written
  at implementation time, not speculated here.

---

## 23. Implementation order

Proposed sequence, with one reordering relative to the suggested outline and the
reasoning for it:

```text
1.  pgvector foundation           (extension + migration + MemoryEmbedding model)
2.  Embedding abstraction         (EmbeddingClient protocol + Settings fields)
3.  FakeEmbeddingClient           (must exist before anything else can be tested)
4.  Embedding persistence         (embedding_repository.py + the post-create
                                   BackgroundTask hook)
5.  Semantic similarity service   (app/semantic/similarity.py -- PURE, no I/O;
                                   moved earlier than "after backfill" because it
                                   has zero dependency on real embeddings existing
                                   and can be exhaustively unit-tested in isolation
                                   immediately, the same way decisions.py was
                                   built and tested before the runner that calls it)
6.  Backfill                      (needs 1-4 to exist; needed before 7-9 can be
                                   evaluated against realistic, non-empty data)
7.  Semantic duplicate/conflict
    integration into the decision
    engine                        (wires 5 into app/extraction/decisions.py's
                                   call site in the runner)
8.  Entity resolution enhancement (wires 5's similarity primitive into
                                   app/extraction/resolution.py; independent of
                                   7, could run in parallel with it)
9.  Vector retrieval              (the new retriever alone, tested standalone
                                   before merging with graph retrieval)
10. Hybrid retrieval + ranking    (merge vector + existing graph/keyword paths)
11. Temporal-aware ranking        (folded into 10, not a separate step -- the
                                   ranking formula and the temporal filter are
                                   designed together in section 8/10, and
                                   shipping ranking without temporal correctness
                                   even briefly would risk exactly the "old
                                   memory looks current" bug the plan exists to
                                   prevent)
12. Evaluation                    (retrieval_v1.jsonl + harness extension --
                                   needs 9-10 to exist to evaluate against)
13. Performance benchmarks        (folded into 12's harness run, not a separate
                                   phase -- section 20)
14. Hardening                     (a dedicated pass, same shape as
                                   docs/PHASE2_HARDENING.md, after everything
                                   above is implemented and evaluated once)
```

The only substantive reordering from the suggested outline: **semantic
similarity (step 5) moves earlier**, immediately after `FakeEmbeddingClient`
rather than after backfill — because it is pure logic with no dependency on
real, backfilled data (exactly like `app/extraction/decisions.py` was written and
exhaustively tested before `ExtractionRunner` existed to call it), and having it
ready early means step 7 (wiring it into the decision engine) is a small,
low-risk integration rather than a large one. Steps 12–13 are explicitly merged
per section 20's own conclusion that a separate benchmarking phase is not
justified before there are real numbers to act on.

---

## 24. New dependencies

- `pgvector` (Python package, thin SQLAlchemy type integration — not a service).
- No new LLM-adjacent dependency for embeddings if Gemini's existing
  `google-genai` SDK's embedding endpoint is used (to be confirmed at
  implementation time; if a separate embeddings SDK/endpoint is required, it is
  isolated to `app/embeddings/providers/gemini.py` exactly as `google.genai`
  is isolated to `app/llm/providers/gemini.py` today).
- No vector database, no Redis, no Celery, no LangChain — unchanged from every
  prior phase's constraint.

## 25. New environment variables

```env
EMBEDDING_PROVIDER=                # "fake" (default/test) | "gemini"
EMBEDDING_MODEL=                   # exact model id, env-only, never hardcoded
EMBEDDING_DIMENSION=               # must match the pgvector column; validated at startup
SEMANTIC_DUPLICATE_THRESHOLD=      # cosine similarity, e.g. 0.90
SEMANTIC_FLAG_THRESHOLD=           # cosine similarity, e.g. 0.80
SEMANTIC_ENTITY_FLAG_THRESHOLD=    # entity-name embedding similarity, e.g. 0.85
RETRIEVAL_WEIGHT_VECTOR=
RETRIEVAL_WEIGHT_GRAPH=
RETRIEVAL_WEIGHT_KEYWORD=
RETRIEVAL_WEIGHT_RECENCY=
RETRIEVAL_WEIGHT_TEMPORAL=
RETRIEVAL_WEIGHT_CONFIDENCE=
RETRIEVAL_TOP_K=                   # default candidates returned per search
```

`EMBEDDING_PROVIDER`/`EMBEDDING_MODEL` already exist as unused placeholders in
`.env.example` from Phase 2 — this phase is what fills them in.

---

## 26. Deployment impact

None beyond section 21: same Railway/Vercel/Aura targets, one new PostgreSQL
extension enabled via migration, no new infrastructure category.

---

## 27. Risks and tradeoffs

- **`LIKELY_DUPLICATE`/`FLAGGED` add review burden without a review UI.** Phase 3
  builds the detection and the persisted signal; a dedicated review workflow
  (approve/reject a flagged pair, similar to what `EntityMergeCandidate.status`
  already anticipates for entities) is not in this phase's scope and is named as
  a Phase 4+ candidate — until then, these are inspectable via direct query
  (matching how `entity_merge_candidates` is inspectable today), not surfaced in
  any UI.
- **Threshold tuning is a real, ongoing cost.** `SEMANTIC_DUPLICATE_THRESHOLD`
  and friends are configuration, not the plan claiming to know the right values
  in advance — the evaluation harness (section 16) exists specifically to make
  tuning them an evidence-based exercise rather than a guess.
- **Embedding provider cost and latency** are a new, real operating cost the
  system did not have before (mirroring the same tradeoff Gemini's LLM calls
  already introduced in Phase 2) — mitigated the same way: `FakeEmbeddingClient`
  keeps development and CI free, and the async, non-blocking lifecycle (section
  3) means a slow or failing embedding provider never affects the user-facing
  request path.
- **HNSW's approximate nature** means vector search is not guaranteed to return
  the true top-K nearest neighbors, only a close approximation — acceptable for
  a memory-retrieval ranking signal (further refined by the deterministic
  temporal filter and multi-signal ranking formula) but worth stating plainly
  rather than implying vector search is exact.
- **Two independent stores for one logical concept (a memory's retrievability)**
  is exactly the tradeoff ADR 0006 already accepted for conversations/extraction
  state, now extended to embeddings — the mitigations are the same in kind
  (one-directional dependency, at-least-once semantics, never treat the
  secondary store's absence as the primary's absence) and are documented in ADR
  0009 rather than re-litigated per feature.

---

## Summary: what this plan does and does not authorize

**Authorized by this plan, once approved:** implementing sections 1–19 and the
implementation order in section 23, in that order, with the same testing and
verification discipline as Phases 1 and 2 (real Neo4j + PostgreSQL verification,
not just unit tests; a hardening pass at the end; no fabricated evaluation
numbers).

**Not authorized, and not part of this plan:** a learned/trained reranker, an
LLM-based query planner, a dedicated vector database, a review UI for flagged
duplicates/merges, automatic embedding pruning, Redis/Celery/any new
infrastructure category, or any change to the exact-triple check, the
conservative decision policy, or the Neo4j-is-canonical boundary.

Waiting for approval before writing any Phase 3 application code.
