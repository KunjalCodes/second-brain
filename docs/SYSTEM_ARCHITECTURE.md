# System architecture (Phase 17, M26)

Diagrams of what is implemented. Each one matches the flow descriptions in
`SYSTEM_GUIDE.md` and the audit in `PHASE17_REPORT.md`.

## 1. High level

```mermaid
flowchart LR
  U[User in browser] --> FE[Next.js frontend]
  FE -->|cookie session + optional X-Memory-Space| API[FastAPI engine]
  CA[External coding agent] -->|stdio MCP| MCP[MCP adapter<br/>17 tools]
  MCP -->|Bearer sbp_ token<br/>/projects/current/...| API
  COL[Git collector script] -->|Bearer sbp_ token| API
  API --> PG[(PostgreSQL + pgvector)]
  API --> NEO[(Neo4j)]
  API --> OBJ[(Object storage)]
  API -.->|optional, configurable provider| LLM[LLM / embeddings]
  SW[Sweeper] --> PG
  SW --> NEO
```

The engine never needs an LLM for project memory; the LLM is used for conversation/document
extraction, the internal agent and embeddings only.

## 2. Conversation flow

```mermaid
sequenceDiagram
  participant U as User
  participant API
  participant PG as PostgreSQL
  participant X as Extraction (LLM + governance)
  participant N as Neo4j
  U->>API: POST /conversations/{id}/messages
  API->>PG: store message (committed)
  API->>PG: enqueue extraction job
  X->>PG: claim job
  X->>X: candidates -> worthiness -> entity resolution -> duplicate/conflict/temporal
  X->>N: write Memory, Entities, Source, edges
  X->>PG: embedding row, review candidates, audit
  U->>API: GET /memories/search or POST /search
  API->>N: graph channel
  API->>PG: vector + keyword channel
```

## 3. Document flow

```mermaid
flowchart TD
  UP[POST /documents] --> ST[(Object storage)]
  UP --> D[documents row + ingestion job]
  D --> P[parse + clean + chunk]
  P --> C[document_chunks]
  C --> E[per-chunk extraction]
  E --> G[same governance as conversations]
  G --> M[Memory + Source document/chunk in Neo4j]
```

## 4. Coding-agent flow (REST or MCP)

```mermaid
sequenceDiagram
  participant A as Coding agent
  participant M as MCP adapter
  participant API
  participant PG
  participant N as Neo4j
  A->>M: get_resume_context
  M->>API: GET /projects/current/resume
  API->>PG: sessions/events/commits (explicit evidence)
  API-->>A: unfinished work, with sources
  A->>M: record_agent_event
  M->>API: POST /projects/current/events
  API->>PG: raw event row (always first)
  API->>N: deterministic projection (no LLM)
  API->>PG: mark projected (memory id = uuid5(space,event))
  A->>M: get_decisions / get_file_history / ...
  M->>API: GET /projects/current/...
```

## 5. MCP flow

```mermaid
flowchart LR
  T[tool call] --> V[argument validation<br/>+ path-segment guard]
  V -->|refused| E[ToolError]
  V --> R[EngineClient.request<br/>route must stay under /projects/current]
  R --> H[HTTP + Bearer sbp_]
  H --> API
  API --> S[size cap + error mapping]
  S --> T2[structured result with notice]
```

## 6. Git ingestion

```mermaid
flowchart TD
  C[scripts/git_collect.py<br/>read-only git log] --> P1[POST /git/commits]
  C --> P2[POST /git/structure]
  P1 --> R[git_commits row, idempotent on sha]
  R --> L{lock row<br/>FOR UPDATE SKIP LOCKED}
  L --> D[deterministic drafts -> Memory ids]
  D --> A{memory exists?}
  A -->|yes: adopt| OK[projected]
  A -->|no| W[create in Neo4j]
  W -->|ConflictError from a race| A
  W --> OK
  P2 --> S[structure facts, supersede stale]
```

## 7. Governance

```mermaid
flowchart TD
  IN[candidate from conversation/document] --> W[worthiness]
  W --> ER[entity resolution<br/>normalise, alias, similarity]
  ER --> DUP[duplicate check]
  DUP --> CON[conflict check]
  CON --> TMP[temporal classification]
  TMP --> ACT{outcome}
  ACT --> NEW[new]
  ACT --> UPD[update / merge]
  ACT --> SUP[supersede]
  ACT --> CF[conflicting -> review]
  PE[project event / Git fact] --> SC[apply_structured_candidate<br/>duplicates + conflicts + temporal<br/>no worthiness, no LLM]
  SC --> ACT
```

Entity merges that are not certain go to the review queue instead of auto-merging; nothing is
deleted silently (`merged`, `superseded`, `invalid` are statuses).

## 8. Retrieval

```mermaid
flowchart LR
  Q[query + temporal scope] --> V[vector]
  Q --> G[graph]
  Q --> K[keyword]
  V --> MG[merge by memory id]
  G --> MG
  K --> MG
  MG --> RR[deterministic re-rank<br/>+ retrieved_via labels]
  RR --> CTX[bounded context]
```

## 9. Agent continuity

```mermaid
flowchart TD
  S1[Session 1 events + commits] --> PG[(raw events + git_commits)]
  PG --> RES[resume builder]
  RES -->|explicit evidence only| U[unfinished work:<br/>task_blocked, failed attempt,<br/>feature without completion]
  RES -->|no evidence| NONE[nothing to resume]
  U --> S2["Session 2, a new process"]
```

Unfinished work and failed attempts are never inferred from silence or from graph proximity.

## 10. PostgreSQL vs Neo4j

```mermaid
flowchart LR
  subgraph PG[PostgreSQL: the system of record for process]
    A1[users, auth_sessions]
    A2[conversations, messages]
    A3[documents, chunks, ingestion jobs]
    A4[extraction jobs/candidates]
    A5[review candidates + audit log]
    A6[memory_embeddings]
    A7[projects, project_tokens]
    A8[agent_sessions, agent_events, git_commits]
  end
  subgraph N[Neo4j: the system of record for knowledge]
    B1[Memory nodes: status, valid_from/until]
    B2[Entity nodes]
    B3[Source nodes]
    B4[MENTIONS, DERIVED_FROM, RELATES_TO,<br/>SUPERSEDES, CONFLICTS_WITH, DUPLICATE_OF, MERGED_INTO]
  end
  PG <-->|memory id / user_id links;<br/>no distributed transaction| N
```

There is no distributed transaction: writes are ordered so a crash leaves a recoverable state
(raw event row first; deterministic memory ids; adopt-existing on retry). Known consistency gaps
are listed in `PHASE17_REPORT.md` §17.
