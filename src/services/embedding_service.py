"""Embedding Service for generating text embeddings.

Used by RecallStore (Memory Light) for dense vector search.
Follows the same singleton pattern as VisionHelper.

Provider-based configuration via environment variables:
- EMBEDDING_PROVIDER: Provider to use ("local" or "openrouter", default: "local")
- EMBEDDING_MODEL: Model name (default: qwen3-embedding-8b)

Provider-specific env vars:
- local:       EMBEDDING_BASE_URL, EMBEDDING_API_KEY (falls back to OPENAI_API_KEY)
- openrouter:  OPENROUTER_API_KEY

Per-account provider override is injected by the orchestrator dispatcher
via EMBEDDING_PROVIDER + the account's API key.
"""

import asyncio
import logging
import os
from typing import List, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Module-level singleton
_embedding_service: Optional["EmbeddingService"] = None


class EmbeddingDimensionError(RuntimeError):
    """Provider returned vectors that don't match the schema dimension.

    Every memory/KB write and dense query would fail at INSERT/cast with a
    raw DB error swallowed per call site, so the service latches degraded
    and fails fast instead (docs/issues/memory_bugs.md B4).
    """


class EmbeddingService:
    """Service for generating text embeddings via configurable provider.

    Supports multiple providers (local endpoint, OpenRouter) selected
    via the EMBEDDING_PROVIDER environment variable. Each provider
    resolves its own base URL and API key from the environment.

    Example:
        ```python
        service = get_embedding_service()
        vector = await service.embed("Hello world")
        vectors = await service.embed_batch(["Hello", "World"])
        ```
    """

    OPENAI_API_URL = "https://api.openai.com/v1"
    OPENROUTER_API_URL = "https://openrouter.ai/api/v1"

    def __init__(self):
        """Initialize the Embedding Service from environment configuration."""
        self.provider = os.getenv("EMBEDDING_PROVIDER", "local").lower()
        base_model = os.getenv("EMBEDDING_MODEL", "qwen3-embedding-8b")

        if self.provider == "openrouter":
            self.api_key = os.getenv("OPENROUTER_API_KEY", "")
            self.base_url = self.OPENROUTER_API_URL
            # OpenRouter uses provider-prefixed model names
            self.model = f"qwen/{base_model}" if "/" not in base_model else base_model
        else:
            # "local" provider (default) — custom endpoint or OpenAI
            self.api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv(
                "OPENAI_API_KEY", ""
            )
            self.base_url = os.getenv("EMBEDDING_BASE_URL", self.OPENAI_API_URL)
            self.model = base_model

        # Schema columns are vector(4096); a provider returning any other
        # dimensionality breaks every write/query. Override only together
        # with a schema re-dimension (memory overhaul Phase 6 territory).
        self.expected_dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "4096"))
        #: Set once a dimension mismatch is detected; latches for the
        #: process lifetime (a mismatch is config, not transient).
        self.degraded_reason: Optional[str] = None

        if not self.api_key:
            logger.warning(
                "No API key found for embedding provider '%s'. "
                "Embedding calls will fail.",
                self.provider,
            )

        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        logger.info(
            f"EmbeddingService initialized (provider={self.provider}, "
            f"model={self.model}, base_url={self.base_url})"
        )

    def _check_dimensions(self, vector: List[float]) -> None:
        """Latch degraded + raise if the provider's dimensionality is wrong."""
        if len(vector) == self.expected_dimensions:
            return
        self.degraded_reason = (
            f"provider '{self.provider}' model '{self.model}' returned "
            f"{len(vector)}-dim vectors; schema expects "
            f"vector({self.expected_dimensions})"
        )
        logger.error(
            "EMBEDDING DIMENSION MISMATCH: %s — every memory/KB write and "
            "dense query would fail at INSERT, so the dense path is disabled "
            "loudly instead. Fix EMBEDDING_MODEL/EMBEDDING_BASE_URL (or set "
            "EMBEDDING_DIMENSIONS alongside a schema re-dimension) and "
            "restart. See docs/issues/memory_bugs.md B4.",
            self.degraded_reason,
        )
        raise EmbeddingDimensionError(self.degraded_reason)

    def _fail_fast_if_degraded(self) -> None:
        if self.degraded_reason:
            raise EmbeddingDimensionError(self.degraded_reason)

    async def embed(self, text: str) -> List[float]:
        """Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector as list of floats

        Raises:
            EmbeddingDimensionError: provider dimensionality doesn't match
                the schema (latched — subsequent calls fail without I/O).
        """
        self._fail_fast_if_degraded()
        response = await self._client.embeddings.create(
            input=text,
            model=self.model,
        )
        vector = response.data[0].embedding
        self._check_dimensions(vector)
        return vector

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts in one API call.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors (same order as input)

        Raises:
            EmbeddingDimensionError: provider dimensionality doesn't match
                the schema (latched — subsequent calls fail without I/O).
        """
        self._fail_fast_if_degraded()
        if not texts:
            return []

        response = await self._client.embeddings.create(
            input=texts,
            model=self.model,
        )
        sorted_data = sorted(response.data, key=lambda x: x.index)
        vectors = [item.embedding for item in sorted_data]
        for vector in vectors:
            self._check_dimensions(vector)
        return vectors

    async def verify_dimensions(self, timeout: float = 20.0) -> Optional[bool]:
        """Probe the endpoint's dimensionality (B4 startup guard).

        Returns:
            True if the dimensionality matches, False on mismatch (degraded
            state is latched and the ERROR logged by the check itself), or
            None if the probe was inconclusive (endpoint unreachable /
            timeout) — connectivity issues are transient and must NOT latch.
        """
        try:
            await asyncio.wait_for(self.embed("dimension probe"), timeout=timeout)
            return True
        except EmbeddingDimensionError:
            return False
        except Exception as e:
            logger.warning(
                "Embedding dimension probe inconclusive (%s: %s) — endpoint "
                "unreachable? Dimensions will be checked on first real use.",
                type(e).__name__,
                e,
            )
            return None

    def health_snapshot(self) -> dict:
        """Status-endpoint view of the embedding path (see B4)."""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "expected_dimensions": self.expected_dimensions,
            "degraded": self.degraded_reason is not None,
            "degraded_reason": self.degraded_reason,
        }


def get_embedding_service() -> EmbeddingService:
    """Get or create the singleton EmbeddingService instance.

    Returns:
        EmbeddingService singleton
    """
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


def peek_embedding_service() -> Optional[EmbeddingService]:
    """Return the singleton if it already exists, without constructing it.

    For status surfacing: a status poll must not be the thing that
    instantiates (and logs) the service on agents that never use memory.
    """
    return _embedding_service
