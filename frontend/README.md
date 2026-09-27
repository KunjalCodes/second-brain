# Memory Explorer (frontend)

The Phase 10 browser UI for inspecting what the AI remembers: its memories,
entities, graph, sources, documents and review queue.

It is a **presentation layer**. The backend decides identity, authorization,
temporal validity (what is true now / was true then), entity merges, graph
budgets, provenance and citations. This app displays those answers and
never recomputes them. See [ADR 0025](../docs/adr/0025-memory-explorer-is-a-client-rendered-app-behind-a-same-origin-proxy.md)–[0030](../docs/adr/0030-free-tier-demo-topology-and-its-limits.md).

## Stack

| Area | Choice |
|---|---|
| Framework | Next.js 16 (App Router), React 19 |
| Language and styling | TypeScript (strict), Tailwind CSS 4 |
| Server state | TanStack Query 5 |
| Graph | Cytoscape.js |
| API types | Generated from the backend's OpenAPI schema (`openapi-typescript`) |
| Tests | Vitest + Testing Library (unit and component); Playwright + axe (end to end) |

No other runtime dependencies: no state library, HTTP client, auth library,
analytics or UI kit. Accessible dialogs use the native `<dialog>`.

## Run locally

Prerequisites:

* Node ≥ 20.9;
* the backend running (see the root README) with `AUTH_MODE=session` and
  `FRONTEND_URL=http://localhost:3000`.

```bash
cd frontend
cp .env.example .env.local      # BACKEND_ORIGIN=http://localhost:8000
npm install
npm run dev                     # http://localhost:3000
```

Sign in with an account created by the operator CLI (run from `backend/`):

```bash
uv run python ../scripts/manage_users.py create --email you@example.com
```

## How it talks to the backend

* The browser calls only this app's own origin. `/api/*` is rewritten by
  the Next.js server to `BACKEND_ORIGIN`, so the session cookie is
  first-party. `BACKEND_ORIGIN` is server-only and is **fixed at build
  time**: rebuild after changing it.
* `lib/api/client.ts` is the only module that performs HTTP. It uses
  `credentials: "include"` and `cache: "no-store"`, and never sets an
  `Authorization` header.
* The session is the HttpOnly `sb_session` cookie. JavaScript never sees a
  token, and nothing is written to web storage.
* After a backend API change:
  1. run `npm run gen:api` to regenerate `lib/api/schema.d.ts`;
  2. commit the result. CI fails if they differ.

## What each page shows

| Page | Backend | Notes |
|---|---|---|
| Overview | `/memories/search`, review lists, `/documents` | Counts link to their pages |
| Memories | `GET /memories/search` | Filters in the URL; search text never in the URL; honours the time lens |
| Memory detail | `/memories/{id}`, `/provenance`, `/graph/memories/{id}/timeline` | Valid time vs recorded time; lifecycle; sources; retract |
| Entities | `/entities/search`, `/entities/{id}`, `/history?as_of=` | Merge redirects followed; memory list marked "Not time-filtered" |
| Graph | The 4 `/graph/*` endpoints | Read-only; truncation always shown; list view; canvas cap of 600 elements |
| Search | `POST /search` | Relevance is relative, not truth; explanation of each channel |
| Documents | `/documents` (upload, list, chunks) | TXT / Markdown / text-layer PDF only; bounded polling |
| Review | `/review/memories`, `/review/entities`, `/review/history` | Consequence stated before confirming; `if_version`; 409 → reload |
| Ask (optional M9) | `POST /conversations/{id}/agent-turn` | Plain-text answers; citations only from `cited_memory_ids` |
| Settings | `/auth/me`, `/auth/password`, `/auth/logout-all` | |

The **time lens** ("Now / As of" in the header) sends `as_of` to endpoints
that support it. Pages whose endpoint does not support it say "Not
time-filtered".

## Checks

```bash
npm run typecheck
npm run lint          # includes react/no-danger as an error
npm test              # unit + component tests (no backend needed)
npm run build
```

## End-to-end tests

The suite runs against a real local stack on a **test** database: a backend
in session mode plus this app. It seeds two throwaway accounts through the
backend's own services (`scripts/e2e_seed.py`) and deletes them, including
uploaded files, afterwards.

```bash
# 1. backend (from backend/), pointing at a TEST database
APP_ENV=development AUTH_MODE=session ALLOW_USER_ID_HEADER=false \
FRONTEND_URL=http://localhost:3100 DATABASE_URL=<test db> \
uv run uvicorn app.main:app --port 8010

# 2. this app (production build, which enforces the production CSP)
BACKEND_ORIGIN=http://localhost:8010 npm run build
BACKEND_ORIGIN=http://localhost:8010 npx next start --port 3100

# 3. tests, from frontend/, with the same DATABASE_URL / NEO4J_* as the
#    backend, because the seed script runs through `uv run`
npx playwright install chromium-headless-shell   # once
npm run test:e2e
```

What the suite covers:

* auth: redirect-after-login, cookie flags, empty `document.cookie` and web
  storage, `Cache-Control: no-store`, CSRF 403, session loss, logout;
* memories: search text kept out of the URL, superseded history, inert
  rendering of an HTML payload, the time lens;
* ranked search, graph neighbourhood and evidence, document upload;
* review decisions (conflict resolution, merge then undo);
* cross-user isolation;
* axe checks on every main page, keyboard skip link, phone-width layout.

Performance measurements, which are not pass/fail:

```bash
E2E_PERF=1 E2E_HUB_SIZE=200 E2E_PERF_GRAPH=1 npx playwright test e2e/perf.spec.ts   # → test-results/perf.json
node scripts/bench-graph-layout.mjs                                                 # headless layout cost
NEXT_SOURCEMAPS=1 BACKEND_ORIGIN=http://localhost:8000 npx next build               # then:
node scripts/bundle-attribution.mjs <chunk files…>                                  # bytes per source module
```

Page-weight figures are split at the `load` event:
- `critical_transferred_bytes`: JS needed to render the page;
- `prefetch_transferred_bytes`: Next.js prefetching the routes linked from the
  navigation, at idle time.

Phase 10's 190.9 KB figure mixed the two. Measured on the same metric (Phase 11):

| Build | Critical JS | Prefetch |
|---|---|---|
| `a9da2db` | 160.1 KB | 33.7 KB |
| After Phase 11 | 168.5 KB | 33.8 KB |

The +8.4 KB is mostly the error boundaries (~4.8 KB). Both builds are within the
180 KB budget. `E2E_PERF_GRAPH=1` seeds hubs for the graph attribution test
(backend / network / layout / paint at ~50 / ~150 / ~300 nodes).

**Uploads through the proxy.** Next.js buffers proxied bodies.
`experimental.proxyClientMaxBodySize` in `next.config.ts` must stay equal to the
backend upload limit (`MAX_DOCUMENT_SIZE_BYTES` + 64 KiB). Its default (10 MB)
silently truncated 20 MiB uploads (measured).

`E2E_KEEP_DATA=1` keeps the seeded data after a run. The next run removes it
first.

## Deployment (optional demo, not production)

See [ADR 0030](../docs/adr/0030-free-tier-demo-topology-and-its-limits.md).
In short: Vercel Hobby (non-commercial) for this app, with `BACKEND_ORIGIN`
set in the **build** environment and registration left off. On the backend
side:

* set `FRONTEND_URL` to the Vercel URL;
* do **not** add the proxy to `TRUSTED_PROXY_IPS`.

Free-tier limits to expect:

* cold starts ("Waking the server…");
* ephemeral document storage;
* AuraDB pausing after 3 days without writes.

Nothing in this repository has been deployed, and the system is **not**
production-ready.
