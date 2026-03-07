"""Embedding Service for generating text embeddings.

Used by RecallStore (Memory Light) for dense vector search.
Follows the same singleton pattern as VisionHelper.

Configuration via environment variables:
- EMBEDDING_API_KEY: API key (falls back to OPENAI_API_KEY)
- EMBEDDING_BASE_URL: API endpoint (defaults to OpenAI)
- EMBEDDING_MODEL: Model name (default: qwen3-embedding-8b)
"""

import logging
import os
from typing import List, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Module-level singleton
_embedding_service: Optional["EmbeddingService"] = None


class EmbeddingService:
    """Service for generating text embeddings via OpenAI-compatible API.

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
        # Sort by index to maintain input order
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
