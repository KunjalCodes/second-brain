# ADR 0003 — One predicate with a validity window, not `previously_used` / `currently_uses`

**Status:** Accepted (Phase 1) — **supersedes the model shown in PRODUCT.md section 9**

## Context

`PRODUCT.md` section 9 and `README.md` illustrate memory evolution as a change of
predicate:

```
Project → previously_used → EasyOCR
Project → currently_uses  → PaddleOCR
```

`CLAUDE.md` section 15 and `ARCHITECTURE.md` section 13 instead model it as one
predicate carrying `valid_from` / `valid_until` / `status`. These conflict.

## Decision

Store **one predicate** (`uses`) with a validity window. Derive
`currently_uses` / `previously_used` as a **view** at query time
(`GET /entities/{id}/history`).

Additionally, relationships use a generic `:RELATES_TO` type with the predicate as a
**property**, rather than a dynamically-typed edge such as `:USES`.

## Rationale

Rewriting the predicate destroys the information needed to answer "what was I using
in March?". With `previously_used` there is no March — only a vague "before". The
validity window keeps the date, so the historical question is answerable, which is
the whole point of the temporal model. `previously_used` is a *presentation* of a
closed window, so it belongs in the view layer.

For the generic edge type: typing edges dynamically (`:USES`) would require
interpolating the predicate into Cypher, because labels and relationship types
cannot be parameterised. That is an injection surface on the hottest write path, in
exchange for a traversal speed-up that is irrelevant at this scale. A property is
fully parameterised and indexed (`relates_to_predicate`).

## Consequences

* "What was I using in March?" works, and is tested at unit and integration level.
* The API returns `current` and `historical` buckets from one predicate. An edge
  whose window starts *after* the as-of instant appears in neither: it had not
  happened yet.
* Traversal patterns are uniform (`-[r:RELATES_TO]-`), so N-hop queries need no
  knowledge of the predicate vocabulary.
* Predicates are validated by shape (`^[a-z][a-z0-9_]{0,62}[a-z0-9]$`) rather than
  against a closed enum, so extraction in later phases can produce specific
  predicates without a migration.
