# ADR 0036 — Scoped project tokens for machine clients (Phase 16)

**Status:** Accepted

## Decision
`sbp_` tokens: hashed at rest, shown once, bound to one project, scoped (`read|ingest|propose`), always expiring,
revocable, valid only on `/projects/{id}/…`, never on session routes, unable to mint tokens, rate-limited per owning
account. The MCP adapter holds one token and sends every call to `/projects/current/…`, so no tool can name a user or
project. Session bearer tokens were rejected for this role (full rights, 7-day expiry, re-login).
