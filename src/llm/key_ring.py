"""Tiered API key fallback with cooldown-based rotation.

Provides automatic key rotation for OpenAI-compatible providers. When a key
fails with auth/quota errors, the KeyRing advances to the next available key.
Exhausted keys enter a cooldown period and become available again after the
cooldown expires (e.g., daily quota resets).

Usage:
    # Single key (no rotation, backward compatible):
    ring = KeyRing(["sk-abc123"])

    # Multiple keys with 30-minute cooldown:
    ring = KeyRing(["sk-key1", "sk-key2", "sk-key3"], cooldown_seconds=1800)

    # Get current key:
    key = ring.current_key  # Returns active key, auto-skips cooled-down ones

    # Rotate on failure:
    new_key = ring.rotate("quota exceeded")  # Returns next key or None

Keys are masked in log output (first 8 chars + "...") to prevent leaking.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _mask_key(key: str) -> str:
    """Mask an API key for safe logging. Shows first 8 chars only."""
    if len(key) <= 8:
        return key[:4] + "..."
    return key[:8] + "..."


def parse_key_string(raw: Optional[str]) -> List[str]:
    """Parse a comma-separated key string into a list of keys.

    Handles whitespace, empty entries, and None input.

    Args:
        raw: Comma-separated API keys, e.g. "key1, key2, key3"

    Returns:
        List of non-empty key strings. Empty list if input is None/empty.

    Examples:
        >>> parse_key_string("sk-abc, sk-def")
        ['sk-abc', 'sk-def']
        >>> parse_key_string("single-key")
        ['single-key']
        >>> parse_key_string(None)
        []
        >>> parse_key_string("  key1 ,, key2 , ")
        ['key1', 'key2']
    """
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


class KeyRing:
    """Thread-safe API key manager with cooldown-based rotation.

    Manages a pool of API keys and rotates through them when failures occur.
    Keys that fail are placed on cooldown and become available again after
    the cooldown period expires (useful for daily quota resets).

    Args:
        keys: List of API keys (at least one required)
        cooldown_seconds: How long a failed key stays unavailable (default: 1800 = 30 min)
        provider: Provider name for logging (default: "openai")

    Raises:
        ValueError: If keys list is empty
    """

    def __init__(
        self,
        keys: List[str],
        cooldown_seconds: float = 1800.0,
        provider: str = "openai",
    ):
        if not keys:
            raise ValueError("KeyRing requires at least one API key")

        self._keys = list(keys)
        self._cooldown_seconds = cooldown_seconds
        self._provider = provider
        self._lock = threading.Lock()

        # Index of the currently active key
        self._current_index = 0

        # Maps key index -> timestamp when cooldown expires (0 = not on cooldown)
        self._cooldown_until: Dict[int, float] = {}

        if len(keys) > 1:
            logger.info(
                f"KeyRing[{provider}]: initialized with {len(keys)} keys "
                f"(cooldown={cooldown_seconds}s). "
                f"Active: {_mask_key(keys[0])}"
            )
        else:
            logger.debug(f"KeyRing[{provider}]: single key mode ({_mask_key(keys[0])})")

    @property
    def current_key(self) -> str:
        """Return the highest-priority available key.

        Always prefers earlier keys (lower index) over later ones. If an earlier
        key's cooldown has expired, switches back to it automatically. This
        ensures "cheap" keys (e.g., university quotas) are always preferred over
        "expensive" fallback keys (e.g., personal API key) listed later.

        Returns:
            The active API key string.

        Raises:
            RuntimeError: If all keys are on cooldown (none available).
        """
        with self._lock:
            return self._get_available_key()

    def _get_available_key(self) -> str:
        """Find the highest-priority (lowest-index) available key. Must hold lock."""
        now = time.monotonic()

        # Always scan from index 0 to prefer earlier keys.
        # If a higher-priority key's cooldown has expired, switch back to it.
        for idx in range(len(self._keys)):
            expiry = self._cooldown_until.get(idx, 0)
            if expiry == 0 or now >= expiry:
                # Clear expired cooldown
                if expiry and now >= expiry:
                    del self._cooldown_until[idx]
                    if idx != self._current_index:
                        logger.info(
                            f"KeyRing[{self._provider}]: key {_mask_key(self._keys[idx])} "
                            f"cooldown expired, switching back (priority)"
                        )

                if idx != self._current_index:
                    old = _mask_key(self._keys[self._current_index])
                    new = _mask_key(self._keys[idx])
                    if idx < self._current_index:
                        logger.info(
                            f"KeyRing[{self._provider}]: "
                            f"preferring higher-priority key {new} over {old}"
                        )
                    self._current_index = idx
                return self._keys[idx]

        # All keys on cooldown
        raise RuntimeError(
            f"KeyRing[{self._provider}]: all {len(self._keys)} keys are on cooldown. "
            f"Next available in {self._time_until_next_available():.0f}s"
        )

    def _time_until_next_available(self) -> float:
        """Seconds until the next key becomes available. Must hold lock."""
        now = time.monotonic()
        if not self._cooldown_until:
            return 0.0
        earliest = min(self._cooldown_until.values())
        return max(0.0, earliest - now)

    def rotate(self, reason: str = "unknown") -> Optional[str]:
        """Mark the current key as failed and rotate to the next available key.

        The failed key enters cooldown for `cooldown_seconds`. If no alternative
        keys are available (all on cooldown), returns None.

        Args:
            reason: Why the key failed (for logging)

        Returns:
            The next available API key, or None if all keys are exhausted.
        """
        with self._lock:
            failed_key = self._keys[self._current_index]
            failed_idx = self._current_index

            # Put failed key on cooldown
            self._cooldown_until[failed_idx] = time.monotonic() + self._cooldown_seconds

            logger.warning(
                f"KeyRing[{self._provider}]: rotating away from {_mask_key(failed_key)} "
                f"(reason: {reason}, cooldown: {self._cooldown_seconds}s)"
            )

            # Advance to next index
            self._current_index = (self._current_index + 1) % len(self._keys)

            # Try to find an available key
            try:
                new_key = self._get_available_key()
                logger.info(
                    f"KeyRing[{self._provider}]: rotated to {_mask_key(new_key)}"
                )
                return new_key
            except RuntimeError:
                logger.error(
                    f"KeyRing[{self._provider}]: all keys exhausted after rotation. "
                    f"Next available in {self._time_until_next_available():.0f}s"
                )
                return None

    @property
    def has_alternatives(self) -> bool:
        """True if there are multiple keys (rotation is possible)."""
        return len(self._keys) > 1

    @property
    def is_single_key(self) -> bool:
        """True if only one key is configured (no fallback possible)."""
        return len(self._keys) == 1

    def get_status(self) -> Dict:
        """Get diagnostic status of all keys.

        Returns:
            Dict with key count, active key (masked), and per-key status.
        """
        with self._lock:
            now = time.monotonic()
            key_statuses = []
            for i, key in enumerate(self._keys):
                expiry = self._cooldown_until.get(i, 0)
                if expiry == 0 or now >= expiry:
                    status = "available"
                    remaining = 0
                else:
                    status = "cooldown"
                    remaining = expiry - now

                key_statuses.append(
                    {
                        "key": _mask_key(key),
                        "status": status,
                        "active": i == self._current_index,
                        "cooldown_remaining_s": round(remaining, 1),
                    }
                )

            return {
                "provider": self._provider,
                "key_count": len(self._keys),
                "active_key": _mask_key(self._keys[self._current_index]),
                "cooldown_seconds": self._cooldown_seconds,
                "keys": key_statuses,
            }


# =============================================================================
# Module-Level Singleton Registry
# =============================================================================

_registry_lock = threading.Lock()
_registry: Dict[tuple, KeyRing] = {}


def get_or_create_key_ring(
    keys: List[str],
    provider: str = "openai",
    cooldown_seconds: float = 1800.0,
) -> KeyRing:
    """Get or create a shared KeyRing for a provider + key set.

    Cache key is ``(provider, tuple(keys))`` so phase LLMs that share both
    provider and keys share one ring (rotation state stays consistent), while
    a different key set — e.g. an explicit ``api_key`` from a session
    config_override — gets its own ring instead of being masked by an
    earlier ring cached against the same provider with a different key.

    Args:
        keys: List of API keys
        provider: Provider identifier (part of registry key)
        cooldown_seconds: Cooldown duration for failed keys

    Returns:
        Shared KeyRing instance for this (provider, keys) pair
    """
    cache_key = (provider, tuple(keys))
    with _registry_lock:
        if cache_key in _registry:
            return _registry[cache_key]

        ring = KeyRing(keys, cooldown_seconds=cooldown_seconds, provider=provider)
        _registry[cache_key] = ring
        return ring


def clear_registry():
    """Clear all cached KeyRing instances. For testing only."""
    with _registry_lock:
        _registry.clear()
