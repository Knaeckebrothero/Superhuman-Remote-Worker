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

import logging
import os
from typing import List, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Module-level singleton
_embedding_service: Optional["EmbeddingService"] = None


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
            self.api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY", "")
            self.base_url = os.getenv("EMBEDDING_BASE_URL", self.OPENAI_API_URL)
            self.model = base_model

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

    async def embed(self, text: str) -> List[float]:
        """Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector as list of floats
        """
        response = await self._client.embeddings.create(
            input=text,
            model=self.model,
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts in one API call.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors (same order as input)
        """
        if not texts:
            return []

        response = await self._client.embeddings.create(
            input=texts,
            model=self.model,
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
