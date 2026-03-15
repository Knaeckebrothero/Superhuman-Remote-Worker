"""Tests for the tiered API key fallback system (KeyRing)."""

import threading
import time

import pytest

from src.llm.key_ring import KeyRing, clear_registry, get_or_create_key_ring, parse_key_string, _mask_key


# =============================================================================
# parse_key_string
# =============================================================================


class TestParseKeyString:
    def test_single_key(self):
        assert parse_key_string("sk-abc123") == ["sk-abc123"]

    def test_multiple_keys(self):
        assert parse_key_string("sk-key1,sk-key2,sk-key3") == [
            "sk-key1",
            "sk-key2",
            "sk-key3",
        ]

    def test_whitespace_handling(self):
        assert parse_key_string("  sk-key1 , sk-key2 , sk-key3  ") == [
            "sk-key1",
            "sk-key2",
            "sk-key3",
        ]

    def test_empty_entries_filtered(self):
        assert parse_key_string("sk-key1,,sk-key2,,,sk-key3") == [
            "sk-key1",
            "sk-key2",
            "sk-key3",
        ]

    def test_none_input(self):
        assert parse_key_string(None) == []

    def test_empty_string(self):
        assert parse_key_string("") == []

    def test_only_whitespace(self):
        assert parse_key_string("   ") == []

    def test_only_commas(self):
        assert parse_key_string(",,,") == []


# =============================================================================
# _mask_key
# =============================================================================


class TestMaskKey:
    def test_long_key(self):
        assert _mask_key("sk-abcdef1234567890") == "sk-abcde..."

    def test_short_key(self):
        assert _mask_key("sk-ab") == "sk-a..."

    def test_exact_eight(self):
        assert _mask_key("12345678") == "1234..."

    def test_longer_than_eight(self):
        assert _mask_key("123456789") == "12345678..."


# =============================================================================
# KeyRing Init
# =============================================================================


class TestKeyRingInit:
    def test_single_key(self):
        ring = KeyRing(["sk-abc"])
        assert ring.current_key == "sk-abc"
        assert ring.is_single_key
        assert not ring.has_alternatives

    def test_multiple_keys(self):
        ring = KeyRing(["sk-1", "sk-2", "sk-3"])
        assert ring.current_key == "sk-1"
        assert ring.has_alternatives
        assert not ring.is_single_key

    def test_empty_keys_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            KeyRing([])

    def test_custom_cooldown(self):
        ring = KeyRing(["sk-1"], cooldown_seconds=60)
        status = ring.get_status()
        assert status["cooldown_seconds"] == 60

    def test_custom_provider(self):
        ring = KeyRing(["sk-1"], provider="custom-api")
        status = ring.get_status()
        assert status["provider"] == "custom-api"


# =============================================================================
# KeyRing Rotation
# =============================================================================


class TestKeyRingRotation:
    def test_rotate_advances_to_next(self):
        ring = KeyRing(["sk-1", "sk-2", "sk-3"], cooldown_seconds=300)
        assert ring.current_key == "sk-1"

        new_key = ring.rotate("auth error")
        assert new_key == "sk-2"
        assert ring.current_key == "sk-2"

    def test_rotate_wraps_around_after_cooldown_expires(self):
        ring = KeyRing(["sk-1", "sk-2"], cooldown_seconds=0.01)
        ring.rotate("error")  # sk-1 on cooldown, now at sk-2
        assert ring.current_key == "sk-2"

        # Wait for cooldown to expire
        time.sleep(0.02)

        ring.rotate("error")  # sk-2 on cooldown, sk-1 should be available
        assert ring.current_key == "sk-1"

    def test_all_keys_exhausted_returns_none(self):
        ring = KeyRing(["sk-1", "sk-2"], cooldown_seconds=300)
        ring.rotate("error1")  # sk-1 on cooldown
        result = ring.rotate("error2")  # sk-2 on cooldown
        assert result is None

    def test_all_keys_exhausted_current_key_raises(self):
        ring = KeyRing(["sk-1", "sk-2"], cooldown_seconds=300)
        ring.rotate("error1")
        ring.rotate("error2")

        with pytest.raises(RuntimeError, match="all.*keys.*cooldown"):
            ring.current_key

    def test_single_key_rotation_returns_none(self):
        ring = KeyRing(["sk-only"], cooldown_seconds=300)
        result = ring.rotate("error")
        assert result is None

    def test_rotate_three_keys_sequential(self):
        ring = KeyRing(["sk-1", "sk-2", "sk-3"], cooldown_seconds=300)
        assert ring.rotate("err") == "sk-2"
        assert ring.rotate("err") == "sk-3"
        assert ring.rotate("err") is None  # all exhausted


# =============================================================================
# Cooldown Expiry & Priority Fallback
# =============================================================================


class TestCooldownExpiry:
    def test_expired_key_auto_recovers_to_higher_priority(self):
        """After cooldown expires, current_key should switch BACK to the
        higher-priority (earlier) key — not stay on the fallback."""
        ring = KeyRing(["sk-1", "sk-2"], cooldown_seconds=0.01)

        ring.rotate("error")
        assert ring.current_key == "sk-2"

        # Wait for sk-1's cooldown to expire
        time.sleep(0.02)

        # current_key should now prefer sk-1 again (higher priority)
        assert ring.current_key == "sk-1"

    def test_rotation_to_expired_cooldown_key(self):
        ring = KeyRing(["sk-1", "sk-2", "sk-3"], cooldown_seconds=0.01)

        ring.rotate("err")  # sk-1 on cooldown
        assert ring.current_key == "sk-2"

        time.sleep(0.02)  # sk-1 cooldown expires

        # current_key should prefer sk-1 (highest priority, cooldown expired)
        assert ring.current_key == "sk-1"

    def test_university_keys_before_personal_key(self):
        """Real-world scenario: 5 uni keys + 1 personal key.
        Exhaust all uni keys, land on personal, then uni key 1 recovers."""
        ring = KeyRing(
            ["sk-uni1", "sk-uni2", "sk-uni3", "sk-uni4", "sk-uni5", "sk-personal"],
            cooldown_seconds=0.01,
        )

        # Exhaust all 5 uni keys
        for i in range(5):
            ring.rotate(f"quota exhausted on uni key {i+1}")

        # Now on personal key
        assert ring.current_key == "sk-personal"

        # Wait for uni key cooldowns to expire
        time.sleep(0.02)

        # Should automatically switch back to sk-uni1 (highest priority)
        assert ring.current_key == "sk-uni1"

    def test_personal_key_not_used_while_uni_keys_available(self):
        """If only some uni keys are exhausted, should never reach personal."""
        ring = KeyRing(
            ["sk-uni1", "sk-uni2", "sk-uni3", "sk-personal"],
            cooldown_seconds=300,
        )

        ring.rotate("quota on uni1")
        # Should go to uni2, not personal
        assert ring.current_key == "sk-uni2"

        ring.rotate("quota on uni2")
        assert ring.current_key == "sk-uni3"


# =============================================================================
# Thread Safety
# =============================================================================


class TestThreadSafety:
    def test_concurrent_rotations(self):
        """Concurrent rotations should not corrupt state."""
        ring = KeyRing(
            ["sk-1", "sk-2", "sk-3", "sk-4", "sk-5"],
            cooldown_seconds=0.01,
        )
        errors = []
        results = []

        def rotate_worker():
            try:
                for _ in range(10):
                    result = ring.rotate("concurrent test")
                    results.append(result)
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=rotate_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        # Results should be either valid keys or None
        for r in results:
            assert r is None or r.startswith("sk-")

    def test_concurrent_current_key_access(self):
        """Concurrent reads of current_key should not crash."""
        ring = KeyRing(["sk-1", "sk-2", "sk-3"], cooldown_seconds=300)
        errors = []

        def reader():
            try:
                for _ in range(100):
                    key = ring.current_key
                    assert key.startswith("sk-")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# =============================================================================
# get_status Diagnostics
# =============================================================================


class TestGetStatus:
    def test_initial_status(self):
        ring = KeyRing(["sk-abc", "sk-def"], cooldown_seconds=1800, provider="test")
        status = ring.get_status()

        assert status["provider"] == "test"
        assert status["key_count"] == 2
        assert status["cooldown_seconds"] == 1800
        assert len(status["keys"]) == 2
        assert status["keys"][0]["active"] is True
        assert status["keys"][0]["status"] == "available"
        assert status["keys"][1]["active"] is False

    def test_status_after_rotation(self):
        ring = KeyRing(["sk-1", "sk-2"], cooldown_seconds=300)
        ring.rotate("test error")

        status = ring.get_status()
        assert status["keys"][0]["status"] == "cooldown"
        assert status["keys"][0]["cooldown_remaining_s"] > 0
        assert status["keys"][1]["status"] == "available"
        assert status["keys"][1]["active"] is True


# =============================================================================
# Singleton Registry
# =============================================================================


class TestRegistry:
    def setup_method(self):
        clear_registry()

    def teardown_method(self):
        clear_registry()

    def test_creates_new(self):
        ring = get_or_create_key_ring(["sk-1", "sk-2"], provider="test")
        assert isinstance(ring, KeyRing)

    def test_returns_same_instance(self):
        ring1 = get_or_create_key_ring(["sk-1", "sk-2"], provider="test")
        ring2 = get_or_create_key_ring(["sk-1", "sk-2"], provider="test")
        assert ring1 is ring2

    def test_different_providers_separate(self):
        ring1 = get_or_create_key_ring(["sk-1"], provider="openai")
        ring2 = get_or_create_key_ring(["sk-1"], provider="groq")
        assert ring1 is not ring2

    def test_clear_registry(self):
        ring1 = get_or_create_key_ring(["sk-1"], provider="test")
        clear_registry()
        ring2 = get_or_create_key_ring(["sk-1"], provider="test")
        assert ring1 is not ring2
