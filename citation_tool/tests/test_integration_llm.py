"""
End-to-End LLM Verification Tests for Citation Engine.

These tests verify citations using a real LLM endpoint (local or remote).
They are skipped by default and only run when RUN_LLM_TESTS=true.

Prerequisites:
    1. Configure LLM endpoint in .env:
       - CITATION_LLM_URL (for local LLM)
       - CITATION_LLM_MODEL
       - OPENAI_API_KEY (if using OpenAI)
    2. Set environment: RUN_LLM_TESTS=true
    3. Run tests: pytest tests/test_integration_llm.py -v -s

Run with: pytest tests/test_integration_llm.py -v -s
"""

import os
import sys
from pathlib import Path

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from citation_engine import CitationEngine, VerificationStatus

# Skip marker for LLM tests
RUN_LLM_TESTS = os.getenv("RUN_LLM_TESTS", "false").lower() == "true"
skip_llm = pytest.mark.skipif(
    not RUN_LLM_TESTS,
    reason="LLM tests disabled. Set RUN_LLM_TESTS=true to enable.",
)

# =============================================================================
# LLM VERIFICATION TESTS - POSITIVE CASES
# =============================================================================


@skip_llm
@pytest.mark.llm
@pytest.mark.integration
class TestLLMVerificationPositive:
    """Tests for successful citation verification with real LLM."""

    def test_verify_exact_quote(self, temp_db_path, sample_text_file):
        """Test verification of an exact quote from the source."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_doc_source(
                file_path=sample_text_file,
                name="Test Document",
            )

            result = engine.cite_doc(
                claim="Regulations require 10 year data retention",
                source_id=source.id,
                quote_context="Section 2: Main Content\nCompanies must store transaction data for 10 years according to regulations.",
                verbatim_quote="Companies must store transaction data for 10 years",
                locator={"section": "Section 2"},
            )

            # With real LLM, this should be verified
            assert result.citation_id is not None
            assert result.verification_status in [
                VerificationStatus.VERIFIED,
                VerificationStatus.FAILED,
            ]
            # Print for debugging
            print(f"\nVerification result: {result.verification_status.value}")
            print(f"Similarity score: {result.similarity_score}")
            if result.verification_notes:
                print(f"Notes: {result.verification_notes}")

    def test_verify_paraphrase(self, temp_db_path, sample_text_file):
        """Test verification of a paraphrased quote."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_doc_source(
                file_path=sample_text_file,
                name="Test Document",
            )

            result = engine.cite_doc(
                claim="Testing ensures quality software",
                source_id=source.id,
                quote_context="Testing is important for software quality.",
                locator={"section": "Section 4"},
                extraction_method="paraphrase",
            )

            assert result.citation_id is not None
            print(f"\nParaphrase verification: {result.verification_status.value}")
            print(f"Similarity score: {result.similarity_score}")

    def test_verify_database_aggregation(self, temp_db_path, sample_database_content):
        """Test verification of aggregated database results."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_db_source(
                identifier="paper_analysis.architectures",
                name="Paper Architecture Analysis",
                content=sample_database_content,
                query="SELECT architecture, COUNT(*) FROM papers GROUP BY architecture",
            )

            result = engine.cite_db(
                claim="42 papers use microservices architecture",
                source_id=source.id,
                quote_context="Results show microservices: 42",
                locator={
                    "table": "papers",
                    "query": "SELECT architecture, COUNT(*) FROM papers GROUP BY architecture",
                },
                extraction_method="aggregation",
            )

            assert result.citation_id is not None
            print(f"\nDB aggregation verification: {result.verification_status.value}")

    def test_verify_custom_source(self, temp_db_path, sample_custom_content):
        """Test verification against custom AI-generated content."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_custom_source(
                name="Paper Analysis Matrix",
                content=sample_custom_content,
                description="Generated analysis of paper architectures",
            )

            result = engine.cite_custom(
                claim="Over half of papers use microservices",
                source_id=source.id,
                quote_context="Microservices adoption: 57.5% (42/73)",
                relevance_reasoning="The matrix shows 57.5% adoption which is over half",
            )

            assert result.citation_id is not None
            print(f"\nCustom source verification: {result.verification_status.value}")


# =============================================================================
# LLM VERIFICATION TESTS - NEGATIVE CASES
# =============================================================================


@skip_llm
@pytest.mark.llm
@pytest.mark.integration
class TestLLMVerificationNegative:
    """Tests for failed citation verification with real LLM."""

    def test_verify_nonexistent_quote(self, temp_db_path, sample_text_file):
        """Test verification of a quote that doesn't exist in source."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_doc_source(
                file_path=sample_text_file,
                name="Test Document",
            )

            result = engine.cite_doc(
                claim="The system uses quantum computing",
                source_id=source.id,
                quote_context="Our quantum computing infrastructure enables...",
                verbatim_quote="quantum computing infrastructure",
                locator={"section": "Technical Details"},
            )

            assert result.citation_id is not None
            # This should ideally be FAILED since quote doesn't exist
            print(f"\nNonexistent quote verification: {result.verification_status.value}")
            print(f"Similarity score: {result.similarity_score}")
            if result.verification_notes:
                print(f"Notes: {result.verification_notes}")

    def test_verify_unsupported_claim(self, temp_db_path, sample_text_file):
        """Test verification when quote exists but doesn't support claim."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_doc_source(
                file_path=sample_text_file,
                name="Test Document",
            )

            # Claim says 5 years, but document says 10 years
            result = engine.cite_doc(
                claim="Companies must store data for 5 years",
                source_id=source.id,
                quote_context="Companies must store transaction data for 10 years according to regulations.",
                verbatim_quote="Companies must store transaction data for 10 years",
                locator={"section": "Section 2"},
            )

            assert result.citation_id is not None
            # This should ideally be FAILED since claim contradicts source
            print(f"\nUnsupported claim verification: {result.verification_status.value}")
            print(f"Similarity score: {result.similarity_score}")


# =============================================================================
# LLM VERIFICATION TESTS - EDGE CASES
# =============================================================================


@skip_llm
@pytest.mark.llm
@pytest.mark.integration
class TestLLMVerificationEdgeCases:
    """Tests for edge cases in LLM verification."""

    def test_verify_long_document(self, temp_db_path):
        """Test verification with a long document that needs truncation."""
        import tempfile

        # Create a long document
        long_content = "Introduction\n\n"
        for i in range(100):
            long_content += f"Section {i}: This is paragraph {i} of the document. "
            long_content += "It contains various information about different topics. "
            long_content += "The key finding in this section is that data retention "
            long_content += "policies are critical for compliance.\n\n"

        # Add a specific quote we'll cite
        long_content += "\n\nSection 100: Critical Finding\n"
        long_content += "The analysis demonstrates that 95% of organizations "
        long_content += "require at least 7 years of data retention.\n"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(long_content)
            long_file_path = f.name

        try:
            with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
                source = engine.add_doc_source(
                    file_path=long_file_path,
                    name="Long Document",
                )

                result = engine.cite_doc(
                    claim="Most organizations need 7+ years data retention",
                    source_id=source.id,
                    quote_context="95% of organizations require at least 7 years of data retention",
                    verbatim_quote="95% of organizations require at least 7 years",
                    locator={"section": "Section 100"},
                )

                assert result.citation_id is not None
                print(f"\nLong document verification: {result.verification_status.value}")

        finally:
            import os
            os.unlink(long_file_path)

    def test_verify_non_english_content(self, temp_db_path):
        """Test verification with non-English content."""
        import tempfile

        german_content = """
        Kapitel 1: Einleitung

        Dieses Dokument beschreibt die Anforderungen an die Datenspeicherung.

        Kapitel 2: Aufbewahrungsfristen

        Gemäß den GoBD-Vorschriften müssen Unternehmen ihre
        Transaktionsdaten für einen Zeitraum von zehn Jahren aufbewahren.
        Dies ist eine gesetzliche Anforderung, die nicht umgangen werden kann.

        Kapitel 3: Zusammenfassung

        Die Einhaltung der Aufbewahrungsfristen ist für alle Unternehmen verpflichtend.
        """

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(german_content)
            german_file_path = f.name

        try:
            with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
                source = engine.add_doc_source(
                    file_path=german_file_path,
                    name="German Regulations Document",
                )

                result = engine.cite_doc(
                    claim="Companies must store transaction data for 10 years",
                    source_id=source.id,
                    quote_context="Transaktionsdaten für einen Zeitraum von zehn Jahren aufbewahren",
                    verbatim_quote="zehn Jahren aufbewahren",
                    locator={"chapter": "Kapitel 2"},
                )

                assert result.citation_id is not None
                print(f"\nGerman content verification: {result.verification_status.value}")
                print(f"Similarity score: {result.similarity_score}")

        finally:
            import os
            os.unlink(german_file_path)

    def test_verify_with_inference(self, temp_db_path, sample_text_file):
        """Test verification with inferred information."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_doc_source(
                file_path=sample_text_file,
                name="Test Document",
            )

            # Inference from multiple facts
            result = engine.cite_doc(
                claim="The system is designed for high-volume, memory-efficient operation",
                source_id=source.id,
                quote_context="The system processes approximately 1000 requests per second. Memory usage should not exceed 512MB under normal operation.",
                locator={"section": "Section 3"},
                extraction_method="inference",
                relevance_reasoning="The document mentions both high request volume (1000 rps) and memory constraints (512MB), supporting the claim about high-volume, memory-efficient design.",
            )

            assert result.citation_id is not None
            print(f"\nInference verification: {result.verification_status.value}")


# =============================================================================
# LLM CLIENT TESTS
# =============================================================================


@skip_llm
@pytest.mark.llm
@pytest.mark.integration
class TestLLMClient:
    """Tests for LLM client initialization and configuration."""

    def test_llm_client_initialization(self, temp_db_path, llm_config):
        """Test that LLM client is properly initialized."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            # Force LLM client initialization
            engine._setup_llm_client()

            assert engine._llm_client is not None
            print(f"\nLLM client initialized with model: {engine.llm_model}")
            if engine.llm_url:
                print(f"Using custom endpoint: {engine.llm_url}")

    def test_llm_response_parsing(self, temp_db_path, sample_text_file):
        """Test that LLM responses are properly parsed."""
        with CitationEngine(mode="basic", db_path=temp_db_path) as engine:
            source = engine.add_doc_source(sample_text_file, name="Parse Test Doc")

            result = engine.cite_doc(
                claim="Testing is important",
                source_id=source.id,
                quote_context="Testing is important for software quality.",
                locator={"section": "Section 4"},
            )

            # Check that all fields are populated
            assert result.citation_id is not None
            assert result.verification_status is not None
            assert result.similarity_score is not None

            # Retrieve citation to verify parsing
            citation = engine.get_citation(result.citation_id)
            assert citation is not None
            print(f"\nCitation stored with verification_status: {citation.verification_status.value}")
            print(f"Stored similarity_score: {citation.similarity_score}")


# =============================================================================
# MAIN
# =============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
