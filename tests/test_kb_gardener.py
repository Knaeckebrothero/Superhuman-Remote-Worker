"""Tests for src/tools/knowledge/gardener.py — the OKF lint/index engine (slice 2).

Pure functions (no workspace, no DB): parse_note_md, lint_kb, render_index_md.
"""


# =============================================================================
# parse_note_md — inverse of _render_note_md
# =============================================================================


class TestParseNoteMd:
    def test_parses_frontmatter_and_body(self):
        from src.tools.knowledge.gardener import parse_note_md

        fm, body = parse_note_md(
            "---\nid: n1\ntype: decision\n---\n\n# Title\n\nbody text\n"
        )
        assert fm == {"id": "n1", "type": "decision"}
        assert "# Title" in body
        assert "body text" in body

    def test_no_frontmatter_returns_none(self):
        from src.tools.knowledge.gardener import parse_note_md

        fm, body = parse_note_md("# Just a heading\n\nno frontmatter here")
        assert fm is None
        assert "Just a heading" in body

    def test_invalid_yaml_raises_valueerror(self):
        from src.tools.knowledge.gardener import parse_note_md
        import pytest

        with pytest.raises(ValueError):
            parse_note_md("---\nid: n1\n  bad: : indent\n---\nbody")

    def test_non_mapping_frontmatter_raises(self):
        from src.tools.knowledge.gardener import parse_note_md
        import pytest

        with pytest.raises(ValueError):
            parse_note_md("---\njust a string\n---\nbody")

    def test_roundtrips_render_note_md(self):
        from src.tools.knowledge.gardener import parse_note_md
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {"id": "chose-jwt", "type": "decision", "content": "We chose JWT."}
        )
        fm, body = parse_note_md(md)
        assert fm["id"] == "chose-jwt"
        assert fm["type"] == "decision"


# =============================================================================
# lint_kb — the deterministic rule set
# =============================================================================


def _note(path, text):
    return {"path": path, "text": text}


def _rules(report):
    return {f.rule for f in report.findings}


class TestLintKb:
    def test_clean_note_has_no_errors(self):
        from src.tools.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "A note."\n---\n\n'
            "# N1\n\nBody linking to [n2](n2.md).\n"
        )
        text2 = (
            '---\nid: n2\ntype: learning\ndescription: "Another."\n---\n\n'
            "# N2\n\nLinks back to [n1](n1.md).\n"
        )
        report = lint_kb(
            [_note("knowledge/n1.md", text), _note("knowledge/n2.md", text2)]
        )
        assert report.errors == []

    def test_missing_frontmatter(self):
        from src.tools.knowledge.gardener import lint_kb

        report = lint_kb([_note("knowledge/x.md", "# No frontmatter\n\nbody")])
        assert "missing-frontmatter" in _rules(report)

    def test_invalid_yaml(self):
        from src.tools.knowledge.gardener import lint_kb

        report = lint_kb([_note("knowledge/x.md", "---\nid: n1\n : : :\n---\nbody")])
        assert "invalid-yaml" in _rules(report)

    def test_missing_required_keys(self):
        from src.tools.knowledge.gardener import lint_kb

        # has id, missing type + description
        report = lint_kb([_note("knowledge/x.md", "---\nid: n1\n---\n\n# X\n")])
        rules = _rules(report)
        assert "missing-required-key" in rules
        msgs = " ".join(f.message for f in report.findings)
        assert "type" in msgs and "description" in msgs

    def test_invalid_id_format(self):
        from src.tools.knowledge.gardener import lint_kb

        text = '---\nid: Not A Slug\ntype: decision\ndescription: "d"\n---\n\n# X\n'
        report = lint_kb([_note("knowledge/x.md", text)])
        assert "invalid-id" in _rules(report)

    def test_duplicate_id(self):
        from src.tools.knowledge.gardener import lint_kb

        text = '---\nid: dup\ntype: decision\ndescription: "d"\n---\n\n# X\n'
        report = lint_kb([_note("knowledge/a.md", text), _note("knowledge/b.md", text)])
        assert "duplicate-id" in _rules(report)

    def test_dead_link(self):
        from src.tools.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# N1\n\nSee [ghost](ghost.md).\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "dead-link" in _rules(report)

    def test_external_links_are_not_dead(self):
        from src.tools.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# N1\n\nSee [docs](https://example.com/page).\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "dead-link" not in _rules(report)

    def test_broken_supersede(self):
        from src.tools.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n'
            "superseded_by: nowhere\n---\n\n# N1\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "broken-supersede" in _rules(report)

    def test_orphan_warning(self):
        from src.tools.knowledge.gardener import lint_kb

        text = '---\nid: lonely\ntype: learning\ndescription: "d"\n---\n\n# Lonely\n\nno links\n'
        report = lint_kb([_note("knowledge/lonely.md", text)])
        assert "orphan" in _rules(report)
        assert all(
            f.severity == "warning" for f in report.findings if f.rule == "orphan"
        )

    def test_index_note_not_orphan(self):
        from src.tools.knowledge.gardener import lint_kb

        text = "no frontmatter index\n\n- [n1](n1.md)\n"
        report = lint_kb([_note("knowledge/index.md", text)])
        assert "orphan" not in _rules(report)

    def test_missing_title_warning(self):
        from src.tools.knowledge.gardener import lint_kb

        text = '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\nno heading here\n'
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "missing-title" in _rules(report)

    def test_duplicate_h1_warning(self):
        # Run-8 nit (docs §11.1): the old serializer emitted the title twice.
        # kb_lint flags the same defect in hand-authored / legacy notes.
        from src.tools.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# My Note\n\n# My Note\n\nBody [n1](n1.md).\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "duplicate-h1" in _rules(report)

    def test_single_h1_no_duplicate_warning(self):
        from src.tools.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# My Note\n\n## A subsection\n\nBody [n1](n1.md).\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "duplicate-h1" not in _rules(report)

    def test_report_separates_errors_and_warnings(self):
        from src.tools.knowledge.gardener import lint_kb

        report = lint_kb([_note("knowledge/x.md", "# no frontmatter\n")])
        assert all(f.severity == "error" for f in report.errors)
        assert all(f.severity == "warning" for f in report.warnings)
        assert len(report.findings) == len(report.errors) + len(report.warnings)


# =============================================================================
# render_index_md — OKF-shaped index.md generation
# =============================================================================


class TestRenderIndexMd:
    def test_groups_by_type_with_link_bullets(self):
        from src.tools.knowledge.gardener import render_index_md

        out = render_index_md(
            [
                {
                    "id": "chose-jwt",
                    "title": "Chose JWT",
                    "type": "decision",
                    "description": "We chose JWT.",
                },
                {
                    "id": "a-lesson",
                    "title": "A Lesson",
                    "type": "learning",
                    "description": "Learned it.",
                },
            ]
        )
        assert "## decision" in out
        assert "## learning" in out
        assert "- [Chose JWT](chose-jwt.md) - We chose JWT." in out
        assert "- [A Lesson](a-lesson.md) - Learned it." in out

    def test_no_frontmatter_fence(self):
        # index.md is a reserved filename and carries NO frontmatter (OKF §6).
        from src.tools.knowledge.gardener import render_index_md

        out = render_index_md([{"id": "n1", "type": "decision", "description": "d"}])
        assert not out.startswith("---")

    def test_title_falls_back_to_id(self):
        from src.tools.knowledge.gardener import render_index_md

        out = render_index_md([{"id": "my-slug", "type": "note", "description": "d"}])
        assert "[my-slug](my-slug.md)" in out

    def test_bullet_without_description(self):
        from src.tools.knowledge.gardener import render_index_md

        out = render_index_md([{"id": "n1", "title": "N1", "type": "decision"}])
        assert "- [N1](n1.md)" in out

    def test_preserves_human_sections_on_regen(self):
        from src.tools.knowledge.gardener import render_index_md, GEN_START, GEN_END

        existing = (
            "# My Custom Title\n\nHuman intro.\n\n"
            f"{GEN_START}\n## old\n\n- [x](x.md)\n{GEN_END}\n\nHuman footer.\n"
        )
        out = render_index_md(
            [{"id": "n1", "title": "N1", "type": "decision", "description": "d"}],
            existing=existing,
        )
        assert "# My Custom Title" in out
        assert "Human intro." in out
        assert "Human footer." in out
        assert "[N1](n1.md)" in out  # regenerated content present
        assert "- [x](x.md)" not in out  # old generated content replaced

    def test_empty_notes_does_not_crash(self):
        from src.tools.knowledge.gardener import render_index_md

        out = render_index_md([])
        assert "# Index" in out

    def test_budget_truncation_is_loud(self):
        from src.tools.knowledge.gardener import render_index_md

        notes = [
            {"id": f"n{i}", "title": f"N{i}", "type": "decision", "description": "d"}
            for i in range(500)
        ]
        out = render_index_md(notes)
        assert "truncated" in out.lower()


# =============================================================================
# small OKF helpers used by the tool wrappers
# =============================================================================


class TestNoteHelpers:
    def test_note_title_extracts_h1(self):
        from src.tools.knowledge.gardener import note_title

        assert note_title("\n# My Title\n\nbody") == "My Title"

    def test_note_title_none_when_no_heading(self):
        from src.tools.knowledge.gardener import note_title

        assert note_title("just prose, no heading") is None

    def test_is_reserved(self):
        from src.tools.knowledge.gardener import is_reserved

        assert is_reserved("knowledge/index.md") is True
        assert is_reserved("knowledge/log.md") is True
        assert is_reserved("knowledge/chose-jwt.md") is False
