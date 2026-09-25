# Frontend

**Not implemented yet.** The Next.js application is Phase 3 work; Phase 1 is the
graph-memory backend only. This directory exists so the deployment topology and the
backend contract are fixed before any UI is written.

## Deployment target

Vercel, with this directory as the project root. Vercel builds Next.js natively --
no Dockerfile, and nothing here needs to change for a preview or production deploy.

## Backend contract

The backend is reached through **one environment variable**:

```
NEXT_PUBLIC_API_URL
```

`http://localhost:8000` in development, the deployed backend URL in production.
Copy `.env.example` to `.env.local` to set it locally, and set it in Vercel's
project settings for deployments.

Two rules for when the app is built:

1. **Never hard-code `localhost:8000`.** Read `NEXT_PUBLIC_API_URL` in exactly one
   module (`lib/api.ts`) and have every request go through it, so switching
   environments is a config change rather than a code change.
2. **Authenticate with the session cookie (Phase 9).** Call `GET /auth/me`
   on load (`401` → show login), `POST /auth/login` to sign in, and send every
   request with `credentials: "include"`. The session token lives only in an
   `HttpOnly` cookie -- never read, store or send it from JavaScript, and never
   put it in `localStorage`. Serve the API **same-origin through a Next.js
   proxy** (ADR 0022) so the cookie is first-party. `X-User-Id` is a backend
   development mode only and is refused in production. See `docs/API.md`
   section 3a.

With the same-origin proxy no CORS is needed. If a preview deployment must
call the backend directly, enable `CORS_ALLOW_VERCEL_PREVIEWS` together with
`CORS_VERCEL_PROJECT=<this project>` -- the backend no longer admits every
`*.vercel.app` site. The frontend origin must still be listed in `FRONTEND_URL`
or `CORS_ORIGINS`: the proxy forwards the browser's `Origin`, which the
backend's CSRF check verifies.

The full endpoint list and response shapes are in [`../docs/API.md`](../docs/API.md),
and the live OpenAPI schema is served at `<backend>/docs`.
