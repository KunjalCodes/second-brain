# ADR 0013 — Document bytes live on local filesystem storage, not object storage

**Status:** Accepted (Phase 6)

## Context

The originally-approved Phase 6 plan designed a `DocumentStorage` abstraction with a
`LocalDocumentStorage` implementation for development and an
`S3CompatibleDocumentStorage` implementation (via `aioboto3`) for anything resembling
production, on the premise that a deployed system would eventually need real object
storage.

At approval time, the project owner added a hard, explicit constraint: **the entire
project must remain zero-cost** -- no paid API, cloud resource, storage service,
database tier, or billing-enabled service may be required, ever, including as a
"production-ready" default a developer might reasonably flip on. Free tiers are
allowed only where their limits are respected; hitting one must degrade gracefully or
be reported, never trigger an upgrade prompt or a billing-enabled fallback.

`aioboto3` targets AWS S3 (or S3-compatible services) directly; even against a
generous free tier, it is designed around a billing relationship that does not exist
in this project's constraint, and there is no free, zero-setup, zero-account
S3-compatible service that fits "local development needs nothing but this repo and
free/open-source software."

## Decision

Build **only** `LocalDocumentStorage`: files written under `<root>/<user_id>/<generated
uuid4 key>` on local disk (`app/storage/local.py`), using nothing beyond the Python
standard library (`pathlib`). The `DocumentStorage` Protocol (`put`/`get`/`delete`,
keyed by an opaque `storage_key`, mirroring `LLMClient`/`EmbeddingClient`'s own
Protocol + concrete-implementation + factory + process-wide-singleton shape) is kept
exactly as planned, so a genuinely zero-cost object-storage-compatible backend (a real
free tier with no billing relationship, if one is later identified and explicitly
approved) could be added as a second registered provider without touching any caller.

The originally-planned `S3CompatibleDocumentStorage` is **not built**. It is not
stubbed, not partially implemented, and not left as dead code behind a flag --
`Settings._check_document_storage_provider` rejects any `DOCUMENT_STORAGE_PROVIDER`
value other than `"local"` at construction time, so the zero-cost constraint is
enforced at configuration load, not merely by omission.

No separate `FakeDocumentStorage` test double was built either. Unlike
`FakeLLMClient`/`FakeEmbeddingClient`, which exist specifically to fake out a genuinely
remote, potentially paid API in tests, `LocalDocumentStorage` pointed at a `tmp_path`
is already free, local, and deterministic -- there is nothing left to fake that isn't
already exactly what a test needs.

## Consequences

* **Zero-cost is structural, not a documentation promise.** A developer cloning this
  repository and running the test suite or a local `uvicorn` instance touches no
  billing-capable service for document storage, by construction, not by discipline.
* **A real, stated limitation, not hidden**: local filesystem storage is not shared
  across multiple application instances and is not guaranteed to survive an ephemeral
  redeploy on most managed hosts. This is documented in `docs/PHASE6_PLAN.md` sections
  10/19 as an explicit, accepted trade-off for v1, not an oversight -- a project
  requirement (zero cost) directly conflicts with a property (durable, multi-instance
  shared storage) that real object storage would otherwise provide for free-tier-or-paid
  money. The trade was made deliberately in the direction the project's own hard
  constraint requires.
* **The abstraction boundary still pays for itself.** Every caller (`DocumentService`,
  `IngestionRunner`) depends on the `DocumentStorage` Protocol, never on
  `LocalDocumentStorage` directly (`app/storage/factory.py`'s `get_document_storage()`
  is the only construction site) -- so this decision is revisable later without a
  caller-side rewrite, exactly the property ADR-style abstraction boundaries exist to
  preserve, without having paid the cost of building and maintaining an unused S3
  adapter today.
* The net new runtime dependency for all of Phase 6 is `pypdf` alone; `aioboto3` was
  removed from the plan entirely rather than added and left unused.
