"""
PostgreSQL Integration Tests for Citation Engine.

These tests verify the multi-agent mode functionality using a real PostgreSQL database.
They are skipped by default and only run when RUN_POSTGRES_TESTS=true.

Prerequisites:
    1. Start PostgreSQL: podman-compose up -d
    2. Set environment: RUN_POSTGRES_TESTS=true
    3. Run tests: pytest tests/test_integration_postgres.py -v

Run with: pytest tests/test_integration_postgres.py -v
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from citation_engine import CitationEngine, SourceType, VerificationStatus
from citation_engine.models import VerificationResult

# Skip marker for PostgreSQL tests
RUN_POSTGRES_TESTS = os.getenv("RUN_POSTGRES_TESTS", "false").lower() == "true"
skip_postgres = pytest.mark.skipif(
    not RUN_POSTGRES_TESTS,
    reason="PostgreSQL tests disabled. Set RUN_POSTGRES_TESTS=true to enable.",
)

# =============================================================================
# POSTGRESQL CONNECTION TESTS
# =============================================================================


@skip_postgres
@pytest.mark.postgres
@pytest.mark.integration
class TestPostgreSQLConnection:
    """Tests for PostgreSQL database connection."""

    def test_postgresql_connection(self, postgres_db_url, monkeypatch):
        """Test PostgreSQL connection is established correctly."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        engine = CitationEngine(mode="multi-agent")
        engine._connect()

        assert engine._conn is not None
        assert engine._db_type == "postgresql"

        engine.close()
        assert engine._conn is None

    def test_postgresql_context_manager(self, postgres_db_url, monkeypatch):
        """Test context manager with PostgreSQL."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            assert engine._conn is not None
            assert engine._db_type == "postgresql"

        assert engine._conn is None

    def test_postgresql_schema_creation(self, postgres_db_url, monkeypatch):
        """Test that PostgreSQL schema is created correctly."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            # Query to check tables exist
            result = engine._query("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name IN ('sources', 'citations', 'schema_migrations')
            """)
            table_names = [row["table_name"] for row in result]

            assert "sources" in table_names
            assert "citations" in table_names
            assert "schema_migrations" in table_names

    def test_postgresql_enum_types(self, postgres_db_url, monkeypatch):
        """Test that PostgreSQL ENUM types are created."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            result = engine._query("""
                SELECT typname FROM pg_type
                WHERE typname IN ('source_type', 'confidence_level',
                                  'extraction_method', 'verification_status')
            """)
            type_names = [row["typname"] for row in result]

            assert "source_type" in type_names
            assert "confidence_level" in type_names
            assert "extraction_method" in type_names
            assert "verification_status" in type_names


# =============================================================================
# SOURCE REGISTRATION TESTS (PostgreSQL)
# =============================================================================


@skip_postgres
@pytest.mark.postgres
@pytest.mark.integration
class TestPostgreSQLSourceRegistration:
    """Tests for source registration with PostgreSQL backend."""

    def test_add_doc_source_postgresql(
        self, postgres_db_url, sample_text_file, monkeypatch
    ):
        """Test document source registration in PostgreSQL."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            source = engine.add_doc_source(
                file_path=sample_text_file,
                name="Test Document PostgreSQL",
                version="1.0",
            )

            assert source.id is not None
            assert source.type == SourceType.DOCUMENT
            assert source.name == "Test Document PostgreSQL"

            # Verify it's in the database
            retrieved = engine.get_source(source.id)
            assert retrieved is not None
            assert retrieved.name == "Test Document PostgreSQL"

    def test_add_db_source_postgresql(self, postgres_db_url, monkeypatch):
        """Test database source registration in PostgreSQL."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            content = json.dumps([
                {"id": 1, "name": "Item 1"},
                {"id": 2, "name": "Item 2"},
            ])

            source = engine.add_db_source(
                identifier="test_database.items_pg",
                name="Test Items PostgreSQL",
                content=content,
                query="SELECT * FROM items",
            )

            assert source.id is not None
            assert source.type == SourceType.DATABASE
            assert source.metadata["query"] == "SELECT * FROM items"

    def test_add_custom_source_postgresql(self, postgres_db_url, monkeypatch):
        """Test custom source registration in PostgreSQL."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            source = engine.add_custom_source(
                name="Analysis Matrix PostgreSQL",
                content="Analysis shows 70% microservices adoption",
                description="Computed analysis",
            )

            assert source.id is not None
            assert source.type == SourceType.CUSTOM

    def test_list_sources_postgresql(
        self, postgres_db_url, sample_text_file, monkeypatch
    ):
        """Test listing sources from PostgreSQL."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            # Add sources
            engine.add_doc_source(sample_text_file, name="List Test Doc 1")
            engine.add_custom_source(name="List Test Custom 1", content="test")

            sources = engine.list_sources()
            assert len(sources) >= 2

            # Filter by type
            doc_sources = engine.list_sources(source_type="document")
            assert all(s.type == SourceType.DOCUMENT for s in doc_sources)


# =============================================================================
# CITATION TESTS (PostgreSQL)
# =============================================================================


@skip_postgres
@pytest.mark.postgres
@pytest.mark.integration
class TestPostgreSQLCitations:
    """Tests for citation operations with PostgreSQL backend."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_cite_doc_postgresql(
        self, mock_verify, postgres_db_url, sample_text_file, monkeypatch
    ):
        """Test document citation in PostgreSQL."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.95,
            matched_location={"line": 10},
            reasoning="Quote found in source",
        )
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            source = engine.add_doc_source(sample_text_file, name="Citation Test Doc")

            result = engine.cite_doc(
                claim="Companies must store data for 10 years",
                source_id=source.id,
                quote_context="Companies must store transaction data for 10 years according to regulations.",
                verbatim_quote="Companies must store transaction data for 10 years",
                locator={"section": "Section 2"},
            )

            assert result.citation_id is not None
            assert result.verification_status == VerificationStatus.VERIFIED

            # Verify citation is in database
            citation = engine.get_citation(result.citation_id)
            assert citation is not None
            assert citation.claim == "Companies must store data for 10 years"

    @patch.object(CitationEngine, "_verify_citation")
    def test_list_citations_postgresql(
        self, mock_verify, postgres_db_url, sample_text_file, monkeypatch
    ):
        """Test listing citations from PostgreSQL."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            source = engine.add_doc_source(sample_text_file, name="List Citations Test")

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

            citations = engine.list_citations(source_id=source.id)
            assert len(citations) >= 2

            # Filter by status
            verified = engine.list_citations(verification_status="verified")
            assert all(
                c.verification_status == VerificationStatus.VERIFIED for c in verified
            )


# =============================================================================
# JSONB FUNCTIONALITY TESTS
# =============================================================================


@skip_postgres
@pytest.mark.postgres
@pytest.mark.integration
class TestPostgreSQLJSONB:
    """Tests for PostgreSQL-specific JSONB functionality."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_jsonb_locator_storage(
        self, mock_verify, postgres_db_url, sample_text_file, monkeypatch
    ):
        """Test that locator is properly stored as JSONB."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            source = engine.add_doc_source(sample_text_file, name="JSONB Test Doc")

            complex_locator = {
                "page": 24,
                "section": "§ 8.1",
                "paragraph": 3,
                "line_range": [10, 15],
                "metadata": {"chapter": "Regulations", "subsection": "Data Retention"},
            }

            result = engine.cite_doc(
                claim="Test claim",
                source_id=source.id,
                quote_context="Test context",
                locator=complex_locator,
            )

            # Retrieve and verify locator is intact
            citation = engine.get_citation(result.citation_id)
            assert citation.locator == complex_locator
            assert citation.locator["line_range"] == [10, 15]
            assert citation.locator["metadata"]["chapter"] == "Regulations"

    def test_jsonb_metadata_storage(
        self, postgres_db_url, sample_text_file, monkeypatch
    ):
        """Test that metadata is properly stored as JSONB."""
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            complex_metadata = {
                "author": "Test Author",
                "publication_date": "2024-01-15",
                "tags": ["regulations", "compliance", "data"],
                "references": [
                    {"id": 1, "title": "Reference 1"},
                    {"id": 2, "title": "Reference 2"},
                ],
            }

            source = engine.add_doc_source(
                file_path=sample_text_file,
                name="JSONB Metadata Test",
                metadata=complex_metadata,
            )

            # Retrieve and verify metadata is intact
            retrieved = engine.get_source(source.id)
            assert retrieved.metadata == complex_metadata
            assert retrieved.metadata["tags"] == ["regulations", "compliance", "data"]


# =============================================================================
# CONCURRENT ACCESS TESTS
# =============================================================================


@skip_postgres
@pytest.mark.postgres
@pytest.mark.integration
class TestPostgreSQLConcurrency:
    """Tests for concurrent database access (multi-agent scenarios)."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_multiple_connections(
        self, mock_verify, postgres_db_url, sample_text_file, monkeypatch
    ):
        """Test multiple simultaneous connections."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        # Simulate two agents connecting simultaneously
        with CitationEngine(mode="multi-agent") as engine1:
            with CitationEngine(mode="multi-agent") as engine2:
                # Both engines should be connected
                assert engine1._conn is not None
                assert engine2._conn is not None

                # Agent 1 creates a source
                source1 = engine1.add_doc_source(
                    sample_text_file, name="Agent1 Source"
                )

                # Agent 2 should be able to see it
                sources = engine2.list_sources()
                source_names = [s.name for s in sources]
                assert "Agent1 Source" in source_names

                # Agent 2 creates a citation on Agent 1's source
                engine2.cite_doc(
                    claim="Cross-agent citation",
                    source_id=source1.id,
                    quote_context="Test context",
                    locator={"page": 1},
                )

                # Agent 1 should be able to see the citation
                citations = engine1.get_citations_for_source(source1.id)
                assert any(c.claim == "Cross-agent citation" for c in citations)


# =============================================================================
# CLEANUP TESTS
# =============================================================================


@skip_postgres
@pytest.mark.postgres
@pytest.mark.integration
class TestPostgreSQLCleanup:
    """Tests for database cleanup and statistics."""

    @patch.object(CitationEngine, "_verify_citation")
    def test_get_statistics(
        self, mock_verify, postgres_db_url, sample_text_file, monkeypatch
    ):
        """Test getting database statistics."""
        mock_verify.return_value = VerificationResult(
            is_verified=True,
            similarity_score=0.9,
            reasoning="OK",
        )
        monkeypatch.setenv("CITATION_DB_URL", postgres_db_url)

        with CitationEngine(mode="multi-agent") as engine:
            # Create some data
            source = engine.add_doc_source(sample_text_file, name="Stats Test")
            engine.cite_doc(
                claim="Stats test claim",
                source_id=source.id,
                quote_context="Context",
                locator={"page": 1},
            )

            stats = engine.get_statistics()

            assert "sources" in stats
            assert "citations" in stats
            assert stats["sources"]["total"] >= 1
            assert stats["citations"]["total"] >= 1


# =============================================================================
# MAIN
# =============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
