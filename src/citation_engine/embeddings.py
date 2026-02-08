"""
Embedding Service for Citation Engine
======================================
OpenAI-compatible embedding API client for vector search.

Supports any OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, LiteLLM).

Environment Variables:
    CITATION_EMBEDDING_MODEL: Model name (default: text-embedding-3-small)
    CITATION_EMBEDDING_URL: API base URL (default: https://api.openai.com/v1)
    CITATION_EMBEDDING_KEY: API key (defaults to OPENAI_API_KEY)
"""

import logging
import os

log = logging.getLogger(__name__)

# Known dimensions for common models (avoids a probe call)
_KNOWN_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "all-MiniLM-L6-v2": 384,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "bge-large-en-v1.5": 1024,
    "bge-small-en-v1.5": 384,
}


class EmbeddingServiceError(Exception):
    """Raised when the embedding service encounters an error."""


class EmbeddingServiceNotConfigured(EmbeddingServiceError):
    """Raised when the embedding service is not configured."""


class EmbeddingService:
    """
    OpenAI-compatible embedding API client.

    Works with OpenAI, Ollama, vLLM, LiteLLM, and any other
    service that implements the /v1/embeddings endpoint.

    Usage:
        service = EmbeddingService()  # Uses env vars
        vector = service.embed("Hello world")
        vectors = service.embed_batch(["Hello", "World"])

    Environment Variables:
        CITATION_EMBEDDING_MODEL: Model name (default: text-embedding-3-small)
        CITATION_EMBEDDING_URL: Base URL (default: https://api.openai.com/v1)
        CITATION_EMBEDDING_KEY: API key (defaults to OPENAI_API_KEY)
    """

    def __init__(
        self,
        model: str | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
    ):
        self.model = model or os.getenv(
            "CITATION_EMBEDDING_MODEL", "text-embedding-3-small"
        )
        self.api_url = (
            api_url
            or os.getenv("CITATION_EMBEDDING_URL")
            or "https://api.openai.com/v1"
        )
        self.api_key = (
            api_key
            or os.getenv("CITATION_EMBEDDING_KEY")
            or os.getenv("OPENAI_API_KEY")
        )

        # Strip trailing slash from URL
        self.api_url = self.api_url.rstrip("/")

        # Validate configuration
        if not self.api_key and "api.openai.com" in self.api_url:
            raise EmbeddingServiceNotConfigured(
                "No API key configured for embedding service. "
                "Set CITATION_EMBEDDING_KEY or OPENAI_API_KEY, "
                "or set CITATION_EMBEDDING_URL to a local endpoint."
            )

        self._dimension: int | None = _KNOWN_DIMENSIONS.get(self.model)
        log.info(
            f"EmbeddingService configured: model={self.model}, "
            f"url={self.api_url}, dimension={self._dimension or 'unknown'}"
        )

    @property
    def dimension(self) -> int:
        """
        Get the embedding dimension for the configured model.

        On first access for unknown models, makes a probe request
        to determine the dimension.

        Returns:
            Embedding vector dimension

        Raises:
            EmbeddingServiceError: If dimension cannot be determined
        """
        if self._dimension is None:
            # Probe with a short text to learn dimension
            try:
                probe = self.embed("dimension probe")
                self._dimension = len(probe)
                log.info(f"Probed embedding dimension: {self._dimension}")
            except Exception as e:
                raise EmbeddingServiceError(
                    f"Cannot determine embedding dimension for model '{self.model}': {e}"
                ) from e
        return self._dimension

    def embed(self, text: str) -> list[float]:
        """
        Embed a single text string.

        Args:
            text: Text to embed

        Returns:
            Embedding vector as list of floats

        Raises:
            EmbeddingServiceError: If the API call fails
        """
        results = self.embed_batch([text])
        return results[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Embed multiple text strings in a single API call.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors (same order as input)

        Raises:
            EmbeddingServiceError: If the API call fails
        """
        if not texts:
            return []

        try:
            import httpx
        except ImportError:
            raise EmbeddingServiceError(
                "httpx is required for embedding service. "
                "Install with: pip install citation-engine[vector]"
            )

        url = f"{self.api_url}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "input": texts,
        }

        log.debug(f"Embedding {len(texts)} text(s) via {url}")

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            raise EmbeddingServiceError(
                f"Embedding API returned {e.response.status_code}: {e.response.text[:500]}"
            ) from e
        except httpx.RequestError as e:
            raise EmbeddingServiceError(
                f"Embedding API request failed: {e}"
            ) from e

        # Parse OpenAI-compatible response format:
        # {"data": [{"embedding": [...], "index": 0}, ...], "model": "...", "usage": {...}}
        embeddings_data = data.get("data", [])
        if len(embeddings_data) != len(texts):
            raise EmbeddingServiceError(
                f"Expected {len(texts)} embeddings, got {len(embeddings_data)}"
            )

        # Sort by index to ensure correct order
        embeddings_data.sort(key=lambda x: x.get("index", 0))
        results = [item["embedding"] for item in embeddings_data]

        # Cache dimension from first result
        if self._dimension is None and results:
            self._dimension = len(results[0])
            log.debug(f"Learned embedding dimension: {self._dimension}")

        return results

    @property
    def is_configured(self) -> bool:
        """Check if the service has enough configuration to make API calls."""
        # Local endpoints (Ollama, vLLM) don't need an API key
        if self.api_key:
            return True
        if self.api_url and "api.openai.com" not in self.api_url:
            return True
        return False
