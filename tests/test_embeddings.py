"""Tests for embedding service, semantic chunking, and auto-embed."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from citation_engine import CitationContext, CitationEngine
from citation_engine.chunking import SemanticChunker, _cosine_similarity, _split_sentences
from citation_engine.embeddings import (
    EmbeddingService,
    EmbeddingServiceNotConfigured,
)

# =============================================================================
# EmbeddingService Tests
# =============================================================================


class TestEmbeddingService:
    """Tests for the EmbeddingService class."""

    def test_init_with_explicit_params(self):
        """Test construction with explicit parameters."""
        service = EmbeddingService(
            model="test-model",
            api_url="http://localhost:11434/v1",
            api_key="test-key",
        )
        assert service.model == "test-model"
        assert service.api_url == "http://localhost:11434/v1"
        assert service.api_key == "test-key"

    def test_init_from_env(self, monkeypatch):
        """Test construction from environment variables."""
        monkeypatch.setenv("CITATION_EMBEDDING_MODEL", "bge-small-en-v1.5")
        monkeypatch.setenv("CITATION_EMBEDDING_URL", "http://ollama:11434/v1")
        monkeypatch.setenv("CITATION_EMBEDDING_KEY", "sk-test")

        service = EmbeddingService()
        assert service.model == "bge-small-en-v1.5"
        assert service.api_url == "http://ollama:11434/v1"
        assert service.api_key == "sk-test"

    def test_init_no_key_openai_url_raises(self, monkeypatch):
        """Test that missing key with OpenAI URL raises error."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("CITATION_EMBEDDING_KEY", raising=False)
        monkeypatch.delenv("CITATION_EMBEDDING_URL", raising=False)

        with pytest.raises(EmbeddingServiceNotConfigured):
            EmbeddingService()

    def test_init_no_key_local_url_ok(self, monkeypatch):
        """Test that local URL works without API key."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("CITATION_EMBEDDING_KEY", raising=False)

        service = EmbeddingService(api_url="http://localhost:11434/v1")
        assert service.is_configured is True

    def test_known_dimension(self):
        """Test known model dimensions are returned without API call."""
        service = EmbeddingService(
            model="text-embedding-3-small",
            api_key="test",
        )
        assert service.dimension == 1536

    def test_strips_trailing_slash(self):
        """Test URL trailing slash is stripped."""
        service = EmbeddingService(
            api_url="http://localhost:11434/v1/",
            api_key="test",
        )
        assert service.api_url == "http://localhost:11434/v1"

    @patch("httpx.Client")
    def test_embed_batch(self, MockClient):
        """Test batch embedding with mocked API response."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"embedding": [0.1, 0.2, 0.3], "index": 0},
                {"embedding": [0.4, 0.5, 0.6], "index": 1},
            ],
            "model": "test-model",
            "usage": {"total_tokens": 10},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        MockClient.return_value = mock_client

        service = EmbeddingService(model="test-model", api_key="test")
        results = service.embed_batch(["hello", "world"])

        assert len(results) == 2
        assert results[0] == [0.1, 0.2, 0.3]
        assert results[1] == [0.4, 0.5, 0.6]

    @patch("httpx.Client")
    def test_embed_single(self, MockClient):
        """Test single text embedding."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        MockClient.return_value = mock_client

        service = EmbeddingService(model="test-model", api_key="test")
        result = service.embed("hello")

        assert result == [0.1, 0.2, 0.3]

    def test_embed_batch_empty(self):
        """Test embedding empty list returns empty."""
        service = EmbeddingService(model="test-model", api_key="test")
        assert service.embed_batch([]) == []

    def test_is_configured_with_key(self):
        """Test is_configured returns True with API key."""
        service = EmbeddingService(model="test", api_key="sk-test")
        assert service.is_configured is True

    def test_is_configured_local_no_key(self, monkeypatch):
        """Test is_configured for local URL without key."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        service = EmbeddingService(
            model="test",
            api_url="http://localhost:11434/v1",
            api_key=None,
        )
        assert service.is_configured is True


# =============================================================================
# SemanticChunker Tests
# =============================================================================


class TestSplitSentences:
    """Tests for the sentence splitting utility."""

    def test_basic_sentences(self):
        sentences = _split_sentences("Hello world. This is a test. Final sentence.")
        assert len(sentences) == 3

    def test_paragraph_boundaries(self):
        text = "First paragraph.\n\nSecond paragraph."
        sentences = _split_sentences(text)
        assert len(sentences) == 2

    def test_empty_text(self):
        assert _split_sentences("") == []
        assert _split_sentences("   ") == []


class TestCosineSimlarity:
    """Tests for cosine similarity utility."""

    def test_identical_vectors(self):
        assert _cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert _cosine_similarity([0, 0], [1, 1]) == 0.0


class TestSemanticChunker:
    """Tests for SemanticChunker."""

    def test_short_text_single_chunk(self):
        """Short text should be returned as-is."""
        chunker = SemanticChunker()
        chunks = chunker.chunk("Short text.")
        assert chunks == ["Short text."]

    def test_empty_text(self):
        """Empty text should return empty list."""
        chunker = SemanticChunker()
        assert chunker.chunk("") == []
        assert chunker.chunk("   ") == []

    def test_fixed_fallback_no_service(self):
        """Without embedding service, falls back to fixed-size chunking."""
        long_text = "This is a sentence. " * 100  # ~2000 chars
        chunker = SemanticChunker(embedding_service=None, max_chunk_size=500)
        chunks = chunker.chunk(long_text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) > 0

    def test_semantic_chunking_with_mock(self):
        """Test semantic chunking with a mock embedding service."""
        mock_service = MagicMock()

        # Windows spanning the topic boundary will mix A and B vectors,
        # producing low similarity. We track call index to control this.
        def fake_embed_batch(texts):
            """Return similar vectors for same-topic windows, dissimilar at boundary."""
            results = []
            for text in texts:
                if "topic B" in text.lower():
                    results.append([0.0, 1.0, 0.0])  # Topic B space
                else:
                    results.append([1.0, 0.0, 0.0])  # Topic A space
            return results

        mock_service.embed_batch = fake_embed_batch

        # Build text with clear topic boundary — must exceed max_chunk_size
        topic_a = ". ".join([f"Topic A sentence number {i} with extra content to increase size" for i in range(15)]) + "."
        topic_b = ". ".join([f"Topic B sentence number {i} with extra content to increase size" for i in range(15)]) + "."
        text = topic_a + " " + topic_b

        chunker = SemanticChunker(
            embedding_service=mock_service,
            similarity_threshold=0.5,
            window_size=2,
            min_chunk_size=10,
            max_chunk_size=500,
        )
        chunks = chunker.chunk(text)

        # Should split into at least 2 chunks (topic A and topic B)
        assert len(chunks) >= 2
        # First chunk should contain topic A content
        assert "Topic A" in chunks[0]

    def test_merge_small_chunks(self):
        """Test that very small chunks get merged."""
        chunker = SemanticChunker(min_chunk_size=50)
        chunks = chunker._merge_small_chunks(["Hi.", "Also short.", "This is a longer chunk that should stay separate on its own."])
        # First two should be merged
        assert len(chunks) <= 2


# =============================================================================
# Auto-Embed Integration Tests
# =============================================================================


class TestAutoEmbed:
    """Tests for auto-embed on source registration."""

    @pytest.fixture
    def engine_with_context(self):
        """Create engine with context for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        ctx = CitationContext(session_id="test-job-embed")
        engine = CitationEngine(mode="basic", db_path=db_path, context=ctx)
        engine._connect()
        yield engine
        engine.close()
        os.unlink(db_path)

    def test_auto_embed_skips_sqlite(self, engine_with_context):
        """Auto-embed should silently skip in SQLite mode."""
        # Register source — should not fail even without embedding service
        source = engine_with_context.add_custom_source(
            name="Test", content="Hello world for embedding test"
        )
        assert source.id > 0

        # Verify no embeddings were created (SQLite mode skips)
        engine_with_context._cursor.execute(
            "SELECT COUNT(*) as cnt FROM source_embeddings"
        )
        result = engine_with_context._cursor.fetchone()
        assert result["cnt"] == 0

    def test_schema_v3_creates_embeddings_table(self, engine_with_context):
        """Verify migration v3 created the source_embeddings table."""
        engine_with_context._cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='source_embeddings'"
        )
        tables = engine_with_context._cursor.fetchall()
        assert len(tables) == 1

    def test_schema_version_is_3(self, engine_with_context):
        """Verify schema version is 3 after migration."""
        engine_with_context._cursor.execute(
            "SELECT MAX(version) as v FROM schema_migrations"
        )
        result = engine_with_context._cursor.fetchone()
        assert result["v"] == 3

    def test_reindex_source_sqlite_raises(self, engine_with_context):
        """reindex_source should raise NotImplementedError in SQLite mode."""
        source = engine_with_context.add_custom_source(
            name="Test", content="test content"
        )
        with pytest.raises(NotImplementedError, match="PostgreSQL"):
            engine_with_context.reindex_source(source.id)
