# ADR 0008 — Heuristic-gated, asynchronous, windowed extraction with a bounded sweep

**Status:** Accepted (Phase 2)

## Context

Extraction requires an LLM call. When it runs determines the system's latency, its
running cost, the quality of what it learns, and how reliably it learns at all. The
naive choice — call the model on every user message, inline — is the worst option on
three of those four axes.

## Decision

```text
User message
     ↓
Store message in PostgreSQL          (synchronous, no LLM)
     ↓
Cheap deterministic heuristic gate
     ↓
Is persistent memory potentially present?
     │
   NO ──────────────────────► continue normally (status = skipped, reason recorded)
     │
    YES
     ↓
Add or extend the extraction window  (debounced)
     ↓
Async extraction job                 (after the response is returned)
     ↓
LLM candidate extraction
     ↓
Memory decision engine               (deterministic)
     ↓
Neo4j
```

Plus a **bounded, idempotent sweep** that reconciles what the live path missed.

## Alternatives considered

| Strategy | Latency | Cost | Quality | Why not |
|---|---|---|---|---|
| Every message, inline | **+1–3 s every turn** | Highest | Good | Ruins the chat experience for a background enrichment |
| Every N turns | Batched | Lower | A fact can straddle the batch boundary | Arbitrary boundary, no principle behind N |
| Conversation end | None | Lowest | Best context | "Conversation end" is undefined in a chat UI, and memory is unavailable *during* the conversation — which defeats the premise |
| Heuristic-gated | None | **Much lower** | Good if the gate stays conservative | **Chosen** |
| Asynchronous | **None** | Unchanged | Unchanged | **Chosen** (combined with the above) |

## Rationale against the five required criteria

**Latency.** The user never waits for the LLM. The message is committed and the
response returned before extraction is enqueued, so extraction contributes exactly
zero to perceived response time. This is the single largest user-visible benefit and
it is why asynchronous is non-negotiable rather than an optimisation.

**LLM cost.** Two independent reductions. The gate suppresses calls for messages that
cannot plausibly contain durable knowledge — questions, pleasantries, very short
messages, assistant turns. Debouncing then collapses a rapid burst of messages into
one job over an extended window rather than one job per message. Actual spend is
measured, not estimated: `extraction_jobs` records input/output tokens, latency and
model per run, which is what makes "cost per conversation" in `EVALUATION.md`
section 13 a measurement.

**Extraction quality.** Extraction runs over a **window** of recent messages, not a
single message. This is a correctness requirement, not a refinement: *"We switched
from EasyOCR to PaddleOCR"* contains no project name, so single-message extraction
cannot resolve what changed and Scenario C fails outright. The window supplies the
referent.

**Reliability.** In-process background work is lost when the process restarts, and a
heuristic gate will sometimes be wrong. Neither may permanently cost the user a
memory, so the sweep reconciles three distinct cases:

1. jobs `failed` with attempts remaining and `next_attempt_at` elapsed → retry
2. jobs `in_progress` whose lease expired (the worker died mid-run) → reclaim
3. messages the gate marked `skipped` → re-evaluate under the *window* gate, so a
   message that looked inert alone but is meaningful in context gets a second chance

The sweep is **bounded** — a row limit per run, a maximum attempt count, and a
lookback window — so it can never turn into an unbounded re-processing storm. It is
**idempotent** by the same key the live path uses, so sweeping costs nothing when
there is nothing to do.

**Duplicate prevention.** Three layers, each catching what the previous cannot:
the job key `UNIQUE (target_message_id, extractor_version)` prevents a second job for
the same work; `extraction_candidates` records which candidates were already applied,
so a mid-run crash retries only the remainder; and the decision engine's `DUPLICATE`
classification catches the general case, including the same fact arriving from a
different message. Notably, a re-run of an already-applied extraction produces
`DUPLICATE` decisions rather than duplicate memories — idempotency falls out of the
decision engine rather than being bolted on.

## Consequences

* The gate is a **deterministic, unit-tested component**, not a prompt. Its decisions
  and reasons are persisted (`messages.extraction_gate_reason`), so gate precision is
  itself measurable and tunable against the evaluation dataset.
* Memory becomes available seconds after a message rather than instantly. Acceptable:
  the product promise is continuity across conversations, not within a single turn.
* **No queue infrastructure.** `BackgroundTasks` plus the sweep is sufficient for
  Phase 2; Redis or Celery would be speculative. This is revisited only when a
  measured latency or reliability problem appears (`ARCHITECTURE.md` section 17).
* The sweep needs a periodic trigger. It is implemented as a plain function exposed
  through a token-authenticated internal endpoint, so the managed host's own cron can
  call it — no new infrastructure, and it stays observable.
* **Deployment constraint:** in-process background work requires a host that keeps
  running after the response is sent. Fine on Railway and Render; on Cloud Run it
  needs CPU-always-allocated or `min-instances >= 1`, otherwise extraction is
  throttled mid-flight. This is the first Phase 2 decision that makes host choice
  non-arbitrary.
