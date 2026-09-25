# ADR 0024 — Development identity is an explicit mode that cannot start in production

**Status:** Accepted (Phase 9)

## Context

The `X-User-Id` placeholder (ADR 0005) is what the pre-Phase-9 test suite
(942 tests), the eval harnesses and quick local API work rely on. It must keep
working for them, but it must never be reachable where real users are.

## Decision

* **`AUTH_MODE = session | dev_header`**, defaulting to `session`.
  * `dev_header` is the placeholder, byte-for-byte: `X-User-Id`, else
    `DEV_USER_ID`. A presented session credential still wins in either mode.
  * `ALLOW_USER_ID_HEADER=true` remains as a deprecated alias. Setting both to
    contradicting values refuses to start, and an empty value means unset.
* **Every `APP_ENV` other than `development` or `test` is production-like**:
  that includes `staging`, `prod`, a typo, and an empty value. In such an
  environment, startup **fails** with a precise message unless all of these
  hold:
  * `AUTH_MODE` resolves to `session` and `ALLOW_USER_ID_HEADER` is not true;
  * session cookies are `Secure`;
  * an explicit browser origin allow-list exists (`FRONTEND_URL` or
    `CORS_ORIGINS`; the CSRF Origin check needs it);
  * scrypt is at or above the floor N≥2^15, r≥8, N·r·p ≥ 2^15·8·3.

  Additionally, in every environment, `CORS_ALLOW_VERCEL_PREVIEWS` requires
  `CORS_VERCEL_PROJECT`.
* **Registration defaults to off** in production-like environments; accounts
  come from the operator CLI.
* **Tests and evals.** The pre-existing suite runs in `dev_header` mode, as its
  conftest always did. Phase 9 tests run in `session` mode with real accounts,
  and a route-enumerating test asserts that every authenticated route returns
  `401` without a credential in session mode. `eval/graph_api_benchmark.py`
  pins `dev_header` explicitly, since it compares endpoint shapes.

## Consequences

* A production deploy cannot start with a trust-the-client identity, even if
  `APP_ENV` is misspelled.
* Local development can use either mode. `session` plus a CLI-created account
  is the recommended default.
