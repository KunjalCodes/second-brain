# ADR 0021 — Authentication identity is separate from memory ownership identity

**Status:** Accepted (Phase 9)

## Context

Every PostgreSQL row, every Neo4j node and edge, and every document-storage
directory is owned by `users.id`, a free-form string of at most 128
characters. Existing data uses ids such as `dev-user`, `demo-user` and
synthetic test ids; Neo4j-only ids like `demo-user` have no `users` row at
all. Rewriting ownership ids would mean migrating every node, edge and row.

## Decision

* **Memory ownership identity stays `users.id`**, unchanged in type, meaning
  and value. There is no Neo4j change and no data migration.
* **Authentication identity is separate:**
  * `user_accounts` holds exactly one account per `users.id`: the normalised
    email, the scrypt hash, `active | disabled`, and timestamps;
  * `auth_sessions` holds sessions.
  * `users` still holds no credential.
* **The server always chooses the ownership id:**
  * a UUID4 at registration;
  * or, through the operator CLI (`scripts/manage_users.py create --user-id`),
    an existing id such as `dev-user` / `demo-user`.

  The CLI refuses an id with no existing data, and refuses an id that already
  has an account, so it cannot reassign ownership of anyone's memories.
* **No client-controlled input can set ownership.** That covers request
  bodies, query and path parameters, headers, agent tool arguments and LLM
  outputs. Structural tests enumerate every route's inputs, every tool
  argument schema and every LLM output schema, and fail on any ownership
  field.
* Before Phase 9, any unseen `X-User-Id` silently created a user
  (`ensure_exists`). In session mode, users exist only through registration
  or the CLI. `ensure_exists` stays as a harmless idempotent no-op.

## Consequences

* Email is not the ownership key, so a future email change (not offered in
  Phase 9) never touches memory data.
* Legacy local data stays reachable by attaching a login to its id.
* OAuth could later attach to `users.external_auth_id` without touching
  ownership.
