# ADR 0022 — Browser credentials are HttpOnly SameSite=Lax cookies with an Origin check; the frontend is same-site

**Status:** Accepted (Phase 9)

## Context

A browser UI (Phase 10) needs a credential that JavaScript cannot read, since
an XSS bug could otherwise exfiltrate it. Cookies are ambient credentials,
which makes CSRF a real threat. Separately, the pre-Phase-9 CORS setting
combined `allow_credentials=True` with a regex admitting **every**
`*.vercel.app` origin. Once credentials exist, any stranger's Vercel
deployment could read authenticated responses. The frontend example
configuration also pointed at a separate backend host (a cross-site
topology), where browsers increasingly block third-party cookies.

## Decision

* **Cookie `sb_session`:** `HttpOnly`, `SameSite=Lax`, `Path=/`, host-only,
  `Max-Age` = the absolute lifetime. It is `Secure` everywhere except
  development/test, and a production-like environment with `Secure` off
  refuses to start.
* **CSRF boundary.** A *cookie-authenticated* `POST/PUT/PATCH/DELETE` must
  carry an `Origin` that is one of:
  * an explicitly configured origin (`FRONTEND_URL`, `CORS_ORIGINS`);
  * the configured Vercel project's preview;
  * this API's own origin.

  Anything else, including a missing `Origin`, gets `403 origin_not_allowed`.
  `SameSite=Lax` is the second layer. Bearer-authenticated requests are
  exempt, since they are not ambient. `/auth/login` and `/auth/register`
  refuse a disallowed browser `Origin` (login CSRF). No CSRF-token framework
  is needed.
* **CORS.** Credentials are allowed only for explicit origins. The wildcard
  preview regex is gone. `CORS_ALLOW_VERCEL_PREVIEWS` is now off by default
  and requires `CORS_VERCEL_PROJECT=<slug>`, which admits only
  `https://<slug>(-…)?.vercel.app`. Allowed request headers are narrowed to
  `Authorization`, `Content-Type` and `X-Request-ID` (plus `X-User-Id` in
  dev-header mode only).
* **Topology (approved Q3).** Phase 10 serves the API through a **Next.js
  same-origin proxy**, so the cookie is first-party. Behind that proxy,
  `TRUSTED_PROXY_IPS` must name the proxy for per-IP rate limits (ADR 0023).

## Consequences

* The frontend contract:
  * never store a token in `localStorage` / `sessionStorage`;
  * use `fetch(..., {credentials: "include"})`;
  * treat `401` as "go to login";
  * `GET /auth/me` bootstraps the session.
* XSS can still act *within* the page while it is open. The Phase 8 rule
  "render all content as text" remains what prevents that.
* A cross-site deployment would break cookie auth in some browsers. That is
  deliberately not supported.
