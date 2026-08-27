"""Unit tests for src/services/image_downscale.py."""

from __future__ import annotations

import base64
import io
import os

import pytest
from PIL import Image

from src.services.image_downscale import (
    DEFAULT_FAMILY_MAX_EDGE_FALLBACK,
    downscale_image_b64,
    downscale_image_bytes,
    normalize_tier,
    resolve_max_edge,
)


def _encode(im: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _solid_jpeg(w: int, h: int, color=(120, 80, 40)) -> bytes:
    return _encode(Image.new("RGB", (w, h), color), "JPEG", quality=90)


def _noise_jpeg(w: int, h: int) -> bytes:
    im = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    return _encode(im, "JPEG", quality=95)


def _dims(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        return im.size


class TestResolveMaxEdge:
    def test_high_resolves_to_family_cap(self):
        assert resolve_max_edge("high", 2576) == 2576

    def test_standard_plateaus_below_large_cap(self):
        assert resolve_max_edge("standard", 2576) == 1568

    def test_economy_is_smallest(self):
        assert resolve_max_edge("economy", 2576) == 768

    def test_clamped_to_small_family_cap(self):
        # A family whose own cap is below the tier nominal wins.
        assert resolve_max_edge("standard", 512) == 512
        assert resolve_max_edge("high", 512) == 512

    def test_unknown_family_uses_default_fallback(self):
        assert resolve_max_edge("high", None) == DEFAULT_FAMILY_MAX_EDGE_FALLBACK
        assert resolve_max_edge("high", 0) == DEFAULT_FAMILY_MAX_EDGE_FALLBACK

    def test_non_numeric_family_cap_falls_back(self):
        # Must never raise on a stray non-int cap (config typo, mock, etc.)
        assert resolve_max_edge("high", "2576") == DEFAULT_FAMILY_MAX_EDGE_FALLBACK
        assert resolve_max_edge("high", object()) == DEFAULT_FAMILY_MAX_EDGE_FALLBACK

    def test_unknown_tier_defaults_to_standard(self):
        assert normalize_tier("bogus") == "standard"
        assert resolve_max_edge("bogus", 2576) == 1568


class TestDownscaleResolution:
    @pytest.mark.parametrize(
        "tier,cap,expected_edge",
        [("economy", 2576, 768), ("standard", 2576, 1568), ("high", 2576, 2576)],
    )
    def test_large_photo_downscaled_to_tier_edge(self, tier, cap, expected_edge):
        data = _solid_jpeg(3000, 2000)
        out, mime = downscale_image_bytes(
            data, "image/jpeg", resolve_max_edge(tier, cap)
        )
        assert mime == "image/jpeg"
        w, h = _dims(out)
        assert max(w, h) == expected_edge
        # aspect ratio preserved (3:2)
        assert abs((w / h) - 1.5) < 0.02

    def test_bytes_reduced_for_large_photo(self):
        data = _noise_jpeg(3000, 2000)
        out, _ = downscale_image_bytes(
            data, "image/jpeg", resolve_max_edge("standard", 2576)
        )
        assert len(out) < len(data)
        assert max(_dims(out)) == 1568


class TestUntouchedFastPath:
    def test_small_image_returned_identical(self):
        data = _solid_jpeg(256, 256)
        out, mime = downscale_image_bytes(data, "image/jpeg", 1568)
        assert out is data  # identity — untouched
        assert mime == "image/jpeg"

    def test_b64_wrapper_returns_same_string_when_untouched(self):
        b64 = base64.b64encode(_solid_jpeg(256, 256)).decode()
        out_b64, mime = downscale_image_b64(b64, "image/jpeg", 1568)
        assert out_b64 is b64
        assert mime == "image/jpeg"

    def test_max_edge_zero_is_noop(self):
        data = _solid_jpeg(4000, 4000)
        out, mime = downscale_image_bytes(data, "image/jpeg", 0)
        assert out is data


class TestFormatAware:
    def test_transparent_png_stays_png(self):
        im = Image.new("RGBA", (2400, 2400), (10, 20, 30, 128))
        data = _encode(im, "PNG")
        out, mime = downscale_image_bytes(data, "image/png", 1568)
        assert mime == "image/png"
        # alpha preserved (not flattened to JPEG)
        with Image.open(io.BytesIO(out)) as reopened:
            assert reopened.mode in ("RGBA", "LA") or "A" in reopened.getbands()
        assert max(_dims(out)) == 1568

    def test_opaque_png_screenshot_stays_png_not_jpeg(self):
        # No alpha, source PNG (a "screenshot"): must not become lossy JPEG.
        im = Image.new("RGB", (2400, 1200), (255, 255, 255))
        data = _encode(im, "PNG")
        out, mime = downscale_image_bytes(data, "image/png", 1568)
        assert mime == "image/png"
        assert max(_dims(out)) == 1568


class TestByteGuardWithoutResize:
    def test_over_threshold_within_edge_is_reencoded_smaller(self):
        # 1400px < 1568 max_edge (no resize), but big bytes -> byte guard fires.
        data = _noise_jpeg(1400, 1400)
        out, mime = downscale_image_bytes(
            data, "image/jpeg", 1568, byte_threshold=100_000
        )
        assert mime == "image/jpeg"
        assert max(_dims(out)) == 1400  # not resized
        assert len(out) < len(data)  # re-encoded smaller


class TestFailOpen:
    def test_corrupt_bytes_returned_unchanged(self):
        data = b"this is definitely not an image"
        out, mime = downscale_image_bytes(data, "image/jpeg", 1568)
        assert out is data
        assert mime == "image/jpeg"

    def test_corrupt_b64_returned_unchanged(self):
        out_b64, mime = downscale_image_b64("!!!not-base64!!!", "image/jpeg", 1568)
        assert out_b64 == "!!!not-base64!!!"
        assert mime == "image/jpeg"


class TestSeamIntegration:
    """The central seam (image_content.py) applies the tier when asked."""

    def _big_b64(self):
        return base64.b64encode(_solid_jpeg(3000, 2000)).decode()

    def test_block_downscales_when_max_edge_set(self):
        from src.services.image_content import make_image_content_block_from_b64

        block = make_image_content_block_from_b64(
            self._big_b64(), "image/jpeg", max_edge=1568
        )
        url = block["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        payload = base64.b64decode(url.split(",", 1)[1])
        assert max(_dims(payload)) == 1568

    def test_block_untouched_when_no_max_edge(self):
        from src.services.image_content import make_image_content_block_from_b64

        b64 = self._big_b64()
        block = make_image_content_block_from_b64(b64, "image/jpeg")
        assert block["image_url"]["url"].split(",", 1)[1] == b64  # unchanged

    def test_multimodal_message_downscales_each_image(self):
        from src.services.image_content import (
            ExtractedImage,
            make_multimodal_user_message,
        )

        imgs = [ExtractedImage(self._big_b64(), "image/jpeg") for _ in range(3)]
        msg = make_multimodal_user_message("look:", imgs, max_edge=768)
        blocks = [p for p in msg.content if p.get("type") == "image_url"]
        assert len(blocks) == 3
        for b in blocks:
            payload = base64.b64decode(b["image_url"]["url"].split(",", 1)[1])
            assert max(_dims(payload)) == 768


class _FakeLimits:
    def __init__(self, max_edge):
        self.image_tokens = {"max_edge": max_edge} if max_edge else None


class _FakeConfig:
    def __init__(self, tier, max_edge):
        self.image_quality = tier
        self.limits = _FakeLimits(max_edge)


class TestResolveImageMaxEdgeFromConfig:
    def test_high_uses_family_cap(self):
        from src.services.image_content import resolve_image_max_edge

        assert resolve_image_max_edge(_FakeConfig("high", 2576)) == 2576

    def test_standard_plateaus(self):
        from src.services.image_content import resolve_image_max_edge

        assert resolve_image_max_edge(_FakeConfig("standard", 2576)) == 1568

    def test_none_config_is_noop(self):
        from src.services.image_content import resolve_image_max_edge

        assert resolve_image_max_edge(None) is None

    def test_missing_family_cap_falls_back(self):
        from src.services.image_content import resolve_image_max_edge
        from src.services.image_downscale import DEFAULT_FAMILY_MAX_EDGE_FALLBACK

        # family declares no image_tokens.max_edge -> universal default
        assert (
            resolve_image_max_edge(_FakeConfig("high", None))
            == DEFAULT_FAMILY_MAX_EDGE_FALLBACK
        )
