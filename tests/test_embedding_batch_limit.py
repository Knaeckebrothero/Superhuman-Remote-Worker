"""Embedding batch overflow fix — the single batching seam.

Pins knowledge-base/knowledge/issues/embedding_batch_overflow_skips_citation_source_embeddings.md:
``EmbeddingService.embed_batch`` splits at the provider cap (default 64, the
deployed TEI limit) with order preserved end to end, retries only transient
classes, rejects non-finite vectors with a typed error, and the
CitationEngine persists typed per-source embedding state instead of the old
generic "skipping" swallow.

The fake provider mirrors TEI exactly: >64 inputs → HTTP 422.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from shared.runtime.services.embedding_service import (
    EmbeddingDimensionError,
    EmbeddingInvalidVectorError,
    EmbeddingService,
)

DIM = 8


def _http_error(cls, status: int):
    request = httpx.Request("POST", "http://tei.test/v1/embeddings")
    response = httpx.Response(status, request=request)
    return cls(f"HTTP {status}", response=response, body=None)


class FakeProvider:
    """OpenAI-compatible embeddings endpoint with a TEI-style 64-input cap."""

    def __init__(self, cap: int = 64, fail_plan: list | None = None):
        self.cap = cap
        self.calls: list[int] = []
        #: exceptions to raise on successive calls (None = succeed)
        self.fail_plan = list(fail_plan or [])

    async def create(self, *, input, model):  # noqa: A002 — SDK signature
        self.calls.append(len(input))
        if self.fail_plan:
            planned = self.fail_plan.pop(0)
            if planned is not None:
                raise planned
        if len(input) > self.cap:
            raise _http_error(openai.UnprocessableEntityError, 422)
        # Encode the global input identity in the vector and return the data
        # SHUFFLED with correct indexes, so order preservation must come from
        # the seam's index sort, not provider politeness.
        data = [
            SimpleNamespace(
                index=i, embedding=[float(text.split("-")[1])] + [0.0] * (DIM - 1)
            )
            for i, text in enumerate(input)
        ]
        random.shuffle(data)
        return SimpleNamespace(data=data)


def _service(provider: FakeProvider, monkeypatch, cap_env: str | None = None):
    if cap_env is not None:
        monkeypatch.setenv("EMBEDDING_MAX_BATCH_SIZE", cap_env)
    service = EmbeddingService(
        model="test-embed",
        base_url="http://tei.test/v1",
        api_key="k",
        expected_dimensions=DIM,
    )
    service._client = SimpleNamespace(
        embeddings=SimpleNamespace(create=provider.create)
    )
    return service


@pytest.fixture(autouse=True)
def _instant_retry_sleep(monkeypatch):
    """Retry backoff must not slow the suite down."""
    import shared.runtime.core.llm_retry as llm_retry

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(llm_retry.asyncio, "sleep", _no_sleep)


class TestBatchSplitting:
    @pytest.mark.asyncio
    async def test_splits_at_cap_and_preserves_global_order(self, monkeypatch):
        provider = FakeProvider(cap=64)
        service = _service(provider, monkeypatch)
        texts = [f"t-{i}" for i in range(150)]

        vectors = await service.embed_batch(texts)

        assert provider.calls == [64, 64, 22], (
            "an oversized input list must be split into consecutive "
            "provider-cap slices — one 150-input request is the exact 422 "
            "defect"
        )
        assert [v[0] for v in vectors] == [float(i) for i in range(150)], (
            "vector i must be the embedding of texts[i] across slice "
            "boundaries and provider-shuffled responses"
        )

    @pytest.mark.asyncio
    async def test_single_small_batch_unsplit(self, monkeypatch):
        provider = FakeProvider(cap=64)
        service = _service(provider, monkeypatch)

        vectors = await service.embed_batch([f"t-{i}" for i in range(10)])

        assert provider.calls == [10]
        assert len(vectors) == 10

    @pytest.mark.asyncio
    async def test_cap_configurable_via_env(self, monkeypatch):
        provider = FakeProvider(cap=8)
        service = _service(provider, monkeypatch, cap_env="8")

        await service.embed_batch([f"t-{i}" for i in range(20)])

        assert provider.calls == [8, 8, 4]

    @pytest.mark.asyncio
    async def test_zero_cap_disables_splitting(self, monkeypatch):
        provider = FakeProvider(cap=10_000)
        service = _service(provider, monkeypatch, cap_env="0")

        await service.embed_batch([f"t-{i}" for i in range(100)])

        assert provider.calls == [100]

    @pytest.mark.asyncio
    async def test_empty_input_no_call(self, monkeypatch):
        provider = FakeProvider()
        service = _service(provider, monkeypatch)

        assert await service.embed_batch([]) == []
        assert provider.calls == []


class TestRetryClassification:
    @pytest.mark.asyncio
    async def test_transient_429_retried_then_succeeds(self, monkeypatch):
        provider = FakeProvider(
            cap=64, fail_plan=[_http_error(openai.RateLimitError, 429), None]
        )
        service = _service(provider, monkeypatch)

        vectors = await service.embed_batch([f"t-{i}" for i in range(3)])

        assert len(vectors) == 3
        assert provider.calls == [3, 3], "the 429 slice must be re-attempted"

    @pytest.mark.asyncio
    async def test_5xx_retried_until_exhausted(self, monkeypatch):
        provider = FakeProvider(
            cap=64,
            fail_plan=[_http_error(openai.InternalServerError, 500)] * 5,
        )
        service = _service(provider, monkeypatch)

        with pytest.raises(openai.InternalServerError):
            await service.embed_batch(["t-0"])

        assert provider.calls == [1, 1, 1], "3 bounded attempts, then raise"

    @pytest.mark.asyncio
    async def test_deterministic_422_not_retried(self, monkeypatch):
        provider = FakeProvider(
            cap=64, fail_plan=[_http_error(openai.UnprocessableEntityError, 422)]
        )
        service = _service(provider, monkeypatch)

        with pytest.raises(openai.UnprocessableEntityError):
            await service.embed_batch(["t-0"])

        assert provider.calls == [1], (
            "a deterministic validation failure must not be looped — "
            "retrying it unchanged is pure backend load"
        )


class TestVectorValidation:
    @pytest.mark.asyncio
    async def test_nan_vector_raises_typed_error(self, monkeypatch):
        provider = FakeProvider(cap=64)
        service = _service(provider, monkeypatch)

        async def _nan_create(*, input, model):  # noqa: A002
            data = [
                SimpleNamespace(index=i, embedding=[math.nan] + [0.0] * (DIM - 1))
                for i, _ in enumerate(input)
            ]
            return SimpleNamespace(data=data)

        service._client = SimpleNamespace(
            embeddings=SimpleNamespace(create=_nan_create)
        )

        with pytest.raises(EmbeddingInvalidVectorError, match="non-finite"):
            await service.embed_batch(["t-0"])

    @pytest.mark.asyncio
    async def test_dimension_mismatch_still_latches(self, monkeypatch):
        service = _service(FakeProvider(), monkeypatch)

        async def _short_create(*, input, model):  # noqa: A002
            return SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[1.0, 2.0])]
            )

        service._client = SimpleNamespace(
            embeddings=SimpleNamespace(create=_short_create)
        )

        with pytest.raises(EmbeddingDimensionError):
            await service.embed_batch(["t-0"])
        # latched: next call fails fast without I/O
        with pytest.raises(EmbeddingDimensionError):
            await service.embed_batch(["t-1"])


class TestEngineEmbeddingState:
    """CitationEngine persists typed per-source state (fix 5)."""

    def _engine(self):
        from agent.citation_engine.engine import CitationEngine

        engine = object.__new__(CitationEngine)
        engine.db = MagicMock()
        engine.db.execute = AsyncMock()
        engine.db.fetchval = AsyncMock(return_value=0)
        return engine

    def test_classify_embed_failure_types(self):
        from agent.citation_engine.engine import CitationEngine

        classify = CitationEngine._classify_embed_failure
        assert classify(EmbeddingInvalidVectorError("nan")) == "invalid_vector"
        assert classify(EmbeddingDimensionError("dim")) == "dimension_mismatch"
        assert classify(_http_error(openai.RateLimitError, 429)) == (
            "transient_exhausted"
        )
        assert classify(_http_error(openai.InternalServerError, 500)) == (
            "transient_exhausted"
        )
        assert classify(_http_error(openai.UnprocessableEntityError, 422)) == (
            "provider_rejected"
        )
        assert classify(RuntimeError("boom")) == "error"

    @pytest.mark.asyncio
    async def test_auto_embed_failure_persists_typed_state(self):
        import uuid as uuid_mod

        engine = self._engine()
        engine._get_embedding_service = MagicMock(return_value=MagicMock())
        engine._embed_source_content = AsyncMock(
            side_effect=EmbeddingInvalidVectorError("NaN at position 3")
        )

        await engine._auto_embed_source(7, "content", uuid_mod.uuid4())

        engine.db.execute.assert_awaited_once()
        sql, source_id, state_json = engine.db.execute.await_args.args
        assert "embedding_state" in sql
        assert source_id == 7
        import json

        state = json.loads(state_json)
        assert state["status"] == "failed"
        assert state["reason_type"] == "invalid_vector"
        assert "NaN" in state["reason"]

    @pytest.mark.asyncio
    async def test_set_embedding_state_never_raises(self):
        engine = self._engine()
        engine.db.execute = AsyncMock(side_effect=RuntimeError("db down"))

        # Bookkeeping failure must not break the embed path.
        await engine._set_embedding_state(1, "complete", chunks=3)
