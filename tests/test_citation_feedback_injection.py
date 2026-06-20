"""Unit tests for citation-verification feedback injection (Phase 2b / D4).

Pure-function coverage (no DB/LLM): the formatter, the synthetic tool-call pair,
and the exclusion checkers. The DB-driven retrieval + end-to-end write-back are
covered by the gated Postgres round-trip (tests/citation_engine/).
"""

from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from src.core.citation_feedback_injection import (
    CITATION_FEEDBACK_TOOL_CALL_ID_PREFIX,
    create_citation_feedback_injection_messages,
    format_failed_citations,
    is_citation_feedback_injection_message,
)
from src.core.workspace_injection import is_workspace_injection_message


def _cit(cid, claim, notes="quote not found", score=0.2):
    return SimpleNamespace(
        id=cid, claim=claim, verification_notes=notes, similarity_score=score
    )


class TestFormatFailedCitations:
    def test_lists_each_failed_citation_with_reason(self):
        block = format_failed_citations(
            [_cit(3, "Records retained for 10 years", notes="not in source")]
        )
        assert "edit_citation" in block
        assert "[3]" in block
        assert "Records retained for 10 years" in block
        assert "not in source" in block

    def test_truncates_long_claim_and_reason(self):
        block = format_failed_citations([_cit(1, "C" * 300, notes="R" * 400)])
        assert "…" in block
        # Claim/reason are bounded, not dumped wholesale.
        assert "C" * 300 not in block
        assert "R" * 400 not in block

    def test_collapses_overflow_into_count(self):
        cits = [_cit(i, f"claim {i}") for i in range(8)]
        block = format_failed_citations(cits, limit=5)
        assert "and 3 more" in block
        # Only the first 5 are listed explicitly.
        assert "[5]" not in block

    def test_missing_score_omits_similarity(self):
        block = format_failed_citations([_cit(2, "claim", score=None)])
        assert "similarity" not in block


class TestInjectionPair:
    def test_pair_has_tool_call_and_prefix(self):
        ai, tool = create_citation_feedback_injection_messages("failed: [1]")
        assert (
            ai.tool_calls and ai.tool_calls[0]["name"] == "check_citation_verification"
        )
        tid = ai.tool_calls[0]["id"]
        assert tid.startswith(CITATION_FEEDBACK_TOOL_CALL_ID_PREFIX)
        assert tool.tool_call_id == tid
        assert tool.content == "failed: [1]"

    def test_is_citation_feedback_recognizes_pair(self):
        ai, tool = create_citation_feedback_injection_messages("x")
        assert is_citation_feedback_injection_message(ai)
        assert is_citation_feedback_injection_message(tool)
        assert not is_citation_feedback_injection_message(HumanMessage(content="hi"))

    def test_unified_checker_excludes_feedback_pair(self):
        # is_workspace_injection_message must exclude citation feedback so it's
        # dropped from summarization/extraction like other transient injections.
        ai, tool = create_citation_feedback_injection_messages("x")
        assert is_workspace_injection_message(ai)
        assert is_workspace_injection_message(tool)
        assert not is_workspace_injection_message(HumanMessage(content="real msg"))
