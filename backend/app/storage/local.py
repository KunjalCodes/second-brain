"""Local filesystem `DocumentStorage` -- the only real backend in Phase 6 v1
(zero-cost constraint, ADR 0013, PHASE6_PLAN.md section 10).

Every file is written under `<root>/<user_id>/<generated key>`. The
generated key is a UUID this module creates itself on `put` -- never a
filename or path fragment a caller supplies -- so there is no code path by
which an untrusted filename can influence where a file is written or read
from (PHASE6_PLAN.md section 9: filename sanitization / no path traversal).
`user_id` is validated against a strict allow-list of characters before it
is ever used to build a path, as defense in depth on top of the existing
`X-User-Id` length check (`app.api.deps.MAX_USER_ID_LENGTH`).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from app.core.errors import NotFoundError, StorageUnavailableError
from app.storage.base import StoragePutResult

#: Matches `app.api.deps`'s own user-id handling: alphanumeric plus a small
#: set of safe separators. Anything else (path separators, `..`, null
#: bytes) is rejected before it can ever reach a filesystem path.
_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _validate_user_id(user_id: str) -> str:
    if not _SAFE_USER_ID.match(user_id):
        # Defense in depth, not the primary isolation boundary (that is
        # `get_current_user_id`) -- a user_id reaching here already passed
        # that check in every real request path, so this should never
        # trigger outside a test deliberately probing it.
        raise StorageUnavailableError(
            "Refusing to build a storage path from an unsafe user id",
            details={"user_id_length": len(user_id)},
        )
    return user_id


def _write(user_dir: Path, key: str, data: bytes) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / key).write_bytes(data)


class LocalDocumentStorage:
    """Writes under a configured root directory. No network, no cost, no
    account -- the default in every non-opt-in test and the only backend
    local development needs (mirrors `FakeLLMClient`/`FakeEmbeddingClient`'s
    "the whole system runs with no external account" role, except this one
    is not a test double: it is the real v1 implementation).
    """

    def __init__(self, *, root_dir: str) -> None:
        self._root = Path(root_dir)

    def _user_dir(self, user_id: str) -> Path:
        return self._root / _validate_user_id(user_id)

    def _path_for(self, *, user_id: str, storage_key: str) -> Path:
        # `storage_key` is always a UUID this module generated in `put` --
        # never taken from a caller-supplied filename -- so no further
        # sanitization is needed here beyond confirming it looks like one.
        if not re.fullmatch(r"[0-9a-f-]{36}", storage_key):
            raise NotFoundError("No such document storage object")
        return self._user_dir(user_id) / storage_key

    async def put(self, *, data: bytes, user_id: str) -> StoragePutResult:
        try:
            user_dir = self._user_dir(user_id)
            key = str(uuid.uuid4())
            # Blocking file I/O runs on a worker thread, not the event loop
            # (Phase 11, S12): a slow disk must not stall every request.
            await asyncio.to_thread(_write, user_dir, key, data)
        except OSError as exc:
            raise StorageUnavailableError(
                "Could not write to local document storage", details={"reason": str(exc)}
            ) from exc
        return StoragePutResult(storage_key=key, size_bytes=len(data))

    async def get(self, *, storage_key: str, user_id: str) -> bytes:
        path = self._path_for(user_id=user_id, storage_key=storage_key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise NotFoundError("No such document storage object") from exc
        except OSError as exc:
            raise StorageUnavailableError(
                "Could not read from local document storage", details={"reason": str(exc)}
            ) from exc

    async def delete(self, *, storage_key: str, user_id: str) -> None:
        path = self._path_for(user_id=user_id, storage_key=storage_key)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise StorageUnavailableError(
                "Could not delete from local document storage", details={"reason": str(exc)}
            ) from exc
