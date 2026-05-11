"""Shared utilities for delivering images to multimodal LLMs.

Image-bearing tools (`read_file` on a JPG, PDF page rendering, etc.) emit
their image payloads as base64 wrapped in XML tags inside the tool's
string return value. The tags below are the producer/consumer contract.
The post-processor in `src/graph.py` (`audited_tools`) and
`src/persistent_graph.py` (tool execution loop) calls
`extract_image_tags()` on each `ToolMessage.content` to:

1. Strip the `<image_data>` / `<page_image>` blocks out of the tool result.
2. Replace each tag with a short marker (e.g. `[image attached: image/png]`).
3. Build a synthesized `HumanMessage` carrying the same image as a real
   provider content block (`{"type": "image_url", ...}`) so a multimodal
   primary model can actually see the image.

The chat-completions image-content-block schema is the lowest common
denominator that LangChain auto-translates to OpenAI/Anthropic/Google
formats — no vendor branching needed here.

Browser screenshot tools currently emit base64 inside a JSON-serialized
dict rather than a `<image_data>` text tag; they are intentionally NOT
covered by this module yet. A follow-up will either emit the same tag
inside the JSON result or introduce a richer content-block-aware tool
result protocol.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from langchain_core.messages import HumanMessage

# ---------------------------------------------------------------------------
# Tag formats — single source of truth shared with src/tools/workspace/files.py
# ---------------------------------------------------------------------------

# Producers format these literals; consumers parse them via the regexes below.
# Keep the producer format and regex aligned.
IMAGE_DATA_TAG_TEMPLATE = '<image_data mime_type="{mime}">\n{b64}\n</image_data>'
PAGE_IMAGE_TAG_TEMPLATE = (
    '<page_image page="{page}" mime_type="{mime}">\n{b64}\n</page_image>'
)

IMAGE_DATA_TAG_RE = re.compile(
    r'<image_data\s+mime_type="(?P<mime>[^"]+)">\s*'
    r"(?P<b64>[A-Za-z0-9+/=\s]+?)\s*</image_data>",
    re.DOTALL,
)
PAGE_IMAGE_TAG_RE = re.compile(
    r'<page_image\s+page="(?P<page>\d+)"\s+mime_type="(?P<mime>[^"]+)">\s*'
    r"(?P<b64>[A-Za-z0-9+/=\s]+?)\s*</page_image>",
    re.DOTALL,
)


@dataclass(frozen=True)
class ExtractedImage:
    """One image lifted from a tool result string.

    `base64_data` has been whitespace-stripped (the regex captures may
    include line breaks from the tag's serialized form).
    """

    base64_data: str
    mime_type: str
    page_num: Optional[int] = None


# ---------------------------------------------------------------------------
# Content-block builders
# ---------------------------------------------------------------------------


def to_data_url_from_b64(b64: str, mime_type: str) -> str:
    """Build a `data:` URL from already-base64-encoded image bytes."""
    return f"data:{mime_type};base64,{b64}"


def to_data_url(image_bytes: bytes, mime_type: str) -> str:
    """Build a `data:` URL from raw image bytes."""
    return to_data_url_from_b64(base64.b64encode(image_bytes).decode(), mime_type)


def make_image_content_block_from_b64(b64: str, mime_type: str) -> dict:
    """Build an OpenAI-compatible image content block from base64 input.

    The `image_url` shape is what LangChain's per-provider message
    translators understand; OpenAI/Anthropic/Google variants are emitted
    automatically by their respective `langchain_*` integrations.
    """
    return {
        "type": "image_url",
        "image_url": {"url": to_data_url_from_b64(b64, mime_type)},
    }


def make_image_content_block(image_bytes: bytes, mime_type: str) -> dict:
    """Build an image content block from raw bytes."""
    return make_image_content_block_from_b64(
        base64.b64encode(image_bytes).decode(), mime_type
    )


def make_multimodal_user_message(
    text: str, images: Sequence[ExtractedImage]
) -> HumanMessage:
    """Build a HumanMessage whose content list interleaves text + images.

    The first part is always the text prefix; subsequent parts are one
    image content block per `ExtractedImage`, in input order.
    """
    parts: list[dict] = [{"type": "text", "text": text}]
    for img in images:
        parts.append(make_image_content_block_from_b64(img.base64_data, img.mime_type))
    return HumanMessage(content=parts)


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------


def _strip_b64_whitespace(b64: str) -> str:
    """Drop whitespace from a base64 capture so the value is canonical."""
    return re.sub(r"\s+", "", b64)


def extract_image_tags(content: str) -> tuple[str, list[ExtractedImage]]:
    """Strip `<image_data>` / `<page_image>` tags from a tool result.

    For each tag found, the matched span in `content` is replaced with a
    short marker so the cleaned string still reads naturally to the model
    (e.g., `[image attached: image/png]`,
    `[page 3 image attached: image/png]`). The extracted images are
    returned in source order so the synthesized `HumanMessage` mirrors
    the original layout.

    Returns a `(cleaned_content, [ExtractedImage, ...])` tuple. When no
    tags are found, the original content is returned unchanged with an
    empty list.
    """
    if not content:
        return content, []

    extracted: list[tuple[int, ExtractedImage]] = []  # (offset, image)
    replacements: list[tuple[int, int, str]] = []  # (start, end, marker)

    for match in IMAGE_DATA_TAG_RE.finditer(content):
        mime = match.group("mime")
        b64 = _strip_b64_whitespace(match.group("b64"))
        extracted.append((match.start(), ExtractedImage(b64, mime)))
        replacements.append((match.start(), match.end(), f"[image attached: {mime}]"))

    for match in PAGE_IMAGE_TAG_RE.finditer(content):
        page = int(match.group("page"))
        mime = match.group("mime")
        b64 = _strip_b64_whitespace(match.group("b64"))
        extracted.append((match.start(), ExtractedImage(b64, mime, page_num=page)))
        replacements.append(
            (
                match.start(),
                match.end(),
                f"[page {page} image attached: {mime}]",
            )
        )

    if not extracted:
        return content, []

    # Apply replacements right-to-left so earlier offsets stay valid.
    replacements.sort(key=lambda r: r[0], reverse=True)
    cleaned = content
    for start, end, marker in replacements:
        cleaned = cleaned[:start] + marker + cleaned[end:]

    # Return images in source-order (left-to-right).
    extracted.sort(key=lambda e: e[0])
    return cleaned, [img for _, img in extracted]
