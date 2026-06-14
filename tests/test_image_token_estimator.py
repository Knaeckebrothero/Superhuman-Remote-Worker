"""Tests for the per-family image-token estimator (context_token_accounting.md S4).

Covers:
- src.core.image_tokens dimension reader (PNG IHDR / JPEG SOF) + per-mode
  formulas (openai_patches / openai_tiles / anthropic_patches / flat),
  asserted against the primary-source-verified worked examples for a
  1700x2200 px page.
- estimate_image_block_tokens dispatch + biased-high fallbacks.
- loader routing of settings.image_tokens -> limits.image_tokens.
- ContextManager.get_token_count using a resolved per-family config.
"""

import base64
import struct

import pytest
from langchain_core.messages import HumanMessage

from src.core.context import ContextConfig, ContextManager
from src.core.image_tokens import (
    DEFAULT_IMAGE_TOKENS,
    estimate_image_block_tokens,
    read_image_dimensions,
)

# Resolved per-family configs (mirror config/model_config_matrix.yaml).
GPT5 = {"mode": "openai_patches", "patch_px": 32, "budget": 10000, "flat": 3000}
CLAUDE_LEGACY = {
    "mode": "anthropic_patches",
    "patch_px": 28,
    "max_edge": 1568,
    "max_tokens": 1568,
    "flat": 1568,
}
CLAUDE_OPUS = {
    "mode": "anthropic_patches",
    "patch_px": 28,
    "max_edge": 2576,
    "max_tokens": 4784,
    "flat": 4784,
}
GPT4O = {
    "mode": "openai_tiles",
    "base": 85,
    "per_tile": 170,
    "tile_px": 512,
    "flat": 1105,
}
OSERIES = {
    "mode": "openai_tiles",
    "base": 75,
    "per_tile": 150,
    "tile_px": 512,
    "flat": 1000,
}
GEMINI = {"mode": "flat", "flat": 2304}


def _png_b64(w: int, h: int) -> str:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = (
        b"\x00\x00\x00\x0d"
        + b"IHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00"
    )
    return base64.b64encode(sig + ihdr).decode()


def _jpeg_b64(w: int, h: int, with_app0: bool = False) -> str:
    data = b"\xff\xd8"  # SOI
    if with_app0:
        # APP0 (JFIF) segment the SOF scan must skip over.
        data += b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    data += (
        b"\xff\xc0\x00\x11\x08"
        + struct.pack(">H", h)
        + struct.pack(">H", w)
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return base64.b64encode(data).decode()


def _img_block(b64: str, mime: str = "image/png") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _png_block(w: int, h: int) -> dict:
    return _img_block(_png_b64(w, h))


# =============================================================================
# Dimension reader
# =============================================================================


class TestReadImageDimensions:
    def test_png(self):
        assert read_image_dimensions(_png_block(1700, 2200)) == (1700, 2200)

    def test_png_square(self):
        assert read_image_dimensions(_png_block(1000, 1000)) == (1000, 1000)

    def test_jpeg(self):
        assert read_image_dimensions(_img_block(_jpeg_b64(640, 480), "image/jpeg")) == (
            640,
            480,
        )

    def test_jpeg_skips_app0_segment(self):
        block = _img_block(_jpeg_b64(1280, 720, with_app0=True), "image/jpeg")
        assert read_image_dimensions(block) == (1280, 720)

    def test_anthropic_source_shape(self):
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": _png_b64(800, 600),
            },
        }
        assert read_image_dimensions(block) == (800, 600)

    def test_responses_api_string_url(self):
        block = {
            "type": "input_image",
            "image_url": f"data:image/png;base64,{_png_b64(512, 512)}",
        }
        assert read_image_dimensions(block) == (512, 512)

    def test_unreadable_returns_none(self):
        assert read_image_dimensions(_img_block("not-valid-base64!!!")) is None

    def test_remote_url_returns_none(self):
        assert (
            read_image_dimensions(
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
            )
            is None
        )


# =============================================================================
# Per-mode formulas — verified worked examples (1700x2200 px page)
# =============================================================================


class TestModeFormulas:
    BLOCK = None  # set in setup

    @pytest.fixture(autouse=True)
    def _block(self):
        TestModeFormulas.BLOCK = _png_block(1700, 2200)

    def test_openai_patches_gpt5(self):
        # raw 54*69 = 3726 <= budget 10000 -> 3726 patches x1.0
        assert estimate_image_block_tokens(self.BLOCK, GPT5) == 3726

    def test_openai_patches_shrinks_over_budget(self):
        # budget 1536 (mini class) forces a shrink; lands at/under budget.
        cfg = {"mode": "openai_patches", "patch_px": 32, "budget": 1536, "flat": 2000}
        out = estimate_image_block_tokens(self.BLOCK, cfg)
        assert out <= 1536

    def test_openai_patches_multiplier(self):
        cfg = {**GPT5, "budget": 1536, "multiplier": 1.62}
        # ~1496 patches * 1.62 ~= 2424
        out = estimate_image_block_tokens(self.BLOCK, cfg)
        assert 2300 <= out <= 2500

    def test_anthropic_legacy_cap(self):
        assert estimate_image_block_tokens(self.BLOCK, CLAUDE_LEGACY) == 1496

    def test_anthropic_legacy_respects_cap(self):
        assert estimate_image_block_tokens(self.BLOCK, CLAUDE_LEGACY) <= 1568

    def test_anthropic_opus_highres(self):
        assert estimate_image_block_tokens(self.BLOCK, CLAUDE_OPUS) == 4758

    def test_anthropic_opus_respects_cap(self):
        assert estimate_image_block_tokens(self.BLOCK, CLAUDE_OPUS) <= 4784

    def test_anthropic_small_image_no_resize(self):
        # 1000x1000 -> 36*36 = 1296 (Anthropic's own documented example).
        block = _png_block(1000, 1000)
        assert estimate_image_block_tokens(block, CLAUDE_LEGACY) == 1296

    def test_openai_tiles_gpt4o(self):
        assert estimate_image_block_tokens(self.BLOCK, GPT4O) == 765

    def test_openai_tiles_oseries(self):
        assert estimate_image_block_tokens(self.BLOCK, OSERIES) == 675

    def test_flat_does_not_read_dims(self):
        # Flat mode returns the constant even when dims are unreadable.
        assert estimate_image_block_tokens(_img_block("garbage!"), GEMINI) == 2304


# =============================================================================
# Dispatch + fallbacks
# =============================================================================


class TestDispatchFallbacks:
    def test_none_config_is_default_flat(self):
        assert (
            estimate_image_block_tokens(_png_block(1700, 2200), None)
            == DEFAULT_IMAGE_TOKENS
        )

    def test_unknown_mode_falls_to_flat(self):
        cfg = {"mode": "martian", "flat": 999}
        assert estimate_image_block_tokens(_png_block(1700, 2200), cfg) == 999

    def test_unreadable_dims_fall_to_family_flat(self):
        # patch mode but dims can't be read -> the family flat, not str(content).
        assert estimate_image_block_tokens(_img_block("garbage!"), GPT5) == 3000

    def test_flat_missing_uses_default(self):
        assert estimate_image_block_tokens(
            _img_block("garbage!"), {"mode": "openai_patches"}
        ) == (DEFAULT_IMAGE_TOKENS)


# =============================================================================
# Loader routing: settings.image_tokens -> limits.image_tokens
# =============================================================================


class TestLoaderRouting:
    def test_gpt5_routes_to_limits(self):
        from src.core.loader import _apply_settings_matrix

        data = {"llm": {"model": "gpt-5.5"}}
        _apply_settings_matrix(data, set())
        assert "image_tokens" in data.get("limits", {})
        assert data["limits"]["image_tokens"]["mode"] == "openai_patches"
        # Must NOT leak into llm (LLMConfig would drop it anyway).
        assert "image_tokens" not in data["llm"]

    def test_claude_sonnet_row_now_multimodal(self):
        from src.core.loader import _apply_settings_matrix

        data = {"llm": {"model": "claude-sonnet-4-6"}}
        _apply_settings_matrix(data, set())
        assert data["llm"]["multimodal"] is True
        assert data["limits"]["image_tokens"]["mode"] == "anthropic_patches"


# =============================================================================
# ContextManager end-to-end with a resolved family config
# =============================================================================


class TestContextManagerIntegration:
    def test_gpt5_image_counted_by_dimensions(self):
        cfg = ContextConfig(image_tokens=GPT5)
        mgr = ContextManager(config=cfg, model="gpt-5.5")
        msg = HumanMessage(
            content=[
                {"type": "text", "text": "Image content from tool call"},
                _png_block(1700, 2200),
            ]
        )
        count = mgr.get_token_count([msg])
        # ~3726 image tokens + a few text tokens + per-message overhead.
        assert 3726 <= count < 3800

    def test_flat_family_ignores_dimensions(self):
        cfg = ContextConfig(image_tokens=GEMINI)
        mgr = ContextManager(config=cfg, model="gemini-3")
        msg = HumanMessage(
            content=[{"type": "text", "text": "x"}, _png_block(1700, 2200)]
        )
        count = mgr.get_token_count([msg])
        assert 2304 <= count < 2400
