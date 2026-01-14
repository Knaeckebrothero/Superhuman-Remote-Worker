"""
Unit tests for CitationEngine core functionality.

Tests cover:
- Database connection (SQLite mode)
- Source registration (document, database, custom)
- Citation creation
- Verification (mocked)
- Retrieval methods
- Export methods

Run with: pytest tests/test_engine.py -v
"""

import json
import os

# Add src to path for imports
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from citation_engine import (
    CitationEngine,
    SourceType,
    VerificationStatus,
)

# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def temp_db_path():
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def engine(temp_db_path):
    """Create a CitationEngine instance with a temp database."""
    engine = CitationEngine(mode="basic", db_path=temp_db_path)
    engine._connect()
    yield engine
    engine.close()


@pytest.fixture
def sample_text_file():
    """Create a sample text file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("""
        This is a sample document for testing.

        Section 1: Introduction
        The purpose of this document is to provide test content.

        Section 2: Main Content
        Companies must store transaction data for 10 years according to regulations.
        This is an important requirement that must be followed.

        Section 3: Conclusion
        Testing is important for software quality.
        """)
        text_path = f.name
    yield text_path
    if os.path.exists(text_path):
        os.unlink(text_path)


# =============================================================================
# DATABASE CONNECTION TESTS
# =============================================================================


class TestDatabaseConnection:
    """Tests for database connection functionality."""

    def test_sqlite_connection(self, temp_db_path):
        """Test SQLite connection is established correctly."""
        engine = CitationEngine(mode="basic", db_path=temp_db_path)
        engine._connect()

        assert engine._conn is not None
        assert engine._db_type == "sqlite"

        engine.close()
        assert engine._conn is None

    def test_context_manager(self, temp_db_path):
        """Test context manager properly opens and closes connection."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            assert engine._conn is not None

        # After exiting context, connection should be closed
        assert engine._conn is None

    def test_schema_creation(self, engine):
        """Test that database schema is created correctly."""
        # Query to check tables exist
        result = engine._query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sources', 'citations')"
        )
        table_names = [row["name"] for row in result]

        assert "sources" in table_names
        assert "citations" in table_names


# =============================================================================
# SOURCE REGISTRATION TESTS
# =============================================================================


class TestSourceRegistration:
    """Tests for source registration functionality."""

    def test_add_doc_source(self, engine, sample_text_file):
        """Test document source registration."""
        source = engine.add_doc_source(
            file_path=sample_text_file,
            name="Test Document",
            version="1.0",
        )

        assert source.id is not None
        assert source.type == SourceType.DOCUMENT
        assert source.name == "Test Document"
        assert source.version == "1.0"
        assert "Companies must store transaction data" in source.content
        assert source.content_hash is not None

    def test_add_doc_source_default_name(self, engine, sample_text_file):
        """Test document source uses filename as default name."""
        source = engine.add_doc_source(file_path=sample_text_file)

        assert source.name == os.path.basename(sample_text_file)

    def test_add_doc_source_file_not_found(self, engine):
        """Test error when document file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            engine.add_doc_source(file_path="/nonexistent/file.txt")

    def test_add_db_source(self, engine):
        """Test database source registration."""
        content = json.dumps([
            {"id": 1, "name": "Item 1"},
            {"id": 2, "name": "Item 2"},
        ])

        source = engine.add_db_source(
            identifier="test_database.items",
            name="Test Items",
            content=content,
            query="SELECT * FROM items",
            result_description="2 items returned",
        )

        assert source.id is not None
        assert source.type == SourceType.DATABASE
        assert source.name == "Test Items"
        assert source.metadata["query"] == "SELECT * FROM items"

    def test_add_custom_source(self, engine):
        """Test custom source registration."""
        content = "Analysis shows 70% of papers use microservices architecture."

        source = engine.add_custom_source(
            name="Architecture Analysis Matrix",
            content=content,
            description="Computed analysis of paper architectures",
        )

        assert source.id is not None
        assert source.type == SourceType.CUSTOM
        assert source.name == "Architecture Analysis Matrix"
        assert source.metadata["description"] == "Computed analysis of paper architectures"

    def test_list_sources(self, engine, sample_text_file):
        """Test listing all registered sources."""
        # Add multiple sources
        engine.add_doc_source(sample_text_file, name="Doc 1")
        engine.add_custom_source(name="Custom 1", content="test")
        engine.add_db_source(identifier="db1", name="DB 1", content="test")

        sources = engine.list_sources()
        assert len(sources) == 3

        # Filter by type
        doc_sources = engine.list_sources(source_type="document")
        assert len(doc_sources) == 1
        assert doc_sources[0].name == "Doc 1"

    def test_get_source(self, engine, sample_text_file):
        """Test retrieving a source by ID."""
        created = engine.add_doc_source(sample_text_file, name="Test Doc")

        retrieved = engine.get_source(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.name == "Test Doc"

    def test_get_source_not_found(self, engine):
        """Test retrieving non-existent source returns None."""
        result = engine.get_source(9999)
        assert result is None


# =============================================================================
# CITATION TESTS
# =============================================================================


class TestCitationCreation:
    """Tests for citation creation functionality."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_cite_doc(self, mock_verify, engine, sample_text_file):
        """Test document citation creation."""
        # Mock verification to return success
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=0.95,
            matched_location={"line": 10},
            reasoning="Quote found in source",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc")

        result = engine.cite_doc(
            claim="Companies must store data for 10 years",
            source_id=source.id,
            quote_context="Section 2: Main Content\nCompanies must store transaction data for 10 years according to regulations.",
            verbatim_quote="Companies must store transaction data for 10 years",
            locator={"section": "Section 2"},
            confidence="high",
        )

        assert result.citation_id is not None
        assert result.verification_status == VerificationStatus.VERIFIED
        assert result.similarity_score == 0.95

    @patch.object(CitationEngine, "_verify_citation")
    def test_cite_doc_verification_failed(self, mock_verify, engine, sample_text_file):
        """Test citation when verification fails."""
        mock_verify.return_value = MagicMock(
            is_verified=False,
            similarity_score=0.2,
            matched_location=None,
            reasoning="Quote not found in source",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc")

        result = engine.cite_doc(
            claim="This claim is not in the document",
            source_id=source.id,
            quote_context="Some context that doesn't exist",
            locator={"section": "Nonexistent"},
        )

        assert result.verification_status == VerificationStatus.FAILED
        assert result.verification_notes is not None

    @patch.object(CitationEngine, "_verify_citation")
    def test_cite_db(self, mock_verify, engine):
        """Test database citation creation."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=1.0,
            reasoning="Data matches claim",
        )

        source = engine.add_db_source(
            identifier="paper_analysis",
            name="Paper Analysis DB",
            content="42 of 60 papers use microservices (70%)",
        )

        result = engine.cite_db(
            claim="70% of papers use microservices architecture",
            source_id=source.id,
            quote_context="Analysis shows 42 of 60 papers (70%) use microservices",
            locator={"table": "paper_analysis", "query": "SELECT COUNT(*)..."},
            extraction_method="aggregation",
        )

        assert result.citation_id is not None
        assert result.verification_status == VerificationStatus.VERIFIED

    @patch.object(CitationEngine, "_verify_citation")
    def test_cite_custom(self, mock_verify, engine):
        """Test custom source citation creation."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=1.0,
            reasoning="Custom content matches claim",
        )

        source = engine.add_custom_source(
            name="Analysis Matrix",
            content="Total papers: 60\nMicroservices: 42 (70%)\nMonolith: 18 (30%)",
        )

        result = engine.cite_custom(
            claim="70% of papers use microservices",
            source_id=source.id,
            quote_context="Microservices: 42 (70%)",
        )

        assert result.citation_id is not None
        assert result.verification_status == VerificationStatus.VERIFIED

    def test_cite_invalid_source(self, engine):
        """Test citation with non-existent source raises error."""
        with pytest.raises(ValueError, match="Source not found"):
            engine.cite_doc(
                claim="Some claim",
                source_id=9999,
                quote_context="Some context",
                locator={"page": 1},
            )

    @patch.object(CitationEngine, "_verify_citation")
    def test_citation_reasoning_required(self, mock_verify, engine, sample_text_file, monkeypatch):
        """Test that reasoning is required based on config."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=1.0,
            reasoning="OK",
        )

        # Set reasoning required for low confidence
        monkeypatch.setattr(engine, "reasoning_required", "low")

        source = engine.add_doc_source(sample_text_file, name="Test Doc")

        # Should fail without reasoning when confidence is low
        with pytest.raises(ValueError, match="relevance_reasoning is required"):
            engine.cite_doc(
                claim="Some claim",
                source_id=source.id,
                quote_context="Context",
                locator={"page": 1},
                confidence="low",
                # No relevance_reasoning provided
            )

        # Should succeed with reasoning
        result = engine.cite_doc(
            claim="Some claim",
            source_id=source.id,
            quote_context="Context",
            locator={"page": 1},
            confidence="low",
            relevance_reasoning="This evidence supports the claim because...",
        )
        assert result.citation_id is not None


# =============================================================================
# RETRIEVAL TESTS
# =============================================================================


class TestRetrieval:
    """Tests for citation retrieval functionality."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_get_citation(self, mock_verify, engine, sample_text_file):
        """Test retrieving a citation by ID."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc")
        result = engine.cite_doc(
            claim="Test claim",
            source_id=source.id,
            quote_context="Test context",
            locator={"page": 1},
        )

        citation = engine.get_citation(result.citation_id)

        assert citation is not None
        assert citation.id == result.citation_id
        assert citation.claim == "Test claim"
        assert citation.source_id == source.id

    @patch.object(CitationEngine, "_verify_citation")
    def test_get_citations_for_source(self, mock_verify, engine, sample_text_file):
        """Test retrieving all citations for a source."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc")

        # Create multiple citations
        engine.cite_doc(
            claim="Claim 1",
            source_id=source.id,
            quote_context="Context 1",
            locator={"page": 1},
        )
        engine.cite_doc(
            claim="Claim 2",
            source_id=source.id,
            quote_context="Context 2",
            locator={"page": 2},
        )

        citations = engine.get_citations_for_source(source.id)

        assert len(citations) == 2
        assert all(c.source_id == source.id for c in citations)

    @patch.object(CitationEngine, "_verify_citation")
    def test_list_citations(self, mock_verify, engine, sample_text_file):
        """Test listing citations with filters."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc")
        engine.cite_doc(
            claim="Test claim",
            source_id=source.id,
            quote_context="Test context",
            locator={"page": 1},
        )

        # List all
        all_citations = engine.list_citations()
        assert len(all_citations) >= 1

        # Filter by source
        source_citations = engine.list_citations(source_id=source.id)
        assert len(source_citations) >= 1

        # Filter by status
        verified_citations = engine.list_citations(verification_status="verified")
        assert len(verified_citations) >= 1


# =============================================================================
# EXPORT TESTS
# =============================================================================


class TestExport:
    """Tests for citation export functionality."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_format_citation_inline(self, mock_verify, engine, sample_text_file):
        """Test inline citation formatting."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc")
        result = engine.cite_doc(
            claim="Test claim",
            source_id=source.id,
            quote_context="Test context",
            locator={"page": 1},
        )

        formatted = engine.format_citation(result.citation_id, style="inline")
        assert formatted == f"[{result.citation_id}]"

    @patch.object(CitationEngine, "_verify_citation")
    def test_format_citation_harvard(self, mock_verify, engine, sample_text_file):
        """Test Harvard citation formatting."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc", version="2024")
        result = engine.cite_doc(
            claim="Test claim",
            source_id=source.id,
            quote_context="Test context",
            locator={"page": 1},
        )

        formatted = engine.format_citation(result.citation_id, style="harvard")
        assert "Test Doc" in formatted
        assert "2024" in formatted

    @patch.object(CitationEngine, "_verify_citation")
    def test_format_citation_bibtex(self, mock_verify, engine, sample_text_file):
        """Test BibTeX citation formatting."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc", version="2024")
        result = engine.cite_doc(
            claim="Test claim",
            source_id=source.id,
            quote_context="Test context",
            locator={"page": 1},
        )

        formatted = engine.format_citation(result.citation_id, style="bibtex")
        assert "@" in formatted
        assert "title" in formatted

    @patch.object(CitationEngine, "_verify_citation")
    def test_export_bibliography(self, mock_verify, engine, sample_text_file):
        """Test bibliography export."""
        mock_verify.return_value = MagicMock(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )

        source = engine.add_doc_source(sample_text_file, name="Test Doc", version="2024")

        # Create multiple citations
        for i in range(3):
            engine.cite_doc(
                claim=f"Claim {i}",
                source_id=source.id,
                quote_context=f"Context {i}",
                locator={"page": i},
            )

        bibliography = engine.export_bibliography(style="harvard")

        # Should contain entries for all citations
        assert "Test Doc" in bibliography


# =============================================================================
# MAIN
# =============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
