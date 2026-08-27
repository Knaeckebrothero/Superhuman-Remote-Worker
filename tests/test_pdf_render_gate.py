"""Tests for the PDF visual-render gate (context_token_accounting.md S2).

Text-rich, image-free pages are not rasterized — their extracted text already
represents them, and rendering only burns image tokens. Image-bearing or
text-sparse pages are always rendered, so a pictogram-bearing page is never
dropped.
"""

from src.utils.pdf import (
    DEFAULT_MIN_TEXT_CHARS_FOR_SKIP,
    compress_ranges,
    should_render_page,
)


class TestShouldRenderPage:
    def test_text_rich_image_free_is_skipped(self):
        assert should_render_page(chars=5000, has_images=False) is False

    def test_image_bearing_always_rendered(self):
        # A page with a pictogram is rendered even if it is text-rich.
        assert should_render_page(chars=5000, has_images=True) is True

    def test_text_sparse_is_rendered(self):
        # Likely scanned/figure page — text extraction missed the content.
        assert should_render_page(chars=10, has_images=False) is True

    def test_boundary_at_threshold_skips(self):
        # >= threshold counts as text-rich.
        assert (
            should_render_page(chars=DEFAULT_MIN_TEXT_CHARS_FOR_SKIP, has_images=False)
            is False
        )
        assert (
            should_render_page(
                chars=DEFAULT_MIN_TEXT_CHARS_FOR_SKIP - 1, has_images=False
            )
            is True
        )

    def test_custom_threshold(self):
        assert (
            should_render_page(chars=150, has_images=False, min_text_chars=200) is True
        )
        assert (
            should_render_page(chars=250, has_images=False, min_text_chars=200) is False
        )


class TestCompressRanges:
    def test_empty(self):
        assert compress_ranges([]) == ""

    def test_single(self):
        assert compress_ranges([3]) == "3"

    def test_contiguous(self):
        assert compress_ranges([2, 3, 4, 5]) == "2-5"

    def test_mixed(self):
        assert compress_ranges([2, 3, 4, 6, 9, 10]) == "2-4, 6, 9-10"

    def test_unsorted_and_deduped(self):
        assert compress_ranges([10, 9, 2, 3, 2]) == "2-3, 9-10"

    def test_full_regulatory_range(self):
        # The 5dbb5770 case: a 680-page text doc — pages 2..680 skipped.
        assert compress_ranges(list(range(2, 681))) == "2-680"
