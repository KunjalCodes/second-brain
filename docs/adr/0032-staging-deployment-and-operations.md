# ADR 0032 — Private staging: offline AI, no pretended client IPs, transient bytes, proven restores

**Status:** Accepted (Phase 11; decisions D1–D12)

## Context

The first deployment target is a private staging environment on free tiers
(Vercel Hobby, Render Free, Neon Free, AuraDB Free). It must be safe without
real AI quota, must not claim guarantees the platforms do not give, and must
fail at boot rather than run misconfigured.

## Decision

- **Refuse unsafe production-like configuration at startup**
  (`_check_deployment_requirements`). That covers missing databases, a short
  internal token, relative storage, debug/SQL echo, non-https origins, Vercel
  previews, and unimplemented or unkeyed LLM providers. Fake AI needs an
  explicit `ALLOW_FAKE_AI=true`.
- **Offline AI by default (D7).** `LLM_PROVIDER=fake` in a running app is the
  `OfflineLLMClient`: zero extraction candidates, and an agent answer stating
  that AI is off. Before Phase 11 it returned 500 (measured). Real-LLM
  evaluation requires `--real-llm` / `EVAL_REAL_LLM=1`.
- **No pretended per-IP identity (D5).** Behind the Next.js rewrite and
  Render's proxy the backend cannot see the client IP, and the Next rewrite
  forwards a client-supplied `X-Forwarded-For` verbatim. `RATE_LIMIT_PER_IP=false`
  replaces per-IP buckets with explicit global ceilings; per-email and per-user
  limits stay. The proxy is never added to `TRUSTED_PROXY_IPS`.
- **Transient document bytes (D3).** Bytes live on the ephemeral disk only
  until chunks exist, then they are deleted. Missing bytes produce an explicit
  `document_missing`. Object storage is deferred to production.
- **Upload path measured, not assumed (D4).** The Next.js proxy truncated bodies
  at 10 MB by default, and a 20 MiB upload failed after 30 s (measured locally).
  `proxyClientMaxBodySize` now equals the backend limit. The Vercel path must
  still be measured at deploy time.
- **Hidden API docs (D8)**, JSON logs, HSTS, `/internal/metrics` behind the
  operator token (D6: JSON, no dependency).
- **Backups (D12).** `scripts/backup.py` produces an encrypted PostgreSQL dump,
  a typed Neo4j export and a manifest. Nightly `ops.yml` artifacts are kept 7
  days. A backup counts only after `verify` passes on a restore; this was done
  locally.

## Consequences

- Staging is **not production**: no SLA, no monitoring service, short native
  backup windows, AuraDB deletion 30 days after it pauses.
- Deployment itself needs the owner's accounts (docs/DEPLOYMENT.md). Every
  step has a pass criterion.
