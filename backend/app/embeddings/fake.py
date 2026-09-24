"""`FakeEmbeddingClient` -- deterministic, offline, no provider account needed.

Same first-class role as `app.llm.fake.FakeLLMClient`: the whole system, including
semantic duplicate detection and vector retrieval, runs end to end with no network
access and no cost. This is what the test suite and the default deterministic
evaluation harness always use.

**This does not pretend to reproduce real embedding model behavior.** It exists
for deterministic testing of storage, querying, ranking, thresholds, idempotency,
and failure handling -- not for measuring real semantic quality (that needs a real
provider, and is explicitly out of scope for the default test suite).
"""

from __future__ import annotations

import hashlib
import math
import re
import time

from app.embeddings.base import EmbeddingResult

#: Matches app.models.normalization's own tokenization philosophy: cheap, no NLP
#: dependency. Words, not characters, are what should make two texts' vectors
#: land close together -- "uses Gemini" and "Gemini is used" should be nearer to
#: each other than either is to "totally unrelated content".
_WORD_PATTERN = re.compile(r"[a-z0-9]+")

#: The single source of truth for FakeEmbeddingClient's default model name.
#: `Settings.embedding_model_resolved` reads this too, so the backfill sweep's
#: "does this memory already have a row under the current model" check can never
#: drift from what a real `embed()` call actually stores.
DEFAULT_FAKE_MODEL = "fake-embedding-v1"


def _tokenize(text: str) -> list[str]:
    return _WORD_PATTERN.findall(text.lower())


def _token_bucket_and_sign(token: str, dimension: int) -> tuple[int, float]:
    """Which vector component a token contributes to, and with what sign.

    The standard "hashing trick" (feature hashing): a deterministic hash decides
    both the bucket (so a token always lands in the same place) and the sign (so
    unrelated tokens partially cancel rather than only ever adding up), which is
    what keeps vectors for longer, unrelated texts from all drifting toward the
    same direction.
    """
    digest = hashlib.sha256(token.encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % dimension
    sign = 1.0 if digest[8] & 1 else -1.0
    return bucket, sign


def deterministic_vector(text: str, *, dimension: int) -> list[float]:
    """A bag-of-words hashed embedding: same text -> same vector, always; texts
    sharing more words land closer together (by cosine similarity) than texts
    sharing none.

    This is a real, if crude, embedding technique (feature hashing / the
    "hashing trick", e.g. scikit-learn's `HashingVectorizer`), not a random
    placeholder -- which is exactly what makes it useful for testing ranking and
    threshold behavior: constructing two texts that share words is a reliable way
    to construct two vectors with high cosine similarity, and
    `tests/unit/test_embeddings_fake.py` relies on exactly that property.
    """
    tokens = _tokenize(text) or ["__empty__"]
    vector = [0.0] * dimension
    for token in tokens:
        bucket, sign = _token_bucket_and_sign(token, dimension)
        vector[bucket] += sign

    magnitude = math.sqrt(sum(v * v for v in vector))
    if magnitude == 0:
        # Every component happened to cancel out (possible with very few unique
        # tokens colliding into opposite-signed buckets). Fall back to a fixed
        # unit vector rather than dividing by zero -- an embedding client must
        # never fail on ordinary text.
        vector = [1.0] + [0.0] * (dimension - 1)
        magnitude = 1.0
    return [v / magnitude for v in vector]


class FakeEmbeddingClient:
    """Deterministic `EmbeddingClient` -- see the module docstring."""

    def __init__(self, *, model: str = DEFAULT_FAKE_MODEL, dimension: int = 768) -> None:
        self._model = model
        self._dimension = dimension

    async def embed(self, *, text: str, timeout: float | None = None) -> EmbeddingResult:
        started = time.monotonic()
        vector = deterministic_vector(text, dimension=self._dimension)
        latency_ms = int((time.monotonic() - started) * 1000)
        return EmbeddingResult(
            vector=vector,
            provider="fake",
            model=self._model,
            dimension=self._dimension,
            latency_ms=latency_ms,
        )
