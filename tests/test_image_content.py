"""Tests for src/services/image_content.py.

Covers:
- extract_image_tags: empty input, single image_data, single page_image,
  mixed/multiple, malformed tags, marker substitution shape,
  source-order preservation, base64 whitespace canonicalization.
- Content-block builders: data URL shape, image_url block shape,
  multimodal HumanMessage shape (text + image parts).
- Tag-template/regex round-trip: a string formatted via the template
  must be re-extractable by the regex.
"""

from __future__ import annotations

import base64

import pytest
from langchain_core.messages import HumanMessage

from src.services.image_content import (
    IMAGE_DATA_TAG_TEMPLATE,
    PAGE_IMAGE_TAG_TEMPLATE,
    ExtractedImage,
    extract_image_tags,
    make_image_content_block,
    make_image_content_block_from_b64,
    make_multimodal_user_message,
    to_data_url,
    to_data_url_from_b64,
)


# =============================================================================
# Test fixtures and helpers
# =============================================================================


def _b64(text: str) -> str:
    """Quick base64 of arbitrary text for compact test data."""
    return base64.b64encode(text.encode()).decode()


# A real, tiny JPEG header — recognizable bytes so any test that decodes
# the data URL gets back something meaningful.
JPEG_HEADER_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00"
PNG_HEADER_BYTES = b"\x89PNG\r\n\x1a\n"


# =============================================================================
# extract_image_tags
# =============================================================================


class TestExtractImageTagsEmpty:
    def test_none_returns_empty(self):
        assert extract_image_tags(None) == (None, [])

    def test_empty_string_returns_empty(self):
        assert extract_image_tags("") == ("", [])

    def test_string_with_no_tags_passes_through(self):
        text = "Plain tool result with no image tags whatsoever."
        cleaned, images = extract_image_tags(text)
        assert cleaned == text
        assert images == []


class TestExtractImageTagsSingleImage:
    def test_single_image_data_tag_extracted(self):
        b64 = _b64("hello-jpeg-bytes")
        content = (
            "[IMAGE: photo.jpg]\nType: image/jpeg\nSize: 16 bytes\n\n"
            + IMAGE_DATA_TAG_TEMPLATE.format(mime="image/jpeg", b64=b64)
        )
        cleaned, images = extract_image_tags(content)

        assert len(images) == 1
        assert images[0].mime_type == "image/jpeg"
        assert images[0].base64_data == b64
        assert images[0].page_num is None
        assert "image attached: image/jpeg" in cleaned
        # The base64 wall must be gone from the cleaned content.
        assert b64 not in cleaned

    def test_single_page_image_tag_extracted(self):
        b64 = _b64("png-page-bytes")
        content = "Some text\n" + PAGE_IMAGE_TAG_TEMPLATE.format(
            page=3, mime="image/png", b64=b64
        )
        cleaned, images = extract_image_tags(content)

        assert len(images) == 1
        assert images[0].mime_type == "image/png"
        assert images[0].base64_data == b64
        assert images[0].page_num == 3
        assert "page 3 image attached: image/png" in cleaned
        assert b64 not in cleaned


class TestExtractImageTagsMultiple:
    def test_two_image_data_tags_preserved_in_order(self):
        b64_a = _b64("first")
        b64_b = _b64("second")
        content = (
            "Header\n"
            + IMAGE_DATA_TAG_TEMPLATE.format(mime="image/png", b64=b64_a)
            + "\nMiddle\n"
            + IMAGE_DATA_TAG_TEMPLATE.format(mime="image/jpeg", b64=b64_b)
            + "\nFooter"
        )
        cleaned, images = extract_image_tags(content)

        assert len(images) == 2
        assert images[0].base64_data == b64_a
        assert images[0].mime_type == "image/png"
        assert images[1].base64_data == b64_b
        assert images[1].mime_type == "image/jpeg"
        assert "Header" in cleaned and "Middle" in cleaned and "Footer" in cleaned
        assert b64_a not in cleaned
        assert b64_b not in cleaned

    def test_mixed_image_data_and_page_image_in_source_order(self):
        b64_img = _b64("standalone")
        b64_page = _b64("page2")
        content = (
            IMAGE_DATA_TAG_TEMPLATE.format(mime="image/jpeg", b64=b64_img)
            + "\nthen page\n"
            + PAGE_IMAGE_TAG_TEMPLATE.format(page=2, mime="image/png", b64=b64_page)
        )
        cleaned, images = extract_image_tags(content)

        assert len(images) == 2
        assert images[0].page_num is None
        assert images[0].base64_data == b64_img
        assert images[1].page_num == 2
        assert images[1].base64_data == b64_page

    def test_page_image_before_image_data_orders_correctly(self):
        # Reverse the source order vs the previous test to prove the
        # extractor is offset-driven, not regex-iteration-order driven.
        b64_page = _b64("page-first")
        b64_img = _b64("image-second")
        content = (
            PAGE_IMAGE_TAG_TEMPLATE.format(page=1, mime="image/png", b64=b64_page)
            + "\n"
            + IMAGE_DATA_TAG_TEMPLATE.format(mime="image/jpeg", b64=b64_img)
        )
        cleaned, images = extract_image_tags(content)

        assert len(images) == 2
        assert images[0].page_num == 1  # appeared first in source
        assert images[1].page_num is None


class TestExtractImageTagsMalformed:
    def test_unclosed_image_data_tag_ignored(self):
        content = '<image_data mime_type="image/png">\nQUJDRA==\n(no closing tag)'
        cleaned, images = extract_image_tags(content)
        assert images == []
        assert cleaned == content

    def test_missing_mime_attribute_ignored(self):
        content = "<image_data>\nQUJDRA==\n</image_data>"
        cleaned, images = extract_image_tags(content)
        assert images == []
        assert cleaned == content

    def test_page_image_missing_page_attribute_ignored(self):
        content = '<page_image mime_type="image/png">\nQUJDRA==\n</page_image>'
        cleaned, images = extract_image_tags(content)
        assert images == []
        assert cleaned == content


class TestExtractImageTagsBase64Hygiene:
    def test_internal_whitespace_in_base64_is_stripped(self):
        # Real serialized payloads can have line breaks inside the base64
        # body (e.g. line-wrapped). The extractor must normalize.
        b64 = _b64("hello-world")
        # Inject newlines + spaces into the middle of the b64 capture
        with_whitespace = b64[:4] + "\n  " + b64[4:8] + "\n\t" + b64[8:]
        content = (
            f'<image_data mime_type="image/png">\n{with_whitespace}\n</image_data>'
        )
        _, images = extract_image_tags(content)
        assert len(images) == 1
        assert images[0].base64_data == b64  # whitespace removed


# =============================================================================
# Content-block builders
# =============================================================================


class TestDataUrlBuilders:
    def test_to_data_url_from_b64_shape(self):
        url = to_data_url_from_b64("QUJD", "image/png")
        assert url == "data:image/png;base64,QUJD"

    def test_to_data_url_from_bytes_encodes_correctly(self):
        url = to_data_url(JPEG_HEADER_BYTES, "image/jpeg")
        expected_b64 = base64.b64encode(JPEG_HEADER_BYTES).decode()
        assert url == f"data:image/jpeg;base64,{expected_b64}"


class TestImageContentBlockBuilders:
    def test_make_image_content_block_from_b64_shape(self):
        block = make_image_content_block_from_b64("QUJD", "image/png")
        assert block == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,QUJD"},
        }

    def test_make_image_content_block_from_bytes_round_trip(self):
        block = make_image_content_block(PNG_HEADER_BYTES, "image/png")
        assert block["type"] == "image_url"
        url = block["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        encoded = url.split(",", 1)[1]
        assert base64.b64decode(encoded) == PNG_HEADER_BYTES


class TestMultimodalUserMessage:
    def test_text_only_when_no_images(self):
        msg = make_multimodal_user_message("hello", [])
        assert isinstance(msg, HumanMessage)
        assert msg.content == [{"type": "text", "text": "hello"}]

    def test_text_plus_one_image(self):
        img = ExtractedImage(base64_data="QUJD", mime_type="image/png")
        msg = make_multimodal_user_message("see this:", [img])
        assert msg.content == [
            {"type": "text", "text": "see this:"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,QUJD"},
            },
        ]

    def test_text_plus_multiple_images_in_order(self):
        img_a = ExtractedImage("QQ==", "image/png")
        img_b = ExtractedImage("QkI=", "image/jpeg", page_num=4)
        msg = make_multimodal_user_message("two:", [img_a, img_b])
        assert len(msg.content) == 3
        assert msg.content[0] == {"type": "text", "text": "two:"}
        assert msg.content[1]["image_url"]["url"] == "data:image/png;base64,QQ=="
        assert msg.content[2]["image_url"]["url"] == "data:image/jpeg;base64,QkI="


# =============================================================================
# Round-trip: producer template format → extractor regex
# =============================================================================


class TestTagTemplateRegexRoundTrip:
    @pytest.mark.parametrize(
        "mime",
        ["image/png", "image/jpeg", "image/webp", "image/gif"],
    )
    def test_image_data_template_extracts_back(self, mime):
        b64 = _b64(f"bytes-for-{mime}")
        content = IMAGE_DATA_TAG_TEMPLATE.format(mime=mime, b64=b64)
        cleaned, images = extract_image_tags(content)
        assert len(images) == 1
        assert images[0].mime_type == mime
        assert images[0].base64_data == b64
        assert b64 not in cleaned

    @pytest.mark.parametrize("page", [1, 7, 42, 999])
    def test_page_image_template_extracts_back(self, page):
        b64 = _b64(f"page-{page}")
        content = PAGE_IMAGE_TAG_TEMPLATE.format(page=page, mime="image/png", b64=b64)
        _, images = extract_image_tags(content)
        assert len(images) == 1
        assert images[0].page_num == page
