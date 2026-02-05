# src/services/description_cache.py
"""
Description Cache for vision-generated content descriptions.

Provides a file-based cache for storing AI-generated descriptions of images
and document pages. Uses content-addressable storage with SHA256 keys based
on file content hash + page number + query.

Ported from Advanced-LLM-Chat/backend/services/cache/description_cache.py with changes:
- File-based storage instead of PostgreSQL
- Content-addressable keys (based on file content, not file_id)
- Synchronous API (matching tool signatures)
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default cache directory (global, shared across all jobs)
DEFAULT_CACHE_DIR = Path("workspace/.vision_cache")


class DescriptionCache:
    """
    File-based cache for vision-generated descriptions.

    Caches AI-generated descriptions of images and document pages to avoid
    repeated API calls. Uses content-addressable storage: the cache key is
    derived from the file's content hash, page number, and query.

    This means:
    - Same file content + page + query = same cache key (cache hit)
    - Modified file = different content hash = cache miss (correct behavior)
    - Same file, different query = different cache key (correct behavior)

    Cache files are stored as plain text in the cache directory:
    `{cache_dir}/{sha256_key}.txt`

    Example:
        ```python
        cache = DescriptionCache()

        # Check for cached description
        description = cache.get(Path("doc.pdf"), page=1)

        if description is None:
            # Generate description via vision model
            description = vision_helper.describe_document_page_sync(...)

            # Cache it for next time
            cache.set(Path("doc.pdf"), description, page=1)
        ```
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize the description cache.

        Args:
            cache_dir: Directory for cache files. Defaults to `workspace/.vision_cache/`.
                      Created automatically if it doesn't exist.
        """
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"DescriptionCache initialized: {self.cache_dir}")

    def _hash_file_content(self, file_path: Path) -> str:
        """Compute SHA256 hash of file contents.

        Args:
            file_path: Path to the file

        Returns:
            Hex-encoded SHA256 hash of file contents
        """
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            # Read in chunks for memory efficiency with large files
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _make_key(
        self,
        file_path: Path,
        page: Optional[int] = None,
        query: Optional[str] = None,
    ) -> str:
        """Create content-addressable cache key.

        The key is based on:
        - File content hash (so modified files get new keys)
        - Page number (for multi-page documents)
        - Query string (different questions = different descriptions)

        Args:
            file_path: Path to the file
            page: Page/slide number (1-indexed), or None for standalone images
            query: Optional query string used for the description

        Returns:
            SHA256 hash suitable for use as cache filename
        """
        content_hash = self._hash_file_content(file_path)

        key_data = {
            "content_hash": content_hash,
            "page": page,
            "query": query or "",
        }

        return hashlib.sha256(
            json.dumps(key_data, sort_keys=True).encode()
        ).hexdigest()

    def get(
        self,
        file_path: Path,
        page: Optional[int] = None,
        query: Optional[str] = None,
    ) -> Optional[str]:
        """Retrieve cached description if available.

        Args:
            file_path: Path to the original file
            page: Page/slide number (1-indexed), or None for images
            query: Optional query string used for the description

        Returns:
            Cached description string, or None if not found
        """
        try:
            key = self._make_key(file_path, page, query)
            cache_file = self.cache_dir / f"{key}.txt"

            if cache_file.exists():
                description = cache_file.read_text(encoding="utf-8")
                logger.debug(
                    f"Cache hit: {file_path.name}, page={page}, "
                    f"query={query[:30] + '...' if query and len(query) > 30 else query}"
                )
                return description

            logger.debug(f"Cache miss: {file_path.name}, page={page}")
            return None

        except Exception as e:
            logger.warning(f"Cache read error for {file_path}: {e}")
            return None

    def set(
        self,
        file_path: Path,
        description: str,
        page: Optional[int] = None,
        query: Optional[str] = None,
    ) -> bool:
        """Store description in cache.

        Args:
            file_path: Path to the original file
            description: AI-generated description to cache
            page: Page/slide number (1-indexed), or None for images
            query: Optional query string used for the description

        Returns:
            True if cached successfully, False on error
        """
        try:
            key = self._make_key(file_path, page, query)
            cache_file = self.cache_dir / f"{key}.txt"

            cache_file.write_text(description, encoding="utf-8")
            logger.debug(
                f"Cached: {file_path.name}, page={page}, "
                f"query={query[:30] + '...' if query and len(query) > 30 else query}"
            )
            return True

        except Exception as e:
            logger.warning(f"Cache write error for {file_path}: {e}")
            return False

    def clear(self) -> int:
        """Clear all cached descriptions.

        Returns:
            Number of cache entries deleted
        """
        count = 0
        try:
            for cache_file in self.cache_dir.glob("*.txt"):
                cache_file.unlink()
                count += 1
            logger.info(f"Cleared {count} cache entries")
        except Exception as e:
            logger.warning(f"Error clearing cache: {e}")
        return count

    def get_stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics:
            - entry_count: Number of cached descriptions
            - total_size_bytes: Total size of cache files
            - cache_dir: Path to cache directory
        """
        entries = list(self.cache_dir.glob("*.txt"))
        total_size = sum(f.stat().st_size for f in entries)

        return {
            "entry_count": len(entries),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "cache_dir": str(self.cache_dir),
        }


# Module-level singleton (lazy-loaded)
_description_cache: Optional[DescriptionCache] = None


def get_description_cache(cache_dir: Optional[Path] = None) -> DescriptionCache:
    """Get or create the DescriptionCache singleton instance.

    Args:
        cache_dir: Optional custom cache directory (only used on first call)

    Returns:
        Shared DescriptionCache instance
    """
    global _description_cache
    if _description_cache is None:
        _description_cache = DescriptionCache(cache_dir)
    return _description_cache
