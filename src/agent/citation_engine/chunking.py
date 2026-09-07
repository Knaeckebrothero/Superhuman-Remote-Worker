"""
Semantic Chunking for Citation Engine
======================================
Splits text into semantically coherent chunks using embedding similarity.

When the embedding service is unavailable, falls back to fixed-size splitting.
"""

import logging
import math
import re

log = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _split_sentences(text: str) -> list[str]:
    """
    Split text into sentences using regex.

    Handles common abbreviations and decimal numbers to avoid false splits.
    """
    # Split on sentence-ending punctuation followed by whitespace and uppercase
    # or on newline boundaries that look like paragraph breaks
    sentences = re.split(
        r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])|(?<=\n)\s*\n+",
        text.strip(),
    )
    # Filter out empty strings and strip whitespace
    return [s.strip() for s in sentences if s.strip()]


class SemanticChunker:
    """
    Splits text into semantically coherent chunks using embedding similarity.

    Algorithm:
    1. Split text into sentences
    2. Create overlapping windows of N sentences
    3. Embed each window
    4. Compute cosine similarity between adjacent windows
    5. Split where similarity drops below threshold
    6. Merge very small chunks with neighbors

    Falls back to fixed-size splitting if the embedding service is unavailable.

    The embedder is dependency-injected (not imported here): pass any object
    exposing ``embed_batch(list[str]) -> list[list[float]]`` — e.g. SRW's
    ``get_embedding_service()``. Semantic mode is currently inactive pending an
    async chunker (see the citation engine integration doc follow-ups); with no
    embedder the chunker uses the fixed-size fallback.

    Usage:
        # Fixed-size fallback (no embedder):
        chunker = SemanticChunker()
        chunks = chunker.chunk("Long document text...")

        # Semantic mode (inject an embedder exposing embed_batch):
        chunker = SemanticChunker(embedding_service=embedder)
        chunks = chunker.chunk("Long document text...")
    """

    def __init__(
        self,
        embedding_service=None,
        similarity_threshold: float = 0.5,
        window_size: int = 3,
        min_chunk_size: int = 100,
        max_chunk_size: int = 2000,
    ):
        """
        Args:
            embedding_service: EmbeddingService instance (None for fixed-size fallback)
            similarity_threshold: Split where similarity drops below this (0.0-1.0)
            window_size: Number of sentences per embedding window
            min_chunk_size: Minimum characters per chunk (smaller chunks get merged)
            max_chunk_size: Maximum characters per chunk (larger chunks get split)
        """
        self._embedding_service = embedding_service
        self.similarity_threshold = similarity_threshold
        self.window_size = window_size
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size

    def chunk(self, text: str) -> list[str]:
        """
        Split text into semantically coherent chunks.

        Args:
            text: The text to chunk

        Returns:
            List of text chunks
        """
        if not text or not text.strip():
            return []

        # Short text: return as single chunk
        if len(text) <= self.max_chunk_size:
            return [text.strip()]

        # Try semantic chunking first
        if self._embedding_service is not None:
            try:
                return self._semantic_chunk(text)
            except Exception as e:
                log.warning(
                    f"Semantic chunking failed, falling back to fixed-size: {e}"
                )

        # Fallback: fixed-size splitting
        return self._fixed_chunk(text)

    def _split_long_sentences(self, sentences: list[str]) -> list[str]:
        """
        Sub-split sentences that are too long for embedding windows.

        A window of `window_size` sentences must fit within the embedding model's
        token limit. If any single sentence exceeds its fair share, split it using
        fixed-size chunking to keep all windows embeddable.
        """
        if not self._embedding_service:
            return sentences

        max_tokens = getattr(self._embedding_service, "max_tokens_per_text", 8191)
        # Each sentence gets an equal share of the window's token budget
        max_chars_per_sentence = (max_tokens * 3) // self.window_size

        result = []
        for sentence in sentences:
            if len(sentence) > max_chars_per_sentence:
                # Split oversized sentence into smaller pieces
                sub_chunks = self._fixed_chunk(
                    sentence,
                    chunk_size=max_chars_per_sentence,
                    overlap=64,
                )
                result.extend(sub_chunks)
            else:
                result.append(sentence)
        return result

    def _semantic_chunk(self, text: str) -> list[str]:
        """Chunk using embedding similarity between sentence windows."""
        sentences = _split_sentences(text)
        sentences = self._split_long_sentences(sentences)

        if len(sentences) <= self.window_size:
            return self._fixed_chunk(text)

        # Create overlapping windows
        windows = []
        for i in range(len(sentences) - self.window_size + 1):
            window_text = " ".join(sentences[i : i + self.window_size])
            windows.append(window_text)

        # Embed all windows in one batch
        embeddings = self._embedding_service.embed_batch(windows)

        # Compute similarities between adjacent windows
        similarities = []
        for i in range(len(embeddings) - 1):
            sim = _cosine_similarity(embeddings[i], embeddings[i + 1])
            similarities.append(sim)

        # Find split points where similarity drops below threshold
        split_indices = []
        for i, sim in enumerate(similarities):
            if sim < self.similarity_threshold:
                # The split happens after window i, which corresponds to
                # after sentence (i + window_size - 1) in the original list
                split_sentence_idx = i + self.window_size
                if split_sentence_idx < len(sentences):
                    split_indices.append(split_sentence_idx)

        # Build chunks from split points
        chunks = []
        prev_idx = 0
        for idx in split_indices:
            chunk_text = " ".join(sentences[prev_idx:idx]).strip()
            if chunk_text:
                chunks.append(chunk_text)
            prev_idx = idx

        # Add remaining sentences as last chunk
        if prev_idx < len(sentences):
            chunk_text = " ".join(sentences[prev_idx:]).strip()
            if chunk_text:
                chunks.append(chunk_text)

        # Merge small chunks with neighbors
        chunks = self._merge_small_chunks(chunks)

        # Split oversized chunks
        final_chunks = []
        for chunk in chunks:
            if len(chunk) > self.max_chunk_size:
                final_chunks.extend(self._fixed_chunk(chunk))
            else:
                final_chunks.append(chunk)

        return final_chunks if final_chunks else [text.strip()]

    def _merge_small_chunks(self, chunks: list[str]) -> list[str]:
        """Merge chunks smaller than min_chunk_size with their neighbors."""
        if len(chunks) <= 1:
            return chunks

        merged = [chunks[0]]
        for chunk in chunks[1:]:
            if len(merged[-1]) < self.min_chunk_size:
                # Merge with previous
                merged[-1] = merged[-1] + " " + chunk
            elif len(chunk) < self.min_chunk_size:
                # Merge small chunk into previous
                merged[-1] = merged[-1] + " " + chunk
            else:
                merged.append(chunk)

        return merged

    def _fixed_chunk(
        self,
        text: str,
        chunk_size: int = 512,
        overlap: int = 64,
    ) -> list[str]:
        """
        Fixed-size character splitting with overlap.

        Tries to split on sentence boundaries within the target window.
        """
        if len(text) <= chunk_size:
            return [text.strip()]

        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size

            if end < len(text):
                # Try to find a sentence boundary near the end
                boundary = text.rfind(". ", start + chunk_size // 2, end)
                if boundary == -1:
                    boundary = text.rfind("\n", start + chunk_size // 2, end)
                if boundary != -1:
                    end = boundary + 1

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            # Advance with overlap
            start = end - overlap if end < len(text) else end

        return chunks
