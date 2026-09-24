"""`FakeEmbeddingClient` / `deterministic_vector` -- no network, no database."""

from __future__ import annotations

import math

import pytest

from app.embeddings.base import EmbeddingResult
from app.embeddings.fake import DEFAULT_FAKE_MODEL, FakeEmbeddingClient, deterministic_vector


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class TestDeterministicVector:
    def test_same_text_always_produces_the_same_vector(self) -> None:
        v1 = deterministic_vector("The project uses Gemini", dimension=64)
        v2 = deterministic_vector("The project uses Gemini", dimension=64)
        assert v1 == v2

    def test_vector_is_unit_length(self) -> None:
        v = deterministic_vector("some arbitrary sentence with several words", dimension=64)
        magnitude = math.sqrt(sum(x * x for x in v))
        assert magnitude == pytest.approx(1.0, abs=1e-9)

    def test_vector_has_the_requested_dimension(self) -> None:
        assert len(deterministic_vector("hello", dimension=768)) == 768
        assert len(deterministic_vector("hello", dimension=32)) == 32

    def test_texts_sharing_most_words_are_highly_similar(self) -> None:
        v1 = deterministic_vector(
            "the project uses gemini for classification of images", dimension=256
        )
        v2 = deterministic_vector(
            "the project uses gemini for classification of documents", dimension=256
        )
        assert _cosine(v1, v2) > 0.8

    def test_texts_sharing_no_words_are_not_spuriously_similar(self) -> None:
        v1 = deterministic_vector(
            "alpha bravo charlie delta echo foxtrot golf hotel", dimension=256
        )
        v2 = deterministic_vector("india juliet kilo lima mike november oscar papa", dimension=256)
        assert abs(_cosine(v1, v2)) < 0.3

    def test_case_insensitive(self) -> None:
        v1 = deterministic_vector("Gemini Uses Classification", dimension=64)
        v2 = deterministic_vector("gemini uses classification", dimension=64)
        assert v1 == v2

    def test_punctuation_does_not_change_the_result(self) -> None:
        v1 = deterministic_vector("uses Gemini, for classification!", dimension=64)
        v2 = deterministic_vector("uses Gemini for classification", dimension=64)
        assert v1 == v2

    def test_empty_text_does_not_raise(self) -> None:
        v = deterministic_vector("", dimension=64)
        assert len(v) == 64
        assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0, abs=1e-9)


class TestFakeEmbeddingClient:
    async def test_embed_returns_a_result_with_the_configured_dimension(self) -> None:
        client = FakeEmbeddingClient(dimension=128)
        result = await client.embed(text="hello world")
        assert isinstance(result, EmbeddingResult)
        assert len(result.vector) == 128
        assert result.dimension == 128

    async def test_provider_and_model_are_reported(self) -> None:
        client = FakeEmbeddingClient(model="my-fake-model", dimension=32)
        result = await client.embed(text="hello")
        assert result.provider == "fake"
        assert result.model == "my-fake-model"

    async def test_default_model_name_matches_the_documented_constant(self) -> None:
        client = FakeEmbeddingClient(dimension=32)
        result = await client.embed(text="hello")
        assert result.model == DEFAULT_FAKE_MODEL

    async def test_timeout_argument_is_accepted_and_ignored(self) -> None:
        client = FakeEmbeddingClient(dimension=32)
        result = await client.embed(text="hello", timeout=5.0)
        assert len(result.vector) == 32

    async def test_no_network_or_filesystem_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cheap sanity guard: FakeEmbeddingClient must never attempt a real
        network call, matching FakeLLMClient's own guarantee.
        """
        import socket

        def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("FakeEmbeddingClient must never open a socket")

        monkeypatch.setattr(socket, "socket", _forbidden)
        client = FakeEmbeddingClient(dimension=32)
        result = await client.embed(text="no network needed")
        assert len(result.vector) == 32
