# ADR 0026 — The browser auth boundary is the session cookie alone

**Status:** Accepted (Phase 10). Amends the proxy note in ADR 0022.

## Context

Phase 9 issues an opaque session token as an HttpOnly `sb_session` cookie
(or in the response body, for scripts). The browser UI must use it without
ever giving JavaScript access to a credential, and must handle expiry,
revocation and redirects safely.

## Decision

* **The cookie is the only browser credential.**
  * The login form never requests `token_in_body`.
  * Nothing is written to `localStorage`, `sessionStorage` or IndexedDB.
  * No `Authorization` header is ever set.
  * `document.cookie` stays empty.

  E2E tests assert all four in a real Chromium after login.
* **`GET /auth/me` is the session check.** The `(app)` layout renders a
  gate: loading, then signed-in content, otherwise a redirect. There is no
  Next.js middleware. The page shell contains no user data, and every data
  request is authorized by the backend, so a client-side gate is only UX,
  never a security boundary.
* **The global 401 policy.** Any 401 from any request clears the whole
  query cache (per-user data) and replaces the route with
  `/login?next=<current>`, adding `&reason=expired` only if a session had
  existed.
  * `invalid_credentials` (wrong password on login or password change) is
    **not** a sign-out event. A unit test pins this, and it fixes a bug
    where a mistyped current password signed the user out.
* **Open-redirect guard.** `safeNext` accepts only same-origin relative
  paths. It rejects `//`, `/\`, schemes, control characters, and
  `/login` / `/register` loops. Unit tests cover the vectors.
* **Registration** is shown only when `NEXT_PUBLIC_REGISTRATION_ENABLED=true`
  (Q11). The backend stays authoritative: it returns 404 when registration
  is disabled.
* **Proxy and client IP (amends ADR 0022).** The M0 spike showed that the
  Next rewrite passes a client-supplied `X-Forwarded-For` through verbatim.
  Listing the proxy in `TRUSTED_PROXY_IPS` would therefore let any client
  choose its own rate-limit IP. The proxy is **not** trusted. Behind it,
  the per-IP login limit sees the proxy's address, so it acts as a shared
  bucket. The per-email limit is unaffected. A correct client-IP scheme
  (G4) is deferred.
* **CSRF.** Through the proxy the backend's same-origin check never matches,
  because `Host` is rewritten. Protection therefore rests on `Origin`
  matching `FRONTEND_URL`, which must be set to the UI's public origin.
  An E2E test posts with `Origin: https://evil.example` and gets 403.

## Consequences

* An XSS bug could act within an open page, but could not steal the
  credential. The remaining defence is inert rendering (ADR 0029).
* Settings exposes a password change (which revokes the other sessions) and
  "sign out everywhere". Both require a real session; in dev-header mode
  the page says there is no session to manage.
