"""Project backlog / idea pipeline — spec:
docs/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md
"""


class TestPriorityRoundTrip:
    def test_render_emits_priority_word(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {"id": "feature-x", "type": "feature", "content": "body", "priority": 0}
        )
        assert "priority: high" in md

    def test_render_omits_priority_when_absent(self):
        """Existing notes must not gain noise in their frontmatter."""
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md({"id": "d-1", "type": "decision", "content": "body"})
        assert "priority:" not in md

    def test_reindex_parses_priority_word_to_rank(self):
        # note_fields(path, fm, body) is the real signature (kb_reindex.py:157)
        # — it takes already-parsed frontmatter as a dict, not raw markdown.
        from orchestrator.services.kb_reindex import note_fields

        parsed = note_fields(
            "feature-x.md",
            {"id": "feature-x", "type": "feature", "priority": "high"},
            "# T\nbody",
        )
        assert parsed["priority"] == 0

    def test_reindex_defaults_unknown_priority_to_normal(self):
        """Frontmatter is human-editable; a typo must not fail the row."""
        from orchestrator.services.kb_reindex import note_fields

        parsed = note_fields(
            "feature-x.md",
            {"id": "feature-x", "type": "feature", "priority": "URGENT!!"},
            "# T\nb",
        )
        assert parsed["priority"] == 1
