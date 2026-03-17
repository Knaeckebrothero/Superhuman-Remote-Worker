"""Embedding Service for generating text embeddings.

Used by RecallStore (Memory Light) for dense vector search.
Follows the same singleton pattern as VisionHelper.

Configuration via environment variables:
- EMBEDDING_API_KEY: API key (falls back to OPENAI_API_KEY)
- EMBEDDING_BASE_URL: API endpoint (defaults to OpenAI)
- EMBEDDING_MODEL: Model name (default: qwen3-embedding-8b)

Fallback: When OPENROUTER_API_KEY is set and the primary embedding server
is unreachable, requests automatically fall back to OpenRouter using the
same model. Since the model is identical, vectors are compatible.
"""

import logging
import os
from typing import List, Optional

from openai import APIConnectionError, AsyncOpenAI

logger = logging.getLogger(__name__)

# Module-level singleton
_embedding_service: Optional["EmbeddingService"] = None

# Errors that indicate the primary server is down (not an auth/input problem).
# APIConnectionError is the openai SDK's wrapper; APITimeoutError is a subclass of it.
_CONNECTIVITY_ERRORS = (APIConnectionError,)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class EmbeddingService:
    """Service for generating text embeddings via OpenAI-compatible API.

    When a fallback API key (OPENROUTER_API_KEY) is configured and the
    primary server is unreachable, requests are transparently retried
    against OpenRouter using the same model.

    Example:
        ```python
        service = get_embedding_service()
        vector = await service.embed("Hello world")
        vectors = await service.embed_batch(["Hello", "World"])
        ```
    """

    OPENAI_API_URL = "https://api.openai.com/v1"

    def __init__(self):
        """Initialize the Embedding Service with configuration from environment."""
        primary_key = os.getenv("OPENAI_API_KEY", "")

        self.api_key = os.getenv("EMBEDDING_API_KEY", primary_key)
        self.base_url = os.getenv("EMBEDDING_BASE_URL", self.OPENAI_API_URL)
        self.model = os.getenv("EMBEDDING_MODEL", "qwen3-embedding-8b")

        if not self.api_key:
            logger.warning(
                "No EMBEDDING_API_KEY or OPENAI_API_KEY set. "
                "Embedding calls will fail."
            )

        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        # Fallback client (OpenRouter) — only if key is available and
        # primary is not already pointing at OpenRouter
        self._fallback_client: AsyncOpenAI | None = None
        self._fallback_model: str | None = None
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if openrouter_key and OPENROUTER_BASE_URL not in self.base_url:
            self._fallback_client = AsyncOpenAI(
                api_key=openrouter_key,
                base_url=OPENROUTER_BASE_URL,
            )
            # OpenRouter uses provider-prefixed model names
            self._fallback_model = f"qwen/{self.model}"
            logger.info(
                f"EmbeddingService fallback configured "
                f"(model={self._fallback_model}, base_url={OPENROUTER_BASE_URL})"
            )

        logger.info(
            f"EmbeddingService initialized (model={self.model}, "
            f"base_url={self.base_url})"
        )

    async def embed(self, text: str) -> List[float]:
        """Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector as list of floats
        """
        try:
            response = await self._client.embeddings.create(
                input=text,
                model=self.model,
            )
            return response.data[0].embedding
        except _CONNECTIVITY_ERRORS as e:
            return await self._fallback_embed(text, e)

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts in one API call.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors (same order as input)
        """
        if not texts:
            return []

        try:
            response = await self._client.embeddings.create(
                input=texts,
                model=self.model,
            )
            sorted_data = sorted(response.data, key=lambda x: x.index)
            return [item.embedding for item in sorted_data]
        except _CONNECTIVITY_ERRORS as e:
            return await self._fallback_embed_batch(texts, e)

    # ── Fallback helpers ─────────────────────────────────────────────

    async def _fallback_embed(self, text: str, original_error: Exception) -> List[float]:
        """Retry a single embed against the fallback client."""
        if not self._fallback_client:
            raise original_error

        logger.warning(
            f"Primary embedding server unreachable ({type(original_error).__name__}), "
            f"falling back to OpenRouter"
        )
        response = await self._fallback_client.embeddings.create(
            input=text,
            model=self._fallback_model,
        )
        return response.data[0].embedding

    async def _fallback_embed_batch(
        self, texts: List[str], original_error: Exception
    ) -> List[List[float]]:
        """Retry a batch embed against the fallback client."""
        if not self._fallback_client:
            raise original_error

        logger.warning(
            f"Primary embedding server unreachable ({type(original_error).__name__}), "
            f"falling back to OpenRouter ({len(texts)} texts)"
        )
        response = await self._fallback_client.embeddings.create(
            input=texts,
            model=self._fallback_model,
        )
        sorted_data = sorted(response.data, key=lambda x: x.index)
        return [item.embedding for item in sorted_data]


def get_embedding_service() -> EmbeddingService:
    """Get or create the singleton EmbeddingService instance.

    Returns:
        EmbeddingService singleton
    """
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
