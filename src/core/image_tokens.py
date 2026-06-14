"""Image-token accounting for multimodal message content.

A provider's vision encoder bills an image content block by its pixel
dimensions (~hundreds of tokens), NOT by the length of the base64 data URL
that transports it. Our local token counters historically did
``str(msg.content)`` over the whole multimodal list, tokenizing the base64 as
if it were text — which inflated a 12-image conversation to ~9.2M "tokens"
(dev session 5dbb5770, 2026-06-13) and spuriously tripped compaction.

This module is the seam for counting image blocks correctly. v1 (slice S1)
uses a single biased-high flat constant per image; the per-family
dimension-aware estimator (openai_patches / openai_tiles / anthropic_patches /
flat) lands in S4. Design: docs/features/context_token_accounting.md.
"""

import base64
import math
import struct
from typing import Any, List, Optional, Tuple

# Recognized multimodal image content-block discriminators across providers:
# OpenAI Chat Completions ("image_url"), Anthropic ("image"), OpenAI Responses
# API ("input_image").
_IMAGE_BLOCK_TYPES = frozenset({"image_url", "image", "input_image"})

# Biased-high flat per-image token cost, used until the per-family estimator is
# wired (S1) and as the fallback when image dimensions can't be read. Matches
# the ``default`` matrix row in context_token_accounting.md §4.2. Deliberately
# high: over-counting compacts slightly early (safe); under-counting risks a
# wasted overflow round-trip.
DEFAULT_IMAGE_TOKENS = 1600


def _is_image_block(item: Any) -> bool:
    """True if a content-list item is an image block (by type or structure)."""
    if not isinstance(item, dict):
        return False
    if item.get("type") in _IMAGE_BLOCK_TYPES:
        return True
    # Structural fallback for unlabeled blocks, so base64 is never tokenized as
    # text even if a provider shape we don't enumerate slips through.
    return "image_url" in item or "source" in item


def split_text_and_image_blocks(content: Any) -> Tuple[str, List[Any]]:
    """Split content into ``(concatenated_text, [image_block_items])``.

    Like :func:`split_text_and_images` but returns the image block dicts
    themselves, so the dimension-aware estimator (S4) can read width/height.
    Plain string content yields no blocks. Base64 payloads are never folded
    into the text.
    """
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []

    text_parts: List[str] = []
    blocks: List[Any] = []
    for item in content:
        if isinstance(item, str):
            text_parts.append(item)
        elif _is_image_block(item):
            blocks.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            # Other non-text, non-image dicts contribute nothing — avoids
            # stringifying arbitrary base64-bearing payloads.
        else:
            text_parts.append(str(item))
    return "\n".join(text_parts), blocks


def split_text_and_images(content: Any) -> Tuple[str, int]:
    """Split message content into ``(concatenated_text, image_block_count)``.

    Plain string content is returned as-is with zero images. For multimodal
    list content, text parts are concatenated and image blocks are counted —
    but their base64 payload is dropped, never stringified.
    """
    text, blocks = split_text_and_image_blocks(content)
    return text, len(blocks)


def estimate_image_tokens(n_images: int) -> int:
    """Biased-high flat estimate for ``n_images`` image blocks (S1).

    Superseded per family by the dimension-aware estimator in S4.
    """
    return max(0, n_images) * DEFAULT_IMAGE_TOKENS


def has_image_content(content: Any) -> bool:
    """True if message content is a multimodal list with >=1 image block.

    Used by compaction-time elision (S3) to find image messages it can shed; a
    plain-string or text-only-list message returns False, so a normal user
    turn is never mistaken for a sheddable image re-delivery.
    """
    if not isinstance(content, list):
        return False
    return any(_is_image_block(item) for item in content)


def _image_block_mime(item: dict) -> str:
    """Best-effort MIME label for an image block ('image' if unknown).

    Covers the three transported shapes: Anthropic ``source.media_type``,
    OpenAI Chat Completions ``image_url.url`` data URL, and the Responses-API
    ``input_image`` string data URL. Never returns the base64 payload.
    """
    source = item.get("source")
    if isinstance(source, dict):
        media_type = source.get("media_type")
        if isinstance(media_type, str) and media_type:
            return media_type
    url = item.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if isinstance(url, str) and url.startswith("data:"):
        # data:image/png;base64,AAAA -> image/png
        return url[5:].split(";", 1)[0].split(",", 1)[0] or "image"
    return "image"


def content_to_summary_text(content: Any) -> str:
    """Flatten message content to a string safe for a text summarizer (S3).

    String content is returned unchanged (callers apply their own character
    truncation). Multimodal list content keeps its text parts and replaces each
    image block with a compact ``[image: <mime>, ~<n> tok]`` marker, so a base64
    data URL is never stringified into the summarizer prompt -- the leak that
    drove 60 doomed 96k-token folds and wedged session 5dbb5770.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif _is_image_block(item):
            parts.append(
                f"[image: {_image_block_mime(item)}, ~{DEFAULT_IMAGE_TOKENS} tok]"
            )
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            # Other non-text, non-image dicts contribute nothing -- avoids
            # stringifying arbitrary base64-bearing payloads.
        else:
            parts.append(str(item))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# S4 — dimension-aware per-family estimator
#
# A provider bills an image by its pixel dimensions, not its base64 length, and
# the exact cost is a function of the model family's tiling/patching scheme. We
# read width x height cheaply from the image header (no full decode, no Pillow)
# and apply the family `mode` from the matrix `settings.image_tokens`. Every
# path is biased high and falls back to a flat constant when dims can't be read
# or the family is unconfigured. Formulas verified against provider primary
# sources — see docs/features/context_token_accounting.md §4.3 + appendix.
# ---------------------------------------------------------------------------


def _extract_b64_from_block(block: Any) -> Optional[str]:
    """Pull the raw base64 payload out of an image block (any provider shape).

    Anthropic ``source.data`` (raw base64), OpenAI Chat Completions
    ``image_url.url`` data URL, Responses-API ``input_image`` string data URL.
    Returns None for remote (non-data) URLs — dims can't be read without a fetch.
    """
    if not isinstance(block, dict):
        return None
    source = block.get("source")
    if isinstance(source, dict):
        data = source.get("data")
        if isinstance(data, str) and data:
            return data
    url = block.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if isinstance(url, str) and "base64," in url:
        return url.split("base64,", 1)[1]
    return None


def _png_dimensions(header: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) from a PNG IHDR (fixed offset 16:24)."""
    if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", header[16:24])
        if w and h:
            return int(w), int(h)
    return None


# JPEG Start-Of-Frame markers carry the frame's height/width; all other
# segments (APPn/DQT/DHT…) are length-prefixed and skipped during the scan.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _jpeg_dimensions(header: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) from the first JPEG SOF marker, scanning segments."""
    n = len(header)
    if n < 4 or header[0] != 0xFF or header[1] != 0xD8:
        return None
    i = 2
    while i + 1 < n:
        if header[i] != 0xFF:
            i += 1
            continue
        marker = header[i + 1]
        if marker in _JPEG_SOF_MARKERS:
            # FF, marker, len(2), precision(1), height(2), width(2)
            if i + 9 <= n:
                h = (header[i + 5] << 8) | header[i + 6]
                w = (header[i + 7] << 8) | header[i + 8]
                if w and h:
                    return w, h
            return None
        # Standalone markers (SOI/EOI/RSTn) carry no length segment.
        if marker == 0xD8 or marker == 0xD9 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 3 >= n:
            break
        seg_len = (header[i + 2] << 8) | header[i + 3]
        if seg_len < 2:
            break
        i += 2 + seg_len
    return None


def read_image_dimensions(block: Any) -> Optional[Tuple[int, int]]:
    """Best-effort (width, height) from an image block's base64 header.

    Decodes only a bounded prefix — a PNG IHDR sits at a fixed offset and a JPEG
    SOF is normally within the first KBs — so a multi-hundred-KB payload is never
    fully decoded. Returns None when the format/dims can't be read; the caller
    then bills the family flat cost (biased-high, safe).
    """
    b64 = _extract_b64_from_block(block)
    if not b64:
        return None
    n = min(len(b64), 16_384)
    n -= n % 4  # base64 decodes in 4-char groups
    if n <= 0:
        return None
    try:
        header = base64.b64decode(b64[:n])
    except Exception:
        return None
    return _png_dimensions(header) or _jpeg_dimensions(header)


def estimate_image_block_tokens(block: Any, image_config: Optional[dict]) -> int:
    """Estimate the provider-billed token cost of one image block (S4).

    ``image_config`` is the resolved ``settings.image_tokens`` for the model's
    family (matrix -> LimitsConfig -> ContextConfig). Biased high throughout:
    over-counting compacts slightly early (safe); under-counting risks a wasted
    overflow round-trip. Falls back to the family ``flat`` (then the global
    default) whenever the mode is ``flat``, dims can't be read, or the mode is
    unknown — so base64 is never tokenized as text.
    """
    if not isinstance(image_config, dict):
        return DEFAULT_IMAGE_TOKENS
    mode = image_config.get("mode", "flat")
    flat = int(image_config.get("flat", DEFAULT_IMAGE_TOKENS))
    if mode == "flat":
        return flat
    dims = read_image_dimensions(block)
    if dims is None:
        return flat
    w, h = dims
    if w <= 0 or h <= 0:
        return flat
    if mode == "openai_patches":
        return _openai_patches_tokens(w, h, image_config)
    if mode == "openai_tiles":
        return _openai_tiles_tokens(w, h, image_config)
    if mode == "anthropic_patches":
        return _anthropic_patches_tokens(w, h, image_config)
    return flat


def _openai_patches_tokens(w: int, h: int, cfg: dict) -> int:
    """GPT-5 / GPT-4.1 family: 32px patches under a budget, shrink if over.

    patches = ceil(w/32)*ceil(h/32); if it exceeds ``budget`` the image is
    scaled by sqrt(32^2 * budget / (w*h)) and re-patched. ``budget`` defaults to
    the expensive no-/original-detail tier (10000); an optional per-model
    ``multiplier`` converts patches -> billed tokens (1.0 for full gpt-5).
    """
    patch = int(cfg.get("patch_px", 32))
    budget = int(cfg.get("budget", 10_000))
    multiplier = float(cfg.get("multiplier", 1.0))
    raw = math.ceil(w / patch) * math.ceil(h / patch)
    if raw <= budget:
        patches = raw
    else:
        # OpenAI's published shrink: scale by sqrt(patch^2 * budget / area),
        # then nudge by the per-axis floor ratio so the re-patched count lands
        # at/under budget rather than overshooting on the ceil.
        sf = math.sqrt((patch * patch * budget) / (w * h))
        adj = sf * min(
            math.floor(w * sf / patch) / (w * sf / patch),
            math.floor(h * sf / patch) / (h * sf / patch),
        )
        patches = math.ceil(w * adj / patch) * math.ceil(h * adj / patch)
    return int(round(min(patches, budget) * multiplier))


def _openai_tiles_tokens(w: int, h: int, cfg: dict) -> int:
    """GPT-4o / o-series: base + per-tile over 512px tiles after a 2048/768 fit.

    Fit within 2048x2048, then scale the shortest side to 768, then count
    512x512 tiles: tokens = base + per_tile * tiles.
    """
    base = int(cfg.get("base", 85))
    per_tile = int(cfg.get("per_tile", 170))
    tile = int(cfg.get("tile_px", 512))
    fw, fh = float(w), float(h)
    longest = max(fw, fh)
    if longest > 2048:
        s = 2048.0 / longest
        fw, fh = fw * s, fh * s
    shortest = min(fw, fh)
    if shortest > 768:
        s = 768.0 / shortest
        fw, fh = fw * s, fh * s
    tiles = math.ceil(fw / tile) * math.ceil(fh / tile)
    return int(base + per_tile * tiles)


def _anthropic_patches_tokens(w: int, h: int, cfg: dict) -> int:
    """Claude: 28px patches after Anthropic's two-constraint resize."""
    patch = int(cfg.get("patch_px", 28))
    max_edge = int(cfg.get("max_edge", 1568))
    max_tokens = int(cfg.get("max_tokens", 1568))
    w_r, h_r = _anthropic_resized(w, h, max_edge, max_tokens, patch)
    return math.ceil(w_r / patch) * math.ceil(h_r / patch)


def _anthropic_resized(
    w: int, h: int, max_edge: int, max_tokens: int, patch: int
) -> Tuple[int, int]:
    """Largest aspect-preserving (w, h) satisfying both Anthropic constraints.

    A faithful port of Anthropic's reference ``resized_size``: each padded edge
    ``ceil(side/28)*28`` must fit ``max_edge`` AND the patch count
    ``ceil(w/28)*ceil(h/28)`` must fit ``max_tokens``. Binary-searches the
    integer long edge (the recursion normalizes so width >= height). Never
    upscales. Reproduces the doc's own examples (1000x1000->1296, A4->1551).
    """

    def _fits(ww: int, hh: int) -> bool:
        return (
            math.ceil(ww / patch) * patch <= max_edge
            and math.ceil(hh / patch) * patch <= max_edge
            and math.ceil(ww / patch) * math.ceil(hh / patch) <= max_tokens
        )

    if _fits(w, h):
        return w, h
    if h > w:
        rh, rw = _anthropic_resized(h, w, max_edge, max_tokens, patch)
        return rw, rh
    # w >= h: binary-search the long edge; lo always fits, hi never does.
    ar = w / h
    lo, hi = 1, w
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _fits(mid, max(round(mid / ar), 1)):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / ar), 1)
