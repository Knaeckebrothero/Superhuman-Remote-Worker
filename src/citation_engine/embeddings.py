"""
Embedding Service for Citation Engine
======================================
OpenAI-compatible embedding API client for vector search.

Supports multiple providers selected via ``EMBEDDING_PROVIDER``:
- ``local`` (default): Custom endpoint or OpenAI, configured via
  ``CITATION_EMBEDDING_URL`` / ``CITATION_EMBEDDING_KEY``
- ``openrouter``: OpenRouter API, configured via ``OPENROUTER_API_KEY``

Per-account provider override is injected by the orchestrator dispatcher.

Environment Variables:
    EMBEDDING_PROVIDER: Provider ("local" or "openrouter", default: "local")
    CITATION_EMBEDDING_MODEL: Model name (default: qwen3-embedding-8b)
    CITATION_EMBEDDING_URL: API base URL for local provider
    CITATION_EMBEDDING_KEY: API key for local provider (defaults to OPENAI_API_KEY)
    CITATION_EMBEDDING_BATCH_SIZE: Max texts per API batch (default: 2048)
    OPENROUTER_API_KEY: API key for OpenRouter provider
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)

# Known dimensions for common models (avoids a probe call)
_KNOWN_DIMENSIONS: dict[str, int] = {
    "qwen3-embedding-8b": 4096,
    "qwen3-embedding-0.6b": 1024,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "all-MiniLM-L6-v2": 384,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "bge-large-en-v1.5": 1024,
    "bge-small-en-v1.5": 384,
}

OPENROUTER_API_URL = "https://openrouter.ai/api/v1"


class EmbeddingServiceError(Exception):
    """Raised when the embedding service encounters an error."""


class EmbeddingServiceNotConfigured(EmbeddingServiceError):
    """Raised when the embedding service is not configured."""


class EmbeddingService:
    """
    OpenAI-compatible embedding API client with configurable providers.

    Supports ``local`` (any OpenAI-compatible endpoint) and ``openrouter``
    providers, selected via the ``EMBEDDING_PROVIDER`` environment variable.

    Usage:
        service = EmbeddingService()  # Uses env vars
        vector = service.embed("Hello world")
        vectors = service.embed_batch(["Hello", "World"])
    """

    def __init__(
        self,
        model: str | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
        max_tokens_per_batch: int = 250_000,
        max_texts_per_batch: int = 2048,
        max_tokens_per_text: int = 8191,
        timeout: float = 60.0,
    ):
        base_model = model or os.getenv("CITATION_EMBEDDING_MODEL", "qwen3-embedding-8b")

        # Explicit constructor args bypass provider logic (direct instantiation)
        if api_url:
            self.provider = "explicit"
            self.model = base_model
            self.api_url = api_url.rstrip("/")
            self.api_key = (
                api_key or os.getenv("CITATION_EMBEDDING_KEY") or os.getenv("OPENAI_API_KEY")
            )
        else:
            self.provider = os.getenv("EMBEDDING_PROVIDER", "local").lower()

            if self.provider == "openrouter":
                self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
                self.api_url = OPENROUTER_API_URL
                self.model = f"qwen/{base_model}" if "/" not in base_model else base_model
            else:
                # "local" provider (default)
                self.api_url = os.getenv("CITATION_EMBEDDING_URL") or "https://api.openai.com/v1"
                self.api_key = (
                    api_key or os.getenv("CITATION_EMBEDDING_KEY") or os.getenv("OPENAI_API_KEY")
                )
                self.model = base_model

            self.api_url = self.api_url.rstrip("/")

        # Validate configuration
        if not self.api_key and "api.openai.com" in self.api_url:
            raise EmbeddingServiceNotConfigured(
                "No API key configured for embedding service. "
                "Set CITATION_EMBEDDING_KEY or OPENAI_API_KEY, "
                "or set CITATION_EMBEDDING_URL to a local endpoint."
            )

        self.max_tokens_per_batch = max_tokens_per_batch
        self.max_texts_per_batch = int(
            os.getenv("CITATION_EMBEDDING_BATCH_SIZE", str(max_texts_per_batch))
        )
        self.max_tokens_per_text = max_tokens_per_text
        self.timeout = timeout

        # Try to load tiktoken for accurate token counting
        self._encoding = None
        try:
            import tiktoken

            self._encoding = tiktoken.encoding_for_model(self.model)
            log.debug(f"Using tiktoken encoding for model {self.model}")
        except (ImportError, KeyError):
            log.debug(f"tiktoken unavailable for model {self.model}, using heuristic")

        # Dimension lookup uses the unprefixed model name
        self._dimension: int | None = _KNOWN_DIMENSIONS.get(base_model)

        log.info(
            f"EmbeddingService configured: provider={self.provider}, "
            f"model={self.model}, url={self.api_url}, "
            f"dimension={self._dimension or 'unknown'}"
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

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count. Uses tiktoken if available, else chars // 2."""
        if self._encoding is not None:
            return max(1, len(self._encoding.encode(text)))
        return max(1, len(text) // 2)

    def _compute_batches(self, texts: list[str]) -> list[list[int]]:
        """
        Partition text indices into sub-batches respecting token and count limits.

        Returns:
            List of index groups, e.g. [[0,1,2], [3,4]] for two batches.
        """
        if not texts:
            return []

        batches: list[list[int]] = []
        current_batch: list[int] = []
        current_tokens = 0

        for i, text in enumerate(texts):
            tokens = self._estimate_tokens(text)

            # Start new batch if adding this text would exceed limits
            if current_batch and (
                current_tokens + tokens > self.max_tokens_per_batch
                or len(current_batch) >= self.max_texts_per_batch
            ):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            current_batch.append(i)
            current_tokens += tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    def _embed_single_batch(
        self,
        texts: list[str],
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
    ) -> list[list[float]]:
        """
        Send a single embedding API request for a batch of texts.

        Returns:
            List of embedding vectors in input order.
        """
        import httpx

        payload = {
            "model": self.model,
            "input": texts,
        }

        try:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            raise EmbeddingServiceError(
                f"Embedding API returned {e.response.status_code}: {e.response.text[:500]}"
            ) from e
        except httpx.RequestError as e:
            raise EmbeddingServiceError(f"Embedding API request failed: {e}") from e

        embeddings_data = data.get("data", [])
        if len(embeddings_data) != len(texts):
            raise EmbeddingServiceError(
                f"Expected {len(texts)} embeddings, got {len(embeddings_data)}"
            )

        # Sort by index to ensure correct order
        embeddings_data.sort(key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in embeddings_data]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Embed multiple text strings, automatically batching to stay within API limits.

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
            ) from None

        url = f"{self.api_url}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Truncate texts that exceed per-text token limit
        if self.max_tokens_per_text:
            truncated = False
            for i, text in enumerate(texts):
                if self._estimate_tokens(text) > self.max_tokens_per_text:
                    if not truncated:
                        texts = list(texts)  # copy on first mutation
                        truncated = True
                    log.warning(
                        f"Text {i} exceeds embedding token limit "
                        f"(~{self._estimate_tokens(text)} > {self.max_tokens_per_text}), truncating"
                    )
                    if self._encoding is not None:
                        tokens = self._encoding.encode(text)
                        texts[i] = self._encoding.decode(tokens[: self.max_tokens_per_text])
                    else:
                        texts[i] = text[: self.max_tokens_per_text * 2]

        batches = self._compute_batches(texts)
        log.debug(f"Embedding {len(texts)} text(s) in {len(batches)} batch(es) via {url}")

        results: list[list[float] | None] = [None] * len(texts)

        with httpx.Client(timeout=self.timeout) as client:
            for batch_indices in batches:
                batch_texts = [texts[i] for i in batch_indices]
                batch_results = self._embed_single_batch(batch_texts, client, url, headers)
                for idx, embedding in zip(batch_indices, batch_results, strict=True):
                    results[idx] = embedding

        # Cache dimension from first result
        if self._dimension is None and results:
            self._dimension = len(results[0])
            log.debug(f"Learned embedding dimension: {self._dimension}")

        return results  # type: ignore[return-value]

    @property
    def is_configured(self) -> bool:
        """Check if the service has enough configuration to make API calls."""
        # Local endpoints (Ollama, vLLM) don't need an API key
        if self.api_key:
            return True
        if self.api_url and "api.openai.com" not in self.api_url:
            return True
        return False
