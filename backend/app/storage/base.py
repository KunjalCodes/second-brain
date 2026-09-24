"""Document byte storage abstraction (Phase 6, ADR 0013, PHASE6_PLAN.md
section 10).

Structurally identical in shape to `app.llm.base.LLMClient`/`app.embeddings.
base.EmbeddingClient`: the rest of the application depends on
`DocumentStorage`, never on a concrete backend. Only `app/storage/local.py`
(and any future backend adapter) may know how bytes are actually persisted.

**Zero-cost constraint (approved at Phase 6 sign-off):** v1 ships exactly
one real backend, `LocalDocumentStorage`, writing to the local filesystem --
no paid object-storage account is required to build, test, or demonstrate
this phase. A future S3-compatible backend can be added behind this same
Protocol without changing any caller, but it is not built until a concrete,
non-speculative need exists (PHASE6_PLAN.md section 10/19).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DocumentStorage(Protocol):
    """Structural interface every document storage backend implements.

    Every method is keyed by an opaque `storage_key` the backend itself
    generates on `put` -- never a path or filename supplied by a caller, so
    a caller can never coerce this abstraction into writing to or reading
    from an attacker-chosen location (PHASE6_PLAN.md section 9).
    """

    async def put(self, *, data: bytes, user_id: str) -> StoragePutResult:
        """Store `data`, returning the opaque key to retrieve it by later.

        Raises `StorageUnavailableError` for a transient failure (e.g. disk
        write failure) worth retrying.
        """
        ...

    async def get(self, *, storage_key: str, user_id: str) -> bytes:
        """Retrieve previously-stored bytes.

        Raises `NotFoundError` if `storage_key` does not exist (or does not
        belong to `user_id`), `StorageUnavailableError` for a transient
        failure.
        """
        ...

    async def delete(self, *, storage_key: str, user_id: str) -> None:
        """Remove previously-stored bytes. A no-op if already absent --
        deletion is idempotent, matching the codebase's standing "repeated
        submission must not error" discipline.
        """
        ...


@dataclass(frozen=True)
class StoragePutResult:
    storage_key: str
    size_bytes: int
