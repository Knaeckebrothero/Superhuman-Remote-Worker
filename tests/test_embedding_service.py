"""Unit tests for EmbeddingService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_env(monkeypatch):
    """Set up environment variables for testing."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-123")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)


@pytest.fixture
def mock_openai_client():
    """Mock AsyncOpenAI client."""
    with patch("src.services.embedding_service.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        yield mock_client, mock_cls


class TestEmbeddingServiceInit:
    """Test EmbeddingService initialization."""

    def test_init_with_openai_key(self, mock_env, mock_openai_client):
        """Uses OPENAI_API_KEY when EMBEDDING_API_KEY not set."""
        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.api_key == "test-key-123"
        assert service.model == "qwen3-embedding-8b"
        assert service.base_url == "https://api.openai.com/v1"

    def test_init_with_embedding_key(self, monkeypatch, mock_openai_client):
        """Prefers EMBEDDING_API_KEY over OPENAI_API_KEY."""
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-key")

        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.api_key == "embedding-key"

    def test_init_custom_model(self, mock_env, monkeypatch, mock_openai_client):
        """Respects EMBEDDING_MODEL env var."""
        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-large")

        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.model == "text-embedding-3-large"

    def test_init_custom_base_url(self, mock_env, monkeypatch, mock_openai_client):
        """Respects EMBEDDING_BASE_URL env var."""
        monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:11434/v1")

        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.base_url == "http://localhost:11434/v1"


class TestEmbeddingServiceSingleton:
    """Test get_embedding_service singleton pattern."""

    def test_singleton_returns_same_instance(self, mock_env, mock_openai_client):
        """get_embedding_service returns the same instance on repeated calls."""
        import src.services.embedding_service as mod

        # Reset singleton
        mod._embedding_service = None

        s1 = mod.get_embedding_service()
        s2 = mod.get_embedding_service()
        assert s1 is s2

        # Cleanup
        mod._embedding_service = None


class TestEmbeddingServiceEmbed:
    """Test embed() and embed_batch() methods."""

    @pytest.mark.asyncio
    async def test_embed_single(self, mock_env, monkeypatch, mock_openai_client):
        """embed() returns a vector from the API response."""
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "3")
        mock_client, _ = mock_openai_client
        from src.services.embedding_service import EmbeddingService

        # Mock the API response
        mock_embedding = MagicMock()
        mock_embedding.embedding = [0.1, 0.2, 0.3]
        mock_response = MagicMock()
        mock_response.data = [mock_embedding]
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        service = EmbeddingService()
        result = await service.embed("test text")

        assert result == [0.1, 0.2, 0.3]
        mock_client.embeddings.create.assert_awaited_once_with(
            input="test text",
            model="qwen3-embedding-8b",
        )

    @pytest.mark.asyncio
    async def test_embed_batch(self, mock_env, monkeypatch, mock_openai_client):
        """embed_batch() returns vectors in input order."""
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1")
        mock_client, _ = mock_openai_client
        from src.services.embedding_service import EmbeddingService

        # Mock response with out-of-order indices
        mock_e1 = MagicMock()
        mock_e1.embedding = [0.1]
        mock_e1.index = 1
        mock_e2 = MagicMock()
        mock_e2.embedding = [0.2]
        mock_e2.index = 0

        mock_response = MagicMock()
        mock_response.data = [mock_e1, mock_e2]  # Intentionally out of order
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        service = EmbeddingService()
        result = await service.embed_batch(["hello", "world"])

        # Should be sorted by index
        assert result == [[0.2], [0.1]]

    @pytest.mark.asyncio
    async def test_embed_batch_empty(self, mock_env, mock_openai_client):
        """embed_batch() handles empty input."""
        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        result = await service.embed_batch([])
        assert result == []


class TestDimensionGuard:
    """B4: dimension mismatch must latch loudly instead of failing quietly."""

    def _mock_response(self, mock_client, vector):
        mock_embedding = MagicMock()
        mock_embedding.embedding = vector
        mock_embedding.index = 0
        mock_response = MagicMock()
        mock_response.data = [mock_embedding]
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

    def test_default_expected_dimensions(self, mock_env, mock_openai_client):
        """Defaults to the schema's vector(4096)."""
        from src.services.embedding_service import EmbeddingService

        assert EmbeddingService().expected_dimensions == 4096

    @pytest.mark.asyncio
    async def test_mismatch_raises_and_latches(self, mock_env, mock_openai_client):
        """Wrong dimensionality raises EmbeddingDimensionError and latches."""
        mock_client, _ = mock_openai_client
        from src.services.embedding_service import (
            EmbeddingDimensionError,
            EmbeddingService,
        )

        self._mock_response(mock_client, [0.1, 0.2, 0.3])  # 3 != 4096
        service = EmbeddingService()

        with pytest.raises(EmbeddingDimensionError, match="3-dim"):
            await service.embed("test")
        assert service.degraded_reason is not None

        # Second call fails fast without touching the API again.
        mock_client.embeddings.create.reset_mock()
        with pytest.raises(EmbeddingDimensionError):
            await service.embed("test")
        mock_client.embeddings.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batch_mismatch_raises(self, mock_env, mock_openai_client):
        """embed_batch() checks dimensions too."""
        mock_client, _ = mock_openai_client
        from src.services.embedding_service import (
            EmbeddingDimensionError,
            EmbeddingService,
        )

        self._mock_response(mock_client, [0.1])
        service = EmbeddingService()

        with pytest.raises(EmbeddingDimensionError):
            await service.embed_batch(["a"])
        assert service.degraded_reason is not None

    @pytest.mark.asyncio
    async def test_verify_dimensions_ok(
        self, mock_env, monkeypatch, mock_openai_client
    ):
        """Probe returns True when dimensions match."""
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "2")
        mock_client, _ = mock_openai_client
        from src.services.embedding_service import EmbeddingService

        self._mock_response(mock_client, [0.1, 0.2])
        service = EmbeddingService()

        assert await service.verify_dimensions() is True
        assert service.degraded_reason is None

    @pytest.mark.asyncio
    async def test_verify_dimensions_mismatch(self, mock_env, mock_openai_client):
        """Probe returns False on mismatch and latches degraded."""
        mock_client, _ = mock_openai_client
        from src.services.embedding_service import EmbeddingService

        self._mock_response(mock_client, [0.1, 0.2])
        service = EmbeddingService()

        assert await service.verify_dimensions() is False
        assert service.degraded_reason is not None

    @pytest.mark.asyncio
    async def test_verify_dimensions_inconclusive(self, mock_env, mock_openai_client):
        """Connectivity failures must NOT latch degraded (transient)."""
        mock_client, _ = mock_openai_client
        from src.services.embedding_service import EmbeddingService

        mock_client.embeddings.create = AsyncMock(side_effect=ConnectionError("down"))
        service = EmbeddingService()

        assert await service.verify_dimensions() is None
        assert service.degraded_reason is None

    def test_health_snapshot(self, mock_env, mock_openai_client):
        """Snapshot carries the degraded flag for /status."""
        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        snap = service.health_snapshot()
        assert snap["degraded"] is False
        assert snap["expected_dimensions"] == 4096
        assert snap["model"] == "qwen3-embedding-8b"

        service.degraded_reason = "boom"
        snap = service.health_snapshot()
        assert snap["degraded"] is True
        assert snap["degraded_reason"] == "boom"

    def test_peek_does_not_construct(self, mock_env, mock_openai_client):
        """peek_embedding_service() never builds the singleton."""
        import src.services.embedding_service as mod

        mod._embedding_service = None
        assert mod.peek_embedding_service() is None
        assert mod._embedding_service is None

        s = mod.get_embedding_service()
        assert mod.peek_embedding_service() is s
        mod._embedding_service = None


class TestEmbeddingProviders:
    """Test provider-based initialization."""

    def test_local_provider_default(self, mock_env, mock_openai_client):
        """Default provider is 'local'."""
        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.provider == "local"
        assert service.model == "qwen3-embedding-8b"
        assert service.base_url == "https://api.openai.com/v1"

    def test_openrouter_provider(self, monkeypatch, mock_openai_client):
        """OpenRouter provider uses correct URL and prefixed model."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-123")

        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.provider == "openrouter"
        assert service.api_key == "or-key-123"
        assert service.base_url == "https://openrouter.ai/api/v1"
        assert service.model == "qwen/qwen3-embedding-8b"

    def test_openrouter_provider_custom_model(self, monkeypatch, mock_openai_client):
        """OpenRouter auto-prefixes model name with qwen/."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-123")
        monkeypatch.setenv("EMBEDDING_MODEL", "qwen3-embedding-0.6b")

        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.model == "qwen/qwen3-embedding-0.6b"

    def test_openrouter_provider_full_model_name(self, monkeypatch, mock_openai_client):
        """OpenRouter skips prefix when model already contains /."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-123")
        monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")

        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.model == "openai/text-embedding-3-small"

    def test_explicit_local_provider(self, mock_env, monkeypatch, mock_openai_client):
        """Explicit EMBEDDING_PROVIDER=local works."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "local")

        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        assert service.provider == "local"
        assert service.api_key == "test-key-123"

    def test_unknown_provider_falls_back_to_local(
        self, mock_env, monkeypatch, mock_openai_client
    ):
        """Unknown provider name falls back to local behavior."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "nonexistent")

        from src.services.embedding_service import EmbeddingService

        service = EmbeddingService()
        # Unknown provider goes through the else (local) branch
        assert service.provider == "nonexistent"
        assert service.base_url == "https://api.openai.com/v1"
