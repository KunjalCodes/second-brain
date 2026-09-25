# ADR 0019 — Every repository module containing Cypher is covered by the isolation audit automatically

**Status:** Accepted (Phase 8)

## Context

Multi-user isolation rests on one rule: every Cypher statement filters on
`$user_id` (ARCHITECTURE.md section 15, ADR 0005). Since Phase 1,
`tests/unit/test_cypher_safety.py` has enforced it statically, but only for
the modules listed by hand in its `parametrize` decorators
(`memory_repository`, `entity_repository`). Phase 8 adds
`graph_repository.py`. Under the old audit, forgetting to add it to that list
would have left the highest-impact read surface unaudited, with no failing
test to say so.

## Decision

* The audit's module list (`AUDITED_MODULES`) is backed by a meta-test.
  `test_every_repository_module_with_cypher_is_audited` walks
  `app/repositories/` with `pkgutil`, imports every module, and fails if any
  module holding Cypher-shaped upper-case constants (`MATCH`/`CREATE`/`MERGE`)
  is missing from the list. It also fails if discovery finds no Cypher at
  all, so a broken walk cannot pass vacuously.
* `graph_repository` additionally has stricter statement-level checks:
  * every labelled node pattern carries `user_id: $user_id`;
  * every `RELATES_TO` predicate checks `r.user_id`;
  * every statement is bounded (`LIMIT $limit`, or driven by a caller-bounded
    `UNWIND` list);
  * no statement writes;
  * no statement uses a variable-length pattern;
  * window comparisons are UTC-normalised.

## Consequences

* Adding Cypher anywhere under `app/repositories/` without registering it
  for audit fails CI. This was verified by a mutation check: removing
  `graph_repository` from the list makes the meta-test fail.
* The audit remains static inspection. The actual query behaviour, including
  cross-user denial on every id-accepting parameter, is still proven against
  real Neo4j by the integration suite.
