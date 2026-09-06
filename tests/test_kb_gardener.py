"""Tests for src/tools/knowledge/gardener.py — the OKF lint/index engine (slice 2).

Pure functions (no workspace, no DB): parse_note_md, lint_kb, render_index_md.
"""


# =============================================================================
# parse_note_md — inverse of _render_note_md
# =============================================================================


class TestParseNoteMd:
    def test_parses_frontmatter_and_body(self):
        from shared.runtime.knowledge.gardener import parse_note_md

        fm, body = parse_note_md(
            "---\nid: n1\ntype: decision\n---\n\n# Title\n\nbody text\n"
        )
        assert fm == {"id": "n1", "type": "decision"}
        assert "# Title" in body
        assert "body text" in body

    def test_no_frontmatter_returns_none(self):
        from shared.runtime.knowledge.gardener import parse_note_md

        fm, body = parse_note_md("# Just a heading\n\nno frontmatter here")
        assert fm is None
        assert "Just a heading" in body

    def test_invalid_yaml_raises_valueerror(self):
        from shared.runtime.knowledge.gardener import parse_note_md
        import pytest

        with pytest.raises(ValueError):
            parse_note_md("---\nid: n1\n  bad: : indent\n---\nbody")

    def test_non_mapping_frontmatter_raises(self):
        from shared.runtime.knowledge.gardener import parse_note_md
        import pytest

        with pytest.raises(ValueError):
            parse_note_md("---\njust a string\n---\nbody")

    def test_roundtrips_render_note_md(self):
        from shared.runtime.knowledge.gardener import parse_note_md
        from agent.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {"id": "chose-jwt", "type": "decision", "content": "We chose JWT."}
        )
        fm, body = parse_note_md(md)
        assert fm["id"] == "chose-jwt"
        assert fm["type"] == "decision"

    def test_roundtrips_tags_keywords_with_yaml_flow_breakers(self):
        # C-1: agent-authored keyword strings carry YAML flow-sequence breakers
        # (@-scalars, embedded commas/colons, nested brackets, `#` comments).
        # The renderer must quote each element so the frontmatter still parses;
        # an unquoted `[@dataclass(frozen=True, slots=True), a: b]` is invalid
        # YAML → the reindexer skips the note and re-warns every sweep.
        from shared.runtime.knowledge.gardener import parse_note_md
        from agent.tools.knowledge.knowledge_tools import _render_note_md

        tags = ["config.py:", "a: b"]
        keywords = [
            "@dataclass(frozen=True, slots=True)",
            "pick-first proposal #1",
            "[nested]",
        ]
        md = _render_note_md(
            {
                "id": "n1",
                "type": "learning",
                "content": "body",
                "tags": tags,
                "keywords": keywords,
            }
        )
        fm, _ = parse_note_md(md)  # must not raise
        assert fm["tags"] == tags
        assert fm["keywords"] == keywords


# =============================================================================
# lint_kb — the deterministic rule set
# =============================================================================


def _note(path, text):
    return {"path": path, "text": text}


def _rules(report):
    return {f.rule for f in report.findings}


class TestLintKb:
    def test_clean_note_has_no_errors(self):
        from shared.runtime.knowledge.gardener import lint_kb

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
        from shared.runtime.knowledge.gardener import lint_kb

        report = lint_kb([_note("knowledge/x.md", "# No frontmatter\n\nbody")])
        assert "missing-frontmatter" in _rules(report)

    def test_invalid_yaml(self):
        from shared.runtime.knowledge.gardener import lint_kb

        report = lint_kb([_note("knowledge/x.md", "---\nid: n1\n : : :\n---\nbody")])
        assert "invalid-yaml" in _rules(report)

    def test_missing_required_keys(self):
        from shared.runtime.knowledge.gardener import lint_kb

        # has id, missing type + description
        report = lint_kb([_note("knowledge/x.md", "---\nid: n1\n---\n\n# X\n")])
        rules = _rules(report)
        assert "missing-required-key" in rules
        msgs = " ".join(f.message for f in report.findings)
        assert "type" in msgs and "description" in msgs

    def test_invalid_id_format(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = '---\nid: Not A Slug\ntype: decision\ndescription: "d"\n---\n\n# X\n'
        report = lint_kb([_note("knowledge/x.md", text)])
        assert "invalid-id" in _rules(report)

    def test_duplicate_id(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = '---\nid: dup\ntype: decision\ndescription: "d"\n---\n\n# X\n'
        report = lint_kb([_note("knowledge/a.md", text), _note("knowledge/b.md", text)])
        assert "duplicate-id" in _rules(report)

    def test_dead_link(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# N1\n\nSee [ghost](ghost.md).\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "dead-link" in _rules(report)

    def test_external_links_are_not_dead(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# N1\n\nSee [docs](https://example.com/page).\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "dead-link" not in _rules(report)

    def test_broken_supersede(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n'
            "superseded_by: nowhere\n---\n\n# N1\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "broken-supersede" in _rules(report)

    def test_orphan_warning(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = '---\nid: lonely\ntype: learning\ndescription: "d"\n---\n\n# Lonely\n\nno links\n'
        report = lint_kb([_note("knowledge/lonely.md", text)])
        assert "orphan" in _rules(report)
        assert all(
            f.severity == "warning" for f in report.findings if f.rule == "orphan"
        )

    def test_index_note_not_orphan(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = "no frontmatter index\n\n- [n1](n1.md)\n"
        report = lint_kb([_note("knowledge/index.md", text)])
        assert "orphan" not in _rules(report)

    def test_missing_title_warning(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\nno heading here\n'
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "missing-title" in _rules(report)

    def test_duplicate_h1_warning(self):
        # Run-8 nit (docs §11.1): the old serializer emitted the title twice.
        # kb_lint flags the same defect in hand-authored / legacy notes.
        from shared.runtime.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# My Note\n\n# My Note\n\nBody [n1](n1.md).\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "duplicate-h1" in _rules(report)

    def test_single_h1_no_duplicate_warning(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# My Note\n\n## A subsection\n\nBody [n1](n1.md).\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "duplicate-h1" not in _rules(report)

    def test_oversized_note_warning(self):
        # §11.1 item 2: 131 KB curator dumps — loud-flag bodies above ~15 KB.
        from shared.runtime.knowledge.gardener import lint_kb

        big_body = "# Big\n\n" + ("x" * (16 * 1024)) + "\n\nSee [n2](n2.md).\n"
        text = f'---\nid: n1\ntype: learning\ndescription: "d"\n---\n\n{big_body}'
        text2 = '---\nid: n2\ntype: decision\ndescription: "d"\n---\n\n# N2\n\n[n1](n1.md)\n'
        report = lint_kb(
            [_note("knowledge/n1.md", text), _note("knowledge/n2.md", text2)]
        )
        rules = _rules(report)
        assert "oversized-note" in rules
        assert all(
            f.severity == "warning"
            for f in report.findings
            if f.rule == "oversized-note"
        )
        # The small sibling is not flagged.
        assert not any(
            f.rule == "oversized-note" and f.path == "knowledge/n2.md"
            for f in report.findings
        )

    def test_normal_size_note_not_flagged(self):
        from shared.runtime.knowledge.gardener import lint_kb

        text = (
            '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
            "# N1\n\nA perfectly reasonable note. [n1](n1.md)\n"
        )
        report = lint_kb([_note("knowledge/n1.md", text)])
        assert "oversized-note" not in _rules(report)

    def test_oversized_reserved_index_exempt(self):
        # index.md has its own render_index_md budget; reserved files stay
        # exempt from note-level rules.
        from shared.runtime.knowledge.gardener import lint_kb

        text = "# Index\n\n" + ("- [x](x.md)\n" * 4000)
        report = lint_kb([_note("knowledge/index.md", text)])
        assert "oversized-note" not in _rules(report)

    def test_slug_forked_twins_warning(self):
        # The bare-agent collision signature: kb_write minted foo-<hash6>.md
        # next to foo.md (both active). Detection net for ungated writers.
        from shared.runtime.knowledge.gardener import lint_kb

        base = (
            '---\nid: my-note\ntype: learning\ndescription: "d"\n---\n\n'
            "# My Note\n\nbody one [my-note-ef33b5](my-note-ef33b5.md)\n"
        )
        fork = (
            '---\nid: my-note-ef33b5\ntype: learning\ndescription: "d"\n---\n\n'
            "# My Note\n\nbody two [my-note](my-note.md)\n"
        )
        report = lint_kb(
            [
                _note("knowledge/my-note.md", base),
                _note("knowledge/my-note-ef33b5.md", fork),
            ]
        )
        rules = _rules(report)
        assert "slug-forked" in rules
        assert all(
            f.severity == "warning" for f in report.findings if f.rule == "slug-forked"
        )

    def test_slug_fork_superseded_base_not_flagged(self):
        # A superseded base + active suffixed survivor is the gate's SUPERSEDE
        # outcome working as designed — not a twin to sweep.
        from shared.runtime.knowledge.gardener import lint_kb

        base = (
            '---\nid: my-note\ntype: learning\ndescription: "d"\n'
            "status: superseded\nsuperseded_by: my-note-ef33b5\n---\n\n"
            "# My Note\n\nold [my-note-ef33b5](my-note-ef33b5.md)\n"
        )
        fork = (
            '---\nid: my-note-ef33b5\ntype: learning\ndescription: "d"\n---\n\n'
            "# My Note\n\nnew [my-note](my-note.md)\n"
        )
        report = lint_kb(
            [
                _note("knowledge/my-note.md", base),
                _note("knowledge/my-note-ef33b5.md", fork),
            ]
        )
        assert "slug-forked" not in _rules(report)

    def test_hexlike_slug_without_base_not_flagged(self):
        # A note whose slug merely *ends* in 6 hex chars but has no base
        # sibling is not a fork.
        from shared.runtime.knowledge.gardener import lint_kb

        text = (
            '---\nid: release-1a2b3c\ntype: decision\ndescription: "d"\n---\n\n'
            "# R\n\n[release-1a2b3c](release-1a2b3c.md)\n"
        )
        report = lint_kb([_note("knowledge/release-1a2b3c.md", text)])
        assert "slug-forked" not in _rules(report)

    def test_report_separates_errors_and_warnings(self):
        from shared.runtime.knowledge.gardener import lint_kb

        report = lint_kb([_note("knowledge/x.md", "# no frontmatter\n")])
        assert all(f.severity == "error" for f in report.errors)
        assert all(f.severity == "warning" for f in report.warnings)
        assert len(report.findings) == len(report.errors) + len(report.warnings)


# =============================================================================
# render_index_md — OKF-shaped index.md generation
# =============================================================================


class TestRenderIndexMd:
    def test_groups_by_type_with_link_bullets(self):
        from shared.runtime.knowledge.gardener import render_index_md

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
        from shared.runtime.knowledge.gardener import render_index_md

        out = render_index_md([{"id": "n1", "type": "decision", "description": "d"}])
        assert not out.startswith("---")

    def test_title_falls_back_to_id(self):
        from shared.runtime.knowledge.gardener import render_index_md

        out = render_index_md([{"id": "my-slug", "type": "note", "description": "d"}])
        assert "[my-slug](my-slug.md)" in out

    def test_bullet_without_description(self):
        from shared.runtime.knowledge.gardener import render_index_md

        out = render_index_md([{"id": "n1", "title": "N1", "type": "decision"}])
        assert "- [N1](n1.md)" in out

    def test_preserves_human_sections_on_regen(self):
        from shared.runtime.knowledge.gardener import (
            render_index_md,
            GEN_START,
            GEN_END,
        )

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
        from shared.runtime.knowledge.gardener import render_index_md

        out = render_index_md([])
        assert "# Index" in out

    def test_budget_truncation_is_loud(self):
        from shared.runtime.knowledge.gardener import render_index_md

        notes = [
            {"id": f"n{i}", "title": f"N{i}", "type": "decision", "description": "d"}
            for i in range(500)
        ]
        out = render_index_md(notes)
        assert "truncated" in out.lower()


# =============================================================================
# near_duplicate_findings — pure formatter for the embedding-backed rule
# =============================================================================


class TestNearDuplicateFindings:
    def test_emits_warning_for_pair_in_scope(self):
        from shared.runtime.knowledge.gardener import near_duplicate_findings

        findings = near_duplicate_findings(
            [("note-a", "note-b", 0.93)],
            ["knowledge/note-a.md", "knowledge/note-b.md"],
        )
        assert len(findings) == 1
        f = findings[0]
        assert f.rule == "near-duplicate"
        assert f.severity == "warning"
        assert f.path == "knowledge/note-a.md"
        assert "note-b" in f.message
        assert "93" in f.message

    def test_pair_outside_vault_skipped(self):
        # The index may hold notes that aren't in the vault being linted
        # (other roots, not-yet-dual-written legacy rows) — never flag those.
        from shared.runtime.knowledge.gardener import near_duplicate_findings

        findings = near_duplicate_findings(
            [("note-a", "ghost", 0.95)],
            ["knowledge/note-a.md"],
        )
        assert findings == []

    def test_empty_pairs_no_findings(self):
        from shared.runtime.knowledge.gardener import near_duplicate_findings

        assert near_duplicate_findings([], []) == []


# =============================================================================
# external URL sweep — pure extraction + formatting (network stays in the tool)
# =============================================================================


class TestExternalUrlMap:
    def test_collects_urls_with_referencing_paths(self):
        from shared.runtime.knowledge.gardener import external_url_map

        n1 = (
            '---\nid: n1\ntype: source\ndescription: "d"\n---\n\n'
            "# N1\n\nSee [docs](https://example.com/a) and [n2](n2.md).\n"
        )
        n2 = (
            '---\nid: n2\ntype: source\ndescription: "d"\n---\n\n'
            "# N2\n\nAlso [docs](https://example.com/a) plus "
            "[other](http://other.io/x).\n"
        )
        url_map = external_url_map(
            [_note("knowledge/n1.md", n1), _note("knowledge/n2.md", n2)]
        )
        assert url_map["https://example.com/a"] == [
            "knowledge/n1.md",
            "knowledge/n2.md",
        ]
        assert url_map["http://other.io/x"] == ["knowledge/n2.md"]

    def test_ignores_internal_mailto_and_anchor_links(self):
        from shared.runtime.knowledge.gardener import external_url_map

        text = (
            '---\nid: n1\ntype: note\ndescription: "d"\n---\n\n'
            "# N1\n\n[a](a.md) [m](mailto:x@y.z) [s](#section)\n"
        )
        assert external_url_map([_note("knowledge/n1.md", text)]) == {}


class TestDeadUrlFindings:
    def test_emits_warning_per_referencing_path(self):
        from shared.runtime.knowledge.gardener import dead_url_findings

        findings = dead_url_findings(
            {"https://gone.example/x": "HTTP 404"},
            {"https://gone.example/x": ["knowledge/n1.md", "knowledge/n2.md"]},
        )
        assert len(findings) == 2
        assert {f.path for f in findings} == {"knowledge/n1.md", "knowledge/n2.md"}
        assert all(f.rule == "dead-external-url" for f in findings)
        assert all(f.severity == "warning" for f in findings)
        assert all("https://gone.example/x" in f.message for f in findings)
        assert all("404" in f.message for f in findings)

    def test_alive_urls_produce_nothing(self):
        from shared.runtime.knowledge.gardener import dead_url_findings

        assert dead_url_findings({}, {"https://ok.example": ["knowledge/n1.md"]}) == []


# =============================================================================
# small OKF helpers used by the tool wrappers
# =============================================================================


class TestNoteHelpers:
    def test_note_title_extracts_h1(self):
        from shared.runtime.knowledge.gardener import note_title

        assert note_title("\n# My Title\n\nbody") == "My Title"

    def test_note_title_none_when_no_heading(self):
        from shared.runtime.knowledge.gardener import note_title

        assert note_title("just prose, no heading") is None

    def test_is_reserved(self):
        from shared.runtime.knowledge.gardener import is_reserved

        assert is_reserved("knowledge/index.md") is True
        assert is_reserved("knowledge/log.md") is True
        assert is_reserved("knowledge/chose-jwt.md") is False


# =============================================================================
# Wikilink extraction — Obsidian [[target]] syntax alongside markdown links.
#
# The vault this indexes is an Obsidian vault: links are overwhelmingly
# [[wikilinks]] in the body and in `related:` frontmatter, not [text](x.md).
# Measured over the 577 live-slice docs: 1721 body wikilinks + 947 frontmatter
# wikilinks vs 512 markdown links — the parser saw 16% of the real graph.
# =============================================================================


class TestWikilinkTargets:
    """_internal_link_targets understands [[wikilinks]], not just [text](x.md)."""

    def test_extracts_plain_wikilink(self):
        from shared.runtime.knowledge.gardener import _internal_link_targets

        assert _internal_link_targets("see [[other_note]] for detail") == ["other_note"]

    def test_strips_alias_after_pipe(self):
        from shared.runtime.knowledge.gardener import _internal_link_targets

        assert _internal_link_targets("see [[other_note|nicer label]]") == [
            "other_note"
        ]

    def test_strips_anchor(self):
        from shared.runtime.knowledge.gardener import _internal_link_targets

        assert _internal_link_targets("see [[other_note#a section]]") == ["other_note"]

    def test_reduces_path_to_basename(self):
        from shared.runtime.knowledge.gardener import _internal_link_targets

        assert _internal_link_targets("see [[features/other_note]]") == ["other_note"]

    def test_ignores_embed_transclusion(self):
        from shared.runtime.knowledge.gardener import _internal_link_targets

        assert _internal_link_targets("![[some_image.png]]") == []

    def test_still_extracts_markdown_links(self):
        from shared.runtime.knowledge.gardener import _internal_link_targets

        assert _internal_link_targets("see [label](other_note.md)") == ["other_note"]

    def test_collects_both_syntaxes_in_one_body(self):
        from shared.runtime.knowledge.gardener import _internal_link_targets

        got = _internal_link_targets("[a](one.md) and [[two]] and [[three|x]]")
        assert got == ["one", "two", "three"]


class TestFrontmatterLinkTargets:
    """`related:` frontmatter is a relationship declaration — it is part of the
    link graph, and it is where 947 of this vault's edges live."""

    def test_extracts_related_wikilinks(self):
        from shared.runtime.knowledge.gardener import frontmatter_link_targets

        assert frontmatter_link_targets({"related": ["[[alpha]]", "[[bravo]]"]}) == [
            "alpha",
            "bravo",
        ]

    def test_accepts_bare_slugs(self):
        from shared.runtime.knowledge.gardener import frontmatter_link_targets

        assert frontmatter_link_targets({"related": ["alpha"]}) == ["alpha"]

    def test_strips_alias_and_anchor(self):
        from shared.runtime.knowledge.gardener import frontmatter_link_targets

        assert frontmatter_link_targets(
            {"related": ["[[alpha|A]]", "[[bravo#s]]"]}
        ) == ["alpha", "bravo"]

    def test_missing_related_is_empty(self):
        from shared.runtime.knowledge.gardener import frontmatter_link_targets

        assert frontmatter_link_targets({"tags": ["x"]}) == []

    def test_none_frontmatter_is_empty(self):
        from shared.runtime.knowledge.gardener import frontmatter_link_targets

        assert frontmatter_link_targets(None) == []

    def test_scalar_related_is_accepted(self):
        from shared.runtime.knowledge.gardener import frontmatter_link_targets

        assert frontmatter_link_targets({"related": "[[alpha]]"}) == ["alpha"]
