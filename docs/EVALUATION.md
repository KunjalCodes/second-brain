# EVALUATION.md

# AI Second Brain

## Evaluation Framework

> **Implementation status (updated Phase 7):** this document is the full
> aspirational framework, written before an agent or answer-generation layer
> existed. It has since been substantially realized:
>
> * `backend/eval/runner.py` (extraction decision accuracy, Phase 2) and
>   `backend/eval/retrieval_runner.py` (Recall@K/Precision@K/MRR, comparing
>   graph-only/vector-only/hybrid retrieval, `eval/datasets/retrieval_v1.jsonl`,
>   Phase 3) are real, running, deterministic harnesses against
>   `FakeLLMClient`/`FakeEmbeddingClient` and real Neo4j/PostgreSQL.
> * Baselines A/B/C/D/E's final "→ LLM → Answer" step, citation metrics, and
>   agent tool-selection metrics (sections 3, 11, 12) — once "not yet
>   measurable" — are now measured by `backend/eval/agent_runner.py`
>   (Phase 5, `eval/datasets/agent_v1.jsonl`), including a real-LLM baseline
>   (`E_real_llm`) that lets the actual configured provider make every
>   tool-selection/citation decision, not a scripted one.
> * Document-sourced evaluation (`backend/eval/document_runner.py`, Phase 6,
>   `eval/datasets/document_v1.jsonl`) extends the same methodology to
>   documents, all 11 planned categories, plus an evaluation-only "raw chunk
>   vector search" baseline B.
> * **Phase 7** added persistent, resumable evaluation-run tracking
>   (`evaluation_runs`/`evaluation_task_results` in PostgreSQL, ADR 0015),
>   generalized the Phase 5 `ambiguous_entity` single-task finding into a
>   measured `retrieval_necessity` category (ADR 0016), and made real-LLM
>   runs checkpointed/resumable across rate-limited invocations (ADR 0017,
>   `eval/persistence.py`, `eval/report.py`). See `docs/PHASE7_PLAN.md`'s
>   implementation-status section for what was actually measured.
>
> What remains true from the original framework: LLM-as-judge answer scoring,
> the full 50→500+ question dataset growth plan, and human evaluation are
> still not implemented — see `docs/PHASE7_PLAN.md`'s "known limitations."

---

# 1. Purpose

The purpose of evaluation is to determine:

> **Does persistent graph-based memory actually improve AI agent performance?**

The project must not rely only on subjective demo quality.

We need measurable experiments.

---

# 2. Evaluation Questions

The evaluation framework should answer:

1. Does memory improve answer accuracy?
2. Does graph retrieval improve multi-hop questions?
3. Does hybrid retrieval outperform vector-only retrieval?
4. Does temporal memory improve historical questions?
5. Does entity resolution improve retrieval?
6. Does provenance improve citation accuracy?
7. Does memory improve agent tool selection?
8. What types of questions still fail?
9. What is the latency/cost tradeoff?

---

# 3. Baselines

Evaluate multiple approaches.

## Baseline A — LLM Only

```text
Question
 ↓
LLM
 ↓
Answer
```

No persistent memory.

---

## Baseline B — Vector RAG

```text
Question
 ↓
Vector Search
 ↓
LLM
 ↓
Answer
```

---

## Baseline C — Graph RAG

```text
Question
 ↓
Graph Search
 ↓
LLM
 ↓
Answer
```

---

## Baseline D — Hybrid

```text
Question
 ↓
Vector + Graph
 ↓
Reranking
 ↓
LLM
 ↓
Answer
```

---

## Baseline E — Hybrid + Temporal Memory

```text
Question
 ↓
Vector + Graph
 ↓
Temporal Filtering
 ↓
Reranking
 ↓
LLM
 ↓
Answer
```

---

# 4. Dataset

Create an evaluation dataset.

Example:

```json
{
  "id": "temporal_001",
  "question": "What OCR library was I using in March?",
  "expected_entities": ["EasyOCR"],
  "expected_relationships": [
    {
      "source": "Drawing Extractor",
      "relation": "uses",
      "target": "EasyOCR"
    }
  ]
}
```

---

# 5. Dataset Categories

Include:

## Simple factual questions

Example:

> What technologies does Project X use?

---

## Relationship questions

Example:

> How is X connected to Y?

---

## Multi-hop questions

Example:

> Which projects use technologies related to OCR?

---

## Temporal questions

Example:

> What was I using in March?

---

## Change questions

Example:

> What changed in the project architecture?

---

## Conflict questions

Example:

> Do I have contradictory information about this project?

---

## Provenance questions

Example:

> Where did I mention this?

---

## Cross-document questions

Example:

> What information appears across both documents?

---

# 6. Retrieval Metrics

Measure:

### Recall@K

Was the correct memory retrieved?

### Precision@K

How much of the retrieved context was relevant?

### MRR

Mean Reciprocal Rank.

### NDCG

Normalized Discounted Cumulative Gain.

Use the metrics appropriate to the retrieval experiment.

---

# 7. Graph Metrics

Measure:

### Entity extraction accuracy

Were correct entities extracted?

### Entity resolution accuracy

Were duplicate entities merged correctly?

### Relationship precision

How many relationships were correct?

### Relationship recall

How many expected relationships were found?

---

# 8. Temporal Metrics

Test:

* current-state accuracy
* historical-state accuracy
* temporal ordering
* validity intervals
* supersession handling

Example:

```text
March:
EasyOCR

June:
PaddleOCR
```

Question:

> What was being used in March?

Expected:

```text
EasyOCR
```

---

# 9. Conflict Resolution Metrics

Create test cases where:

```text
Old:
Project uses EasyOCR

New:
Project uses PaddleOCR
```

Measure whether the system correctly determines:

```text
EasyOCR:
historical

PaddleOCR:
current
```

---

# 10. Answer Metrics

Evaluate:

* factual correctness
* completeness
* relevance
* unsupported claims
* hallucination rate

LLM-as-judge may be used carefully, but critical evaluation should use deterministic/reference-based checks where possible.

---

# 11. Citation Metrics

Measure:

### Citation precision

Does the citation support the claim?

### Citation recall

Are important claims supported?

### Source correctness

Was the actual source used?

---

# 12. Agent Metrics

Measure:

* tool selection accuracy
* unnecessary tool calls
* successful tool execution
* number of tool calls
* task completion rate

Example:

Question:

> What did I decide about OCR last month?

Expected tool sequence may include:

```text
search_memory
get_memory_history
get_source
```

The exact sequence should not always be required if multiple valid strategies exist.

Evaluation should focus on whether the agent accomplishes the task efficiently and correctly.

---

# 13. Efficiency Metrics

Measure:

```text
Latency
Token usage
Number of tool calls
Number of retrieved memories
Database query time
LLM cost
```

Compare approaches.

Example:

```text
Vector-only:
low latency

Graph + vector:
higher latency

Graph + vector + reranking:
higher accuracy but potentially higher cost
```

The project should investigate the tradeoff.

---

# 14. Ablation Studies

Where practical, remove components and compare.

Example:

```text
Full System
- Temporal Memory
- Graph Retrieval
- Vector Retrieval
- Reranking
- Entity Resolution
```

This helps determine which components actually matter.

---

# 15. Example Experiment

Question:

> What OCR technologies have I used, and which one am I currently using?

Compare:

```text
LLM only
Vector RAG
Graph RAG
Hybrid
Hybrid + Temporal
```

Expected result:

The temporal hybrid system should ideally distinguish:

```text
Previously:
EasyOCR

Currently:
PaddleOCR
```

Do not assume it will.

Measure it.

---

# 16. Failure Analysis

Every failed evaluation should be categorized.

Categories:

```text
Entity Resolution Failure
Relationship Extraction Failure
Retrieval Failure
Temporal Reasoning Failure
Conflict Resolution Failure
Agent Failure
LLM Reasoning Failure
Citation Failure
```

For each important failure record:

```text
Question
Expected
Actual
Failure Category
Likely Cause
Potential Fix
```

---

# 17. Evaluation Dataset Growth

Start with a small manually curated dataset.

Example:

```text
50 questions
```

Then expand to:

```text
100
250
500+
```

The dataset should become increasingly difficult.

---

# 18. Synthetic Data

Synthetic conversations may be used to create controlled scenarios.

Example:

```text
January:
Project uses EasyOCR.

March:
Project uses EasyOCR.

May:
Project switches to PaddleOCR.

June:
Project adds OpenCV.
```

Generate questions about:

* current state
* historical state
* transitions
* relationships
* contradictions

Clearly label synthetic data.

---

# 19. Human Evaluation

Some dimensions are difficult to measure automatically.

Human evaluation may assess:

* usefulness
* correctness
* relevance
* explanation quality
* trustworthiness

Use a defined rubric.

Example:

```text
1 = incorrect
2 = mostly incorrect
3 = partially correct
4 = mostly correct
5 = fully correct
```

---

# 20. Benchmark Reporting

The README should eventually include actual measurements.

Example format:

```text
Method                     Accuracy
-----------------------------------
LLM Only                   measured
Vector RAG                 measured
Graph RAG                  measured
Hybrid                     measured
Hybrid + Temporal Memory  measured
```

Do not use illustrative values in the final report.

## Phase 4 measured results

Retrieval dataset expanded from 4 to 12 scenarios
(`eval/datasets/retrieval_v1.jsonl`), adding categories `1hop`, `2hop`, `3hop`,
`historical`, `duplicate`, `conflict`, `ambiguous_entity`, `distractor_heavy`
alongside the existing `graph_favored`/`vector_favored`/`temporal`. Run via
`uv run python eval/retrieval_runner.py` against real Neo4j/PostgreSQL with
`FakeEmbeddingClient`:

```text
Configuration   Mean Recall@K   Mean Precision@K   MRR
graph_only          91.7%            100.0%       0.917
vector_only         68.8%            100.0%       0.750
hybrid             100.0%            100.0%       1.000
```

`graph_only` misses `vector_favored` entirely (0%); `vector_only` misses
`1hop`/`2hop` entirely (0%) and only partially catches `3hop` (25%, since a
paraphrase overlapping the query's words can score above the similarity floor
by coincidence even without graph traversal) — each channel's unique
contribution is visible, and `hybrid` recovers full recall across every
category.

Dense-graph multi-hop benchmark (`eval/graph_scale_benchmark.py`),
deterministic synthetic graphs at 100 / 1,000 / 10,000 nodes with ~1% hub
entities (high in/out-degree): raw traversal latency (hops=2, from a hub)
scaled 43.9ms → 102.2ms → 564.2ms; full hybrid search latency, BEFORE fixing
a missing Neo4j index on `RELATES_TO.memory_id`, scaled 532ms → 451ms →
**39,523ms (mean), 105,280ms (max)** at 10,000 nodes — the actual bottleneck
was every `MemoryService.get()` call scanning the user's whole relationship
set (219,782 edges) to find one memory's own edges, not the multi-hop
traversal itself. After adding the index (`app/graph/schema.py`), the same
10,000-node case measured 532ms → 451ms → **582ms (mean), 1,565ms (max)** — no
fan-out cap was needed once the real cause was measured and fixed. These are
single-machine, single-Neo4j-Community-instance, synthetic-data measurements
— not a production-scale claim.

Semantic-duplicate threshold calibration (`eval/duplicate_threshold_calibration.py`):
a 12-pair dev set / 8-pair holdout set, similarity from the real
`FakeEmbeddingClient`. Policy: lowest FPR subject to recall ≥ 90% on dev,
selected threshold evaluated exactly once on holdout, never re-tuned.
Selected threshold=0.40 (dev: precision 66.7%, recall 100%, FPR 50%);
holdout: precision 57.1%, recall 100%, FPR 75%. This demonstrates the
methodology with real numbers on a small, hand-built dataset — the wide gap
between dev and holdout FPR, and the low absolute precision, reflect
`FakeEmbeddingClient`'s crude bag-of-words similarity (not discriminative
enough to cleanly separate this dataset's duplicate/non-duplicate pairs), not
a recommendation to change `SEMANTIC_DUPLICATE_THRESHOLD` (0.90) in
production — that needs a much larger labeled set and a real embedding model.

---

# 21. Evaluation Reproducibility

Every benchmark should record:

* model
* model version
* embedding model
* dataset version
* retrieval configuration
* prompt version
* date
* relevant hyperparameters

This makes experiments reproducible.

---

# 22. Evaluation Philosophy

The goal is not to prove that the proposed architecture is always better.

The goal is to discover:

> **When does structured persistent memory help, and when does it not?**

Negative results are valuable.

---

# 23. Final Evaluation Goal

At the end of the project, we should be able to make a defensible statement such as:

> "Hybrid temporal graph + vector memory improved performance on multi-hop and historical questions compared with vector-only retrieval, while introducing an observed latency/cost tradeoff."

Only make such a claim if experiments support it.
