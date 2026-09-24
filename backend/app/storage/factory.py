"""Document storage backend selection from configuration.

The only place `DOCUMENT_STORAGE_PROVIDER` is read to decide *which class*
to instantiate -- mirrors `app.llm.factory`/`app.embeddings.factory` exactly.
Only `"local"` is registered in Phase 6 v1 (zero-cost constraint); a value
other than `"local"` is already rejected at `Settings` construction time
(`Settings._check_document_storage_provider`), so the `ConfigurationError`
below is unreachable in practice and exists only so this factory fails
loudly, like every sibling factory, if that invariant is ever weakened.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.storage.base import DocumentStorage
from app.storage.local import LocalDocumentStorage


def build_document_storage(settings: Settings | None = None) -> DocumentStorage:
    settings = settings or get_settings()

    if settings.document_storage_provider == "local":
        return LocalDocumentStorage(root_dir=settings.document_storage_dir)

    raise ConfigurationError(
        f"No DocumentStorage implementation is registered for "
        f"DOCUMENT_STORAGE_PROVIDER={settings.document_storage_provider!r} yet"
    )


_client: DocumentStorage | None = None


def get_document_storage() -> DocumentStorage:
    """Process-wide client, mirroring `get_llm_client` / `get_embedding_client`."""
    global _client
    if _client is None:
        _client = build_document_storage()
    return _client


def reset_document_storage() -> None:
    """Drop the process-wide client. Used by tests."""
    global _client
    _client = None
