"""Tests for unified search: keyword, semantic, hybrid RRF, and evidence labeling."""

import os
import tempfile

import pytest
from citation_engine import CitationContext, CitationEngine, SearchResult, SearchResults
from citation_engine.search import (
    _RankedHit,
    label_evidence,
    overall_label,
    rrf_merge,
)


# =============================================================================
# RRF Merge Tests
# =============================================================================


class TestRRFMerge:
    """Tests for Reciprocal Rank Fusion merge."""

    def _hit(self, source_id, name="src", chunk="text", in_kw=True, in_sem=False, score=1.0):
        return _RankedHit(
            source_id=source_id,
            source_name=name,
            source_type="document",
            chunk_text=chunk,
            page_reference=None,
            rrf_score=score,
            in_keyword=in_kw,
            in_semantic=in_sem,
        )

    def test_keyword_only(self):
        """Keyword-only results get RRF scores."""
        kw = [self._hit(1, chunk="a"), self._hit(2, chunk="b")]
        result = rrf_merge(kw, [], top_k=10)
        assert len(result) == 2
        # First result should have higher score
        assert result[0].rrf_score > result[1].rrf_score

    def test_semantic_only(self):
        """Semantic-only results get RRF scores."""
        sem = [self._hit(1, chunk="a", in_kw=False, in_sem=True)]
        result = rrf_merge([], sem, top_k=10)
        assert len(result) == 1
        assert result[0].in_semantic is True

    def test_both_boost(self):
        """Items in both lists get boosted score."""
        kw = [self._hit(1, chunk="same content")]
        sem = [self._hit(1, chunk="same content", in_kw=False, in_sem=True)]
        result = rrf_merge(kw, sem, top_k=10)
        assert len(result) == 1
        assert result[0].in_keyword is True
        assert result[0].in_semantic is True
        # Score should be sum of both RRF scores
        single_score = 1.0 / (60 + 1)
        assert result[0].rrf_score == pytest.approx(2 * single_score)

    def test_dedup_by_source_and_chunk(self):
        """Same source_id + chunk prefix deduplicates."""
        kw = [self._hit(1, chunk="exact same text here")]
        sem = [self._hit(1, chunk="exact same text here", in_kw=False, in_sem=True)]
        result = rrf_merge(kw, sem, top_k=10)
        assert len(result) == 1

    def test_different_sources_not_merged(self):
        """Different source_ids are kept separate."""
        kw = [self._hit(1, chunk="text")]
        sem = [self._hit(2, chunk="text", in_kw=False, in_sem=True)]
        result = rrf_merge(kw, sem, top_k=10)
        assert len(result) == 2

    def test_top_k_limit(self):
        """Results are limited to top_k."""
        kw = [self._hit(i, chunk=f"chunk {i}") for i in range(20)]
        result = rrf_merge(kw, [], top_k=5)
        assert len(result) == 5

    def test_empty_inputs(self):
        """Empty inputs return empty."""
        assert rrf_merge([], []) == []


# =============================================================================
# Evidence Labeling Tests
# =============================================================================


class TestEvidenceLabeling:
    """Tests for evidence label assignment."""

    def _hit(self, score, in_kw=True, in_sem=False, source_id=1):
        return _RankedHit(
            source_id=source_id,
            source_name="test",
            source_type="document",
            chunk_text="some text",
            page_reference=None,
            rrf_score=score,
            in_keyword=in_kw,
            in_semantic=in_sem,
        )

    def test_both_methods_gets_high(self):
        """Hit found by both keyword and semantic gets HIGH."""
        hits = [self._hit(0.5, in_kw=True, in_sem=True)]
        results = label_evidence(hits)
        assert results[0].evidence_label == "HIGH"
        assert "keyword and semantic" in results[0].evidence_reason

    def test_high_score_gets_high(self):
        """High-scoring hit in single method gets HIGH."""
        hits = [self._hit(1.0, in_kw=True, in_sem=False)]
        results = label_evidence(hits)
        assert results[0].evidence_label == "HIGH"

    def test_medium_score_gets_medium(self):
        """Medium-scoring hit gets MEDIUM."""
        hits = [
            self._hit(1.0, in_kw=True),  # HIGH (for normalization)
            self._hit(0.4, in_kw=True, source_id=2),  # MEDIUM (0.4/1.0 = 0.4 > 0.3)
        ]
        results = label_evidence(hits)
        assert results[1].evidence_label == "MEDIUM"

    def test_low_score_gets_low(self):
        """Low-scoring hit gets LOW."""
        hits = [
            self._hit(1.0, in_kw=True),  # HIGH (for normalization)
            self._hit(0.1, in_kw=True, source_id=2),  # LOW (0.1/1.0 = 0.1 < 0.3)
        ]
        results = label_evidence(hits)
        assert results[1].evidence_label == "LOW"

    def test_empty_input(self):
        """Empty input returns empty."""
        assert label_evidence([]) == []

    def test_returns_search_results(self):
        """Results are SearchResult instances."""
        hits = [self._hit(1.0)]
        results = label_evidence(hits)
        assert isinstance(results[0], SearchResult)


# =============================================================================
# Overall Label Tests
# =============================================================================


class TestOverallLabel:
    """Tests for aggregate evidence summary."""

    def _result(self, label, name="source"):
        return SearchResult(
            source_id=1,
            source_name=name,
            source_type="document",
            chunk_text="text",
            evidence_label=label,
            evidence_reason="test",
            score=1.0,
        )

    def test_no_results(self):
        assert overall_label([]) == "No results found"

    def test_multiple_high(self):
        results = [self._result("HIGH", "doc1"), self._result("HIGH", "doc2")]
        label = overall_label(results)
        assert label.startswith("HIGH")
        assert "2 sources" in label

    def test_single_high(self):
        results = [self._result("HIGH", "doc1")]
        label = overall_label(results)
        assert label.startswith("HIGH")
        assert "1 strong source" in label

    def test_medium_results(self):
        results = [self._result("MEDIUM"), self._result("MEDIUM")]
        label = overall_label(results)
        assert label.startswith("MEDIUM")

    def test_low_only(self):
        results = [self._result("LOW")]
        label = overall_label(results)
        assert label.startswith("LOW")


# =============================================================================
# Integration Tests (SQLite / keyword-only)
# =============================================================================


class TestSearchLibraryIntegration:
    """Integration tests for search_library on SQLite (keyword mode only)."""

    @pytest.fixture
    def engine(self):
        """Create engine with test sources."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        ctx = CitationContext(session_id="test-search-job")
        engine = CitationEngine(mode="basic", db_path=db_path, context=ctx)
        engine._connect()

        # Register test sources with distinct content
        engine.add_custom_source(
            name="GoBD Handbook",
            content="Aufbewahrungsfrist beträgt 10 Jahre für steuerrelevante Dokumente gemäß GoBD",
        )
        engine.add_custom_source(
            name="GDPR Guide",
            content="Data retention period for personal data should be minimized under GDPR Article 5",
        )
        engine.add_custom_source(
            name="Tax Commentary",
            content="§ 147 AO defines the retention requirements for business documents in Germany",
        )

        yield engine
        engine.close()
        os.unlink(db_path)

    def test_keyword_search_finds_matches(self, engine):
        """Keyword search finds sources containing the query terms."""
        results = engine.search_library(query="retention", mode="keyword")
        assert isinstance(results, SearchResults)
        assert len(results.results) >= 1
        assert results.mode == "keyword"
        # GDPR Guide and Tax Commentary both mention "retention"
        source_names = [r.source_name for r in results.results]
        assert "GDPR Guide" in source_names or "Tax Commentary" in source_names

    def test_keyword_search_no_matches(self, engine):
        """Search for non-existent term returns empty."""
        results = engine.search_library(query="quantum_physics_xyz", mode="keyword")
        assert len(results.results) == 0
        assert "No results" in results.overall_label

    def test_hybrid_falls_back_to_keyword_on_sqlite(self, engine):
        """Hybrid mode falls back to keyword on SQLite."""
        results = engine.search_library(query="retention", mode="hybrid")
        assert results.mode == "keyword"  # Fell back from hybrid
        assert len(results.results) >= 1

    def test_semantic_falls_back_to_keyword_on_sqlite(self, engine):
        """Semantic mode falls back to keyword on SQLite."""
        results = engine.search_library(query="retention", mode="semantic")
        assert results.mode == "keyword"  # Fell back from semantic

    def test_search_with_tags_filter(self, engine):
        """Tag filter narrows results."""
        # Tag one source
        engine.tag_source(source_id=1, tags=["compliance"])

        # Search with tag filter — only tagged source should match
        results = engine.search_library(
            query="Aufbewahrungsfrist", mode="keyword", tags=["compliance"]
        )
        assert len(results.results) >= 1
        for r in results.results:
            assert r.source_id == 1

    def test_search_with_source_type_filter(self, engine):
        """Source type filter works."""
        results = engine.search_library(query="retention", mode="keyword", source_type="custom")
        assert len(results.results) >= 1

    def test_search_scope_annotations(self, engine):
        """Scope=annotations searches annotation content."""
        # Add an annotation to source 1
        engine.annotate_source(
            source_id=1,
            content="This source discusses Aufbewahrungspflicht for 10 years",
            annotation_type="note",
        )

        results = engine.search_library(
            query="Aufbewahrungspflicht", mode="keyword", scope="annotations"
        )
        assert len(results.results) >= 1

    def test_search_scope_all(self, engine):
        """Scope=all searches both content and annotations."""
        engine.annotate_source(
            source_id=1,
            content="Unique annotation keyword xylophone",
            annotation_type="note",
        )

        results = engine.search_library(query="xylophone", mode="keyword", scope="all")
        assert len(results.results) >= 1

    def test_empty_query_raises(self, engine):
        """Empty query raises ValueError."""
        with pytest.raises(ValueError, match="empty"):
            engine.search_library(query="")

    def test_invalid_mode_raises(self, engine):
        """Invalid mode raises ValueError."""
        with pytest.raises(ValueError, match="Invalid search mode"):
            engine.search_library(query="test", mode="invalid")

    def test_invalid_scope_raises(self, engine):
        """Invalid scope raises ValueError."""
        with pytest.raises(ValueError, match="Invalid scope"):
            engine.search_library(query="test", scope="invalid")

    def test_top_k_limits_results(self, engine):
        """top_k parameter limits result count."""
        results = engine.search_library(query="retention", mode="keyword", top_k=1)
        assert len(results.results) <= 1

    def test_results_have_evidence_labels(self, engine):
        """All results have evidence labels."""
        results = engine.search_library(query="retention", mode="keyword")
        for r in results.results:
            assert r.evidence_label in ("HIGH", "MEDIUM", "LOW")
            assert r.evidence_reason

    def test_results_to_dict(self, engine):
        """SearchResults.to_dict() works."""
        results = engine.search_library(query="retention", mode="keyword")
        d = results.to_dict()
        assert d["query"] == "retention"
        assert d["mode"] == "keyword"
        assert "count" in d
        assert isinstance(d["results"], list)
