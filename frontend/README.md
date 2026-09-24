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
2. **Send the user identity header.** Until authentication lands in Phase 8 the
   backend reads `X-User-Id`. That is a development placeholder, not auth -- see
   `docs/API.md`.

The backend already allows Vercel preview origins (`https://*.vercel.app`) via
`CORS_ALLOW_VERCEL_PREVIEWS`, so preview deployments work without extra
configuration. Add a custom production domain to `CORS_ORIGINS` on the backend.

The full endpoint list and response shapes are in [`../docs/API.md`](../docs/API.md),
and the live OpenAPI schema is served at `<backend>/docs`.
