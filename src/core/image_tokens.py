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

from typing import Any, Tuple

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


def split_text_and_images(content: Any) -> Tuple[str, int]:
    """Split message content into ``(concatenated_text, image_block_count)``.

    Plain string content is returned as-is with zero images. For multimodal
    list content, text parts are concatenated and image blocks are counted —
    but their base64 payload is dropped, never stringified.
    """
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return str(content), 0

    text_parts: list[str] = []
    n_images = 0
    for item in content:
        if isinstance(item, str):
            text_parts.append(item)
        elif _is_image_block(item):
            n_images += 1
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            # Other non-text, non-image dicts contribute nothing — avoids
            # stringifying arbitrary base64-bearing payloads.
        else:
            text_parts.append(str(item))
    return "\n".join(text_parts), n_images


def estimate_image_tokens(n_images: int) -> int:
    """Biased-high flat estimate for ``n_images`` image blocks (S1).

    Superseded per family by the dimension-aware estimator in S4.
    """
    return max(0, n_images) * DEFAULT_IMAGE_TOKENS
