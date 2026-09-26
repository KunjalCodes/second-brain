# ADR 0025 — The Memory Explorer is a client-rendered Next.js app behind a same-origin rewrite proxy

**Status:** Accepted (Phase 10)

## Context

Phase 10 adds the first browser UI. The backend already owns everything
that makes a memory correct: identity, authorization, temporal validity,
current/historical semantics, merge resolution, graph budgets, provenance
and citation validation (Phases 1–9). The risks are a UI that re-implements
any of that, and a UI that calls the backend cross-site, where browsers
block third-party cookies (ADR 0022).

## Decision

* **Next.js App Router, TypeScript (strict), Tailwind CSS.** Every page
  that shows user data is a client component. User data is fetched in the
  browser after `GET /auth/me` succeeds. Nothing user-specific is
  server-rendered, so nothing user-specific can be cached or prerendered by
  the framework.
* **Same-origin proxy.** `next.config.ts` rewrites `/api/:path*` to
  `${BACKEND_ORIGIN}/:path*`. `BACKEND_ORIGIN` is a server-only variable
  (no `NEXT_PUBLIC_` prefix). Measured: the rewrite destination is written
  into `.next/routes-manifest.json` **at build time**, so it must be set in
  the build environment, and it does not appear in any client bundle.
* **One fetch point.** `lib/api/client.ts` is the only module that performs
  HTTP. Every request uses `credentials: "include"` and
  `cache: "no-store"`. Non-2xx becomes a typed `ApiError` carrying status,
  code, request id and `Retry-After`. Domain states such as truncation
  flags, evidence status and provenance nulls pass through unchanged.
* **Server state in TanStack Query.** No other state library.
  * Filters live in the URL.
  * Free text (memory search, entity search, ranked search) lives in
    component state only, never in the URL (Q7).
  * The time lens lives in React context.
  * Retry policy: never retry 4xx; retry a 429 once after `Retry-After`;
    retry network errors and 5xx at most twice. Mutations never retry.
* **Generated types.** `lib/api/schema.d.ts` is generated from the FastAPI
  app object (`npm run gen:api`, no server needed).
  * CI regenerates it and fails on a diff.
  * A unit test checks that every path and method the UI calls exists in
    that schema, and was mutation-tested by renaming one path.

## Consequences

* Business rules stay on the server. The frontend never computes whether a
  memory is current; it shows the server's `temporal_state`.
* A direct link to a page shows a loading state for one round trip
  (`/auth/me`) before content appears. With a sleeping free-tier backend
  this becomes "Waking the server…" (ADR 0030).
* Proxy header behaviour (M0 spike, verified with curl and Chromium):
  * the rewrite forwards `Origin` and `Cookie` unchanged;
  * it rewrites `Host` and adds `x-forwarded-host`;
  * it passes a client-supplied `X-Forwarded-For` **verbatim**. See
    ADR 0026 for why the proxy therefore must not be listed in
    `TRUSTED_PROXY_IPS`.
* Changing the backend URL requires a rebuild, not just a restart.
