# ADR 0029 — All backend content is rendered as inert text

**Status:** Accepted (Phase 10)

## Context

Memories, document chunks, messages, entity names, review evidence and
agent answers are all derived from user- or document-supplied text. Any of
them may contain HTML or prompt-injection text ("Ignore previous
instructions…"). Phase 8 set the rule "render all content as text". The UI
must enforce it.

## Decision

* **React text nodes only.**
  * There is no Markdown or HTML renderer dependency.
  * `react/no-danger` is an ESLint **error**.
  * CI greps for `dangerouslySetInnerHTML` / `.innerHTML =` and fails on a
    match.
  * Long text uses a `pre-wrap` class; the content itself is never parsed.
* **URLs.**
  * A source URL is always shown as text.
  * An "open" link is offered only for `http:` / `https:`, with
    `target="_blank" rel="noopener noreferrer"`.
  * `javascript:` and other schemes are labelled "not opened".
* **Review evidence** (a free-form dict) is rendered as key and value text.
* **Agent answers** are plain text. Citations come only from the server's
  validated `cited_memory_ids`, never parsed out of the answer.
* **Headers** (`next.config.ts`):
  * CSP: `default-src 'self'`, `connect-src 'self'`, `object-src 'none'`,
    `frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'`;
  * `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `nosniff`,
    no `X-Powered-By`.
  * `script-src` includes `'unsafe-inline'` because of Next.js's inline
    bootstrap. `'unsafe-eval'` is included in development only; the E2E
    suite passes against a production build without it.

## Consequences

* Tests:
  * A component test renders an `<img onerror>` / `<script>` payload in
    provenance and asserts it appears as text with no element created.
  * An E2E test does the same for a seeded memory in a real browser, and
    checks `window.__pwned` stays undefined.
* The CSP is defence in depth, not the primary control: `'unsafe-inline'`
  scripts are allowed. A nonce-based CSP would require dynamic rendering
  of every page and is deferred.
* Rich formatting of memory content is intentionally unavailable.
