# ADR 0034 — A project is an isolated memory space (Phase 16)

**Status:** Accepted

## Context
Coding agents work on several projects. Memory from project A must never reach project B, and a user's personal memory
must stay separate. Isolation today is one key (`user_id`) on every SQL row and Cypher statement, audited.

## Decision
- A project owns a dedicated `users` row (`space_id`). Requests resolve to that id only after an ownership check
  (account + `X-Memory-Space`) or a project-token check. Every existing repository is unchanged.
- Project-scoped SQL tables (`agent_events`, `agent_sessions`, `git_commits`) are keyed by the space id too.
- Rejected: a `project_id` property on every row (touches every statement, one missed filter leaks).

## Consequences
Isolation by construction; entity resolution is per project. Personal and project memory cannot be searched together
(accepted; not worked around). Deleting a project is not implemented (archive status only).
