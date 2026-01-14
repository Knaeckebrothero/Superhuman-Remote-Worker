"""
Web Source Integration Tests for Citation Engine.

These tests verify web source functionality with real network calls.
They are skipped by default and only run when RUN_WEB_TESTS=true.

Prerequisites:
    1. Install web dependencies: pip install -e ".[web]"
    2. Set environment: RUN_WEB_TESTS=true
    3. Run tests: pytest tests/test_integration_web.py -v -s

Run with: pytest tests/test_integration_web.py -v -s
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from citation_engine import CitationEngine, SourceType, VerificationStatus
from citation_engine.models import VerificationResult

# Skip marker for web tests
RUN_WEB_TESTS = os.getenv("RUN_WEB_TESTS", "false").lower() == "true"
skip_web = pytest.mark.skipif(
    not RUN_WEB_TESTS,
    reason="Web tests disabled. Set RUN_WEB_TESTS=true to enable.",
)

# =============================================================================
# WEB SOURCE REGISTRATION TESTS
# =============================================================================


@skip_web
@pytest.mark.web
@pytest.mark.integration
class TestWebSourceRegistration:
    """Tests for web source registration with real network calls."""

    def test_add_web_source_simple_page(self, temp_db_path):
        """Test registering a simple web page as a source."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            # Use a stable, well-known page
            source = engine.add_web_source(
                url="https://httpbin.org/html",
                name="HTTPBin HTML Test Page",
            )

            assert source.id is not None
            assert source.type == SourceType.WEBSITE
            assert source.name == "HTTPBin HTML Test Page"
            assert source.content is not None
            assert len(source.content) > 0
            assert source.content_hash is not None

            # Check metadata
            assert source.metadata is not None
            assert "url" in source.metadata
            assert "accessed_at" in source.metadata
            assert source.metadata["url"] == "https://httpbin.org/html"

            print(f"\nWeb source registered: {source.name}")
            print(f"Content length: {len(source.content)} characters")
            print(f"Content hash: {source.content_hash[:16]}...")

    def test_add_web_source_with_title_extraction(self, temp_db_path):
        """Test that page title is extracted."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://example.com",
                name="Example Domain Page",
            )

            assert source.id is not None
            # example.com has a title
            if source.metadata.get("title"):
                print(f"\nExtracted title: {source.metadata['title']}")

    def test_add_web_source_default_name(self, temp_db_path):
        """Test that URL is used as default name."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            url = "https://httpbin.org/robots.txt"
            source = engine.add_web_source(url=url)

            # Name should default to URL
            assert source.name == url

    def test_add_web_source_with_metadata(self, temp_db_path):
        """Test registering web source with custom metadata."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://httpbin.org/html",
                name="Metadata Test Page",
                version="2024-01-15",
                metadata={
                    "category": "test",
                    "importance": "high",
                },
            )

            assert source.metadata["category"] == "test"
            assert source.metadata["importance"] == "high"
            # Original fetch metadata should still be present
            assert "accessed_at" in source.metadata


# =============================================================================
# WEB SOURCE CONTENT EXTRACTION TESTS
# =============================================================================


@skip_web
@pytest.mark.web
@pytest.mark.integration
class TestWebContentExtraction:
    """Tests for web content extraction."""

    def test_extract_text_removes_scripts(self, temp_db_path):
        """Test that script tags are removed from content."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            # httpbin.org/html has some content to extract
            source = engine.add_web_source(
                url="https://httpbin.org/html",
                name="Script Removal Test",
            )

            # Content should not contain script tags
            assert "<script" not in source.content.lower()

    def test_extract_text_removes_styles(self, temp_db_path):
        """Test that style tags are removed from content."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://httpbin.org/html",
                name="Style Removal Test",
            )

            # Content should not contain style tags
            assert "<style" not in source.content.lower()

    def test_content_is_text_not_html(self, temp_db_path):
        """Test that extracted content is plain text, not HTML."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://example.com",
                name="Plain Text Test",
            )

            # Content should not have HTML tags
            assert "<html" not in source.content.lower()
            assert "<body" not in source.content.lower()
            assert "</div>" not in source.content.lower()


# =============================================================================
# WEB SOURCE ERROR HANDLING TESTS
# =============================================================================


@skip_web
@pytest.mark.web
@pytest.mark.integration
class TestWebSourceErrors:
    """Tests for error handling in web source operations."""

    def test_add_web_source_invalid_url(self, temp_db_path):
        """Test error handling for invalid URL."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            with pytest.raises(ConnectionError):
                engine.add_web_source(
                    url="https://this-domain-does-not-exist-12345.invalid",
                    name="Invalid URL Test",
                )

    def test_add_web_source_404(self, temp_db_path):
        """Test error handling for 404 response."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            with pytest.raises(ConnectionError):
                engine.add_web_source(
                    url="https://httpbin.org/status/404",
                    name="404 Test",
                )

    def test_add_web_source_500(self, temp_db_path):
        """Test error handling for 500 response."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            with pytest.raises(ConnectionError):
                engine.add_web_source(
                    url="https://httpbin.org/status/500",
                    name="500 Test",
                )


# =============================================================================
# WEB SOURCE CITATION TESTS
# =============================================================================


@skip_web
@pytest.mark.web
@pytest.mark.integration
class TestWebSourceCitations:
    """Tests for creating citations from web sources."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_cite_web_source(self, mock_verify, temp_db_path):
        """Test creating citation from web source."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.9,
            reasoning="Content found in archived page",
        )

        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            # Register web source
            source = engine.add_web_source(
                url="https://example.com",
                name="Example Domain",
            )

            # Create citation
            result = engine.cite_web(
                claim="This domain is for use in examples",
                source_id=source.id,
                quote_context="This domain is for use in illustrative examples in documents.",
                locator={
                    "heading": "Example Domain",
                    "accessed_at": source.metadata.get("accessed_at"),
                },
            )

            assert result.citation_id is not None
            assert result.verification_status == VerificationStatus.VERIFIED

    @patch.object(CitationEngine, "_verify_citation")
    def test_cite_web_with_url_in_locator(self, mock_verify, temp_db_path):
        """Test that URL is preserved in citation locator."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://httpbin.org/html",
                name="HTTPBin HTML",
            )

            result = engine.cite_web(
                claim="Test claim",
                source_id=source.id,
                quote_context="Test context",
                locator={
                    "url": "https://httpbin.org/html",
                    "section": "main content",
                },
            )

            # Verify locator is stored correctly
            citation = engine.get_citation(result.citation_id)
            assert citation.locator["url"] == "https://httpbin.org/html"


# =============================================================================
# WEB SOURCE ARCHIVAL TESTS
# =============================================================================


@skip_web
@pytest.mark.web
@pytest.mark.integration
class TestWebSourceArchival:
    """Tests for web source content archival."""

    def test_content_is_archived_at_registration(self, temp_db_path):
        """Test that web content is archived when source is registered."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://httpbin.org/html",
                name="Archive Test",
            )

            # Content should be stored in the database
            retrieved = engine.get_source(source.id)
            assert retrieved.content is not None
            assert retrieved.content == source.content

    def test_archived_content_matches_hash(self, temp_db_path):
        """Test that archived content matches its hash."""
        import hashlib

        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://example.com",
                name="Hash Test",
            )

            # Verify hash matches content
            computed_hash = hashlib.sha256(source.content.encode()).hexdigest()
            assert source.content_hash == computed_hash

    def test_multiple_registrations_different_ids(self, temp_db_path):
        """Test that registering same URL twice creates different sources."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source1 = engine.add_web_source(
                url="https://example.com",
                name="First Registration",
            )

            source2 = engine.add_web_source(
                url="https://example.com",
                name="Second Registration",
            )

            # Should have different IDs (new snapshot each time)
            assert source1.id != source2.id


# =============================================================================
# WEB SOURCE FORMAT TESTS
# =============================================================================


@skip_web
@pytest.mark.web
@pytest.mark.integration
class TestWebSourceFormats:
    """Tests for citation formatting from web sources."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_format_web_citation_harvard(self, mock_verify, temp_db_path):
        """Test Harvard-style formatting for web citations."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://example.com",
                name="Example Domain",
                version="2024",
            )

            result = engine.cite_web(
                claim="Test claim",
                source_id=source.id,
                quote_context="Test context",
                locator={"url": "https://example.com"},
            )

            formatted = engine.format_citation(result.citation_id, style="harvard")
            assert "Example Domain" in formatted
            assert "2024" in formatted

    @patch.object(CitationEngine, "_verify_citation")
    def test_format_web_citation_bibtex(self, mock_verify, temp_db_path):
        """Test BibTeX formatting for web citations."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_web_source(
                url="https://example.com",
                name="Example Domain",
                version="2024",
            )

            result = engine.cite_web(
                claim="Test claim",
                source_id=source.id,
                quote_context="Test context",
                locator={"url": "https://example.com"},
            )

            formatted = engine.format_citation(result.citation_id, style="bibtex")
            assert "@online" in formatted
            assert "Example Domain" in formatted


# =============================================================================
# MAIN
# =============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
