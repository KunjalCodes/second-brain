# ADR 0005 — `X-User-Id` header as the Phase 1 identity placeholder

**Status:** **Superseded** by [ADR 0020](0020-postgresql-server-side-sessions.md) (Phase 9). The header survives only as the explicit development mode described in [ADR 0024](0024-development-identity-cannot-run-in-production.md).

## Context

`CLAUDE.md` section 26 requires authentication with per-user isolation and says never
to trust the frontend for it. Authentication is Phase 9. But `ARCHITECTURE.md`
section 15 requires `user_id` on every record from the start, and namespacing
retrofitted later would mean migrating every node and rewriting every query.

## Decision

Namespace everything by `user_id` **now**, and resolve the request's user from the
`X-User-Id` header, falling back to `DEV_USER_ID`.

Guard-rails, because a development placeholder that reaches production is a total
authorization bypass:

1. Resolution happens in exactly one function, `app.api.deps.get_current_user_id`.
   Phase 9 replaces that function and nothing else.
2. Startup **fails** if `APP_ENV=production` and `ALLOW_USER_ID_HEADER=true`.
3. Cross-user access returns **404, not 403**, so the API never confirms that
   another user's record exists.
4. Every Cypher statement filters on `$user_id`, and
   `tests/unit/test_cypher_safety.py` statically asserts it for every query constant,
   so a query that forgets the filter fails CI.
5. Multi-hop traversal additionally requires *every node and relationship along the
   path* to belong to the requesting user, so a query cannot walk out of the user's
   own subgraph.

## Consequences

* **This is not authentication and provides no security.** Any caller can claim any
  user id. It exists so that the isolation *mechanism* is built, exercised and tested
  from the first commit, leaving Phase 9 to supply real identity.
* The backend must not be exposed to untrusted callers before Phase 9.
* Isolation is enforced in application code. Once real auth exists, Neo4j has no
  row-level security equivalent, so the repository chokepoint plus the static audit
  remain the enforcement mechanism — which is why both are tested.
