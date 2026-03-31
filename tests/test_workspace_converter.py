"""Tests for src/tools/knowledge/workspace_converter.py.

Covers section 14 of persistent_agent_tests.md:
  14.1 _slugify()
  14.2 _classify_section()
  14.3 _extract_tags()
  14.4 _parse_sections()
  14.5 _infer_links()
  14.6 convert_workspace()
  14.7 convert_and_write()
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.tools.knowledge.workspace_converter import (
    _slugify,
    _classify_section,
    _extract_tags,
    _parse_sections,
    _infer_links,
    convert_workspace,
    convert_and_write,
)


# =============================================================================
# 14.1: _slugify()
# =============================================================================


class TestSlugify:
    """Tests for the _slugify() helper."""

    def test_lowercases(self):
        assert _slugify("Hello World") == "hello-world"

    def test_replaces_spaces_with_hyphens(self):
        assert _slugify("hello world test") == "hello-world-test"

    def test_strips_special_characters(self):
        assert _slugify("hello! @world#") == "hello-world"

    def test_strips_underscores(self):
        # Underscores match [^a-z0-9\s-] so they are stripped before space→hyphen
        result = _slugify("hello_world")
        assert result == "helloworld"

    def test_collapses_multiple_hyphens(self):
        assert _slugify("hello---world") == "hello-world"

    def test_strips_leading_trailing_hyphens(self):
        assert _slugify("--hello--") == "hello"

    def test_truncates_to_max_length(self):
        long_title = "a" * 100
        result = _slugify(long_title)
        assert len(result) == 80

    def test_custom_max_length(self):
        result = _slugify("hello world test", max_length=5)
        assert len(result) <= 5

    def test_empty_string(self):
        assert _slugify("") == ""

    def test_only_special_chars(self):
        assert _slugify("!@#$%") == ""


# =============================================================================
# 14.2: _classify_section()
# =============================================================================


class TestClassifySection:
    """Tests for _classify_section()."""

    def test_decision_keyword(self):
        assert _classify_section("This decision was important") == "decision"

    def test_decision_chose(self):
        assert _classify_section("We chose React") == "decision"

    def test_decision_picked(self):
        assert _classify_section("Team picked option A") == "decision"

    def test_decision_selected(self):
        assert _classify_section("We selected the approach") == "decision"

    def test_learning_learned(self):
        assert _classify_section("We learned about X") == "learning"

    def test_learning_discovered(self):
        assert _classify_section("Discovered a bug") == "learning"

    def test_learning_found(self):
        assert _classify_section("Found an issue") == "learning"

    def test_learning_error(self):
        assert _classify_section("Error in deployment") == "learning"

    def test_learning_fix(self):
        assert _classify_section("Applied a fix") == "learning"

    def test_goal_keyword(self):
        assert _classify_section("Our goal is to ship") == "goal"

    def test_goal_objective(self):
        assert _classify_section("Main objective achieved") == "goal"

    def test_goal_milestone(self):
        assert _classify_section("Reached milestone 3") == "goal"

    def test_plan_keyword(self):
        assert _classify_section("The plan for Q2") == "plan"

    def test_plan_roadmap(self):
        assert _classify_section("Updated roadmap") == "plan"

    def test_plan_phase(self):
        assert _classify_section("Phase 2 begins") == "plan"

    def test_question_keyword(self):
        assert _classify_section("Open question about API") == "question"

    def test_question_investigate(self):
        assert _classify_section("Need to investigate this") == "question"

    def test_question_todo(self):
        assert _classify_section("TODO: check the logs") == "question"

    def test_question_open(self):
        assert _classify_section("Still open for review") == "question"

    def test_state_status(self):
        assert _classify_section("Current status of deploy") == "state"

    def test_state_progress(self):
        assert _classify_section("Progress on feature X") == "state"

    def test_state_current(self):
        assert _classify_section("Current state of things") == "state"

    def test_code_keyword(self):
        assert _classify_section("The code for this feature") == "code"

    def test_code_implementation(self):
        assert _classify_section("Implementation details") == "code"

    def test_code_module(self):
        assert _classify_section("New module added") == "code"

    def test_code_class(self):
        assert _classify_section("Created class UserManager") == "code"

    def test_code_function(self):
        assert _classify_section("Refactored function parse") == "code"

    def test_default_type_no_match(self):
        assert _classify_section("Some random text here") == "learning"

    def test_case_insensitive(self):
        assert _classify_section("The DECISION was clear") == "decision"

    def test_first_match_wins(self):
        # "decision" rule is before "learning" rule
        assert _classify_section("decision learned discovered") == "decision"


# =============================================================================
# 14.3: _extract_tags()
# =============================================================================


class TestExtractTags:
    """Tests for _extract_tags()."""

    def test_extracts_inline_code_identifiers(self):
        tags = _extract_tags("Using `SomeClass` here")
        assert "someclass" in tags

    def test_inline_code_lowercased(self):
        tags = _extract_tags("The `MyFunction` works")
        assert "myfunction" in tags

    def test_inline_code_underscores_to_hyphens(self):
        tags = _extract_tags("Called `my_helper_func` here")
        assert "my-helper-func" in tags

    def test_extracts_capitalized_phrases(self):
        tags = _extract_tags("Using React Router for navigation")
        assert "react" in tags or "react-router" in tags

    def test_filters_stop_words(self):
        tags = _extract_tags("using The quick brown fox")
        for tag in tags:
            assert tag not in {"the", "a", "an", "is", "are"}

    def test_filters_short_tags(self):
        tags = _extract_tags("`ab` and `cd` and `xyz`")
        # ab and cd are <= 2 chars
        assert "ab" not in tags
        assert "cd" not in tags

    def test_no_duplicate_tags(self):
        tags = _extract_tags("`Foo` and `Foo` and Foo Bar")
        assert len([t for t in tags if t == "foo"]) <= 1

    def test_max_tags_default_10(self):
        # Generate content with many potential tags
        code_refs = " ".join(f"`Tag{i}Ref`" for i in range(20))
        tags = _extract_tags(code_refs)
        assert len(tags) <= 10

    def test_max_tags_custom(self):
        code_refs = " ".join(f"`Tag{i}Ref`" for i in range(20))
        tags = _extract_tags(code_refs, max_tags=3)
        assert len(tags) <= 3

    def test_extracts_marker_phrase_words(self):
        tags = _extract_tags("implemented asyncio for concurrency")
        assert "asyncio" in tags

    def test_marker_using(self):
        tags = _extract_tags("using postgresql for storage")
        assert "postgresql" in tags

    def test_marker_via(self):
        tags = _extract_tags("connected via websocket")
        assert "websocket" in tags

    def test_marker_chose(self):
        tags = _extract_tags("chose fastapi as framework")
        assert "fastapi" in tags

    def test_empty_content(self):
        tags = _extract_tags("")
        assert tags == []


# =============================================================================
# 14.4: _parse_sections()
# =============================================================================


class TestParseSections:
    """Tests for _parse_sections()."""

    def test_splits_on_h2(self):
        content = "## Section One\nBody one\n## Section Two\nBody two"
        sections = _parse_sections(content)
        assert len(sections) == 2
        assert sections[0]["title"] == "Section One"
        assert sections[1]["title"] == "Section Two"

    def test_splits_on_h3(self):
        content = "### Sub One\nBody one\n### Sub Two\nBody two"
        sections = _parse_sections(content)
        assert len(sections) == 2

    def test_does_not_split_on_h1(self):
        content = "# Top Level\nBody here"
        sections = _parse_sections(content)
        # No ## or ### — treated as single section
        assert len(sections) == 1
        assert sections[0]["title"] == "Workspace Notes"

    def test_does_not_split_on_h4(self):
        content = "#### Deep Heading\nBody here"
        sections = _parse_sections(content)
        assert len(sections) == 1
        assert sections[0]["title"] == "Workspace Notes"

    def test_no_headings_nonempty_content(self):
        content = "Just some plain text\nwith multiple lines"
        sections = _parse_sections(content)
        assert len(sections) == 1
        assert sections[0]["title"] == "Workspace Notes"
        assert sections[0]["level"] == "2"
        assert "plain text" in sections[0]["body"]

    def test_empty_content(self):
        assert _parse_sections("") == []

    def test_whitespace_only_content(self):
        assert _parse_sections("   \n\n  ") == []

    def test_empty_body_sections_filtered(self):
        content = "## Section One\n\n## Section Two\nHas body"
        sections = _parse_sections(content)
        # Section One has empty body, should be filtered
        assert len(sections) == 1
        assert sections[0]["title"] == "Section Two"

    def test_section_has_required_keys(self):
        content = "## My Section\nSome body text"
        sections = _parse_sections(content)
        assert len(sections) == 1
        sec = sections[0]
        assert "title" in sec
        assert "level" in sec
        assert "body" in sec

    def test_level_is_string(self):
        content = "## H2 Section\nBody\n### H3 Section\nBody"
        sections = _parse_sections(content)
        assert sections[0]["level"] == "2"
        assert sections[1]["level"] == "3"

    def test_mixed_h2_h3(self):
        content = "## Main\nBody main\n### Sub\nBody sub\n## Other\nBody other"
        sections = _parse_sections(content)
        assert len(sections) == 3


# =============================================================================
# 14.5: _infer_links()
# =============================================================================


class TestInferLinks:
    """Tests for _infer_links()."""

    def test_creates_references_link(self):
        sections = [
            {
                "slug": "auth-setup",
                "title": "Auth Setup",
                "body": "See database config below",
            },
            {
                "slug": "database-config",
                "title": "Database Config",
                "body": "PostgreSQL setup",
            },
        ]
        links = _infer_links(sections)
        assert "auth-setup" in links
        assert links["auth-setup"][0]["target"] == "database-config"
        assert links["auth-setup"][0]["type"] == "REFERENCES"

    def test_does_not_self_link(self):
        sections = [
            {
                "slug": "auth-setup",
                "title": "Auth Setup",
                "body": "Auth Setup is important",
            },
        ]
        links = _infer_links(sections)
        assert "auth-setup" not in links

    def test_short_titles_excluded(self):
        sections = [
            {"slug": "api", "title": "API", "body": "The API is used everywhere"},
            {"slug": "main-app", "title": "Main App", "body": "Uses the API"},
        ]
        links = _infer_links(sections)
        # "API" is only 3 chars (< 4), should not match
        assert "main-app" not in links

    def test_title_4_chars_included(self):
        sections = [
            {"slug": "nats", "title": "NATS", "body": "Messaging layer"},
            {"slug": "main-app", "title": "Main App", "body": "Connected via nats"},
        ]
        links = _infer_links(sections)
        assert "main-app" in links

    def test_case_insensitive_matching(self):
        sections = [
            {"slug": "database", "title": "Database", "body": "PostgreSQL setup"},
            {
                "slug": "overview",
                "title": "Overview",
                "body": "The DATABASE handles state",
            },
        ]
        links = _infer_links(sections)
        assert "overview" in links

    def test_no_links_when_no_references(self):
        sections = [
            {
                "slug": "alpha",
                "title": "Alpha Section",
                "body": "Completely unrelated text",
            },
            {"slug": "beta", "title": "Beta Section", "body": "Also totally different"},
        ]
        links = _infer_links(sections)
        assert links == {}

    def test_bidirectional_references(self):
        sections = [
            {"slug": "auth", "title": "Auth System", "body": "Depends on user manager"},
            {
                "slug": "user-manager",
                "title": "User Manager",
                "body": "Used by auth system",
            },
        ]
        links = _infer_links(sections)
        assert "auth" in links
        assert "user-manager" in links


# =============================================================================
# 14.6: convert_workspace()
# =============================================================================


class TestConvertWorkspace:
    """Tests for convert_workspace()."""

    def test_empty_content_returns_empty(self):
        assert convert_workspace("") == []

    def test_whitespace_only_returns_empty(self):
        assert convert_workspace("   \n\n  ") == []

    def test_generates_slug_for_sections(self):
        content = "## My Section\nSome body text"
        notes = convert_workspace(content)
        assert notes[0]["slug"] == "my-section"

    def test_deduplicates_slugs_with_suffix(self):
        content = "## Test\nBody one\n## Test\nBody two"
        notes = convert_workspace(content)
        slugs = [n["slug"] for n in notes]
        assert len(set(slugs)) == 2
        assert "test" in slugs
        assert "test-2" in slugs

    def test_untitled_slug_for_empty_title(self):
        # Force an empty title through _parse_sections by crafting content
        # where heading text is all special chars
        content = "## !@#\nBody text here"
        notes = convert_workspace(content)
        assert notes[0]["slug"] == "untitled"

    def test_classifies_sections(self):
        content = "## Decision\nWe chose React over Vue"
        notes = convert_workspace(content)
        assert notes[0]["note_type"] == "decision"

    def test_extracts_tags_from_body(self):
        content = "## Setup\nUsing `PostgreSQL` for storage"
        notes = convert_workspace(content)
        assert "postgresql" in notes[0]["tags"]

    def test_appends_agent_config_tag(self):
        content = "## Notes\nSome body here"
        notes = convert_workspace(content, config_name="scholar")
        assert "agent-scholar" in notes[0]["tags"]

    def test_appends_migrated_tag_with_job_id(self):
        content = "## Notes\nSome body here"
        notes = convert_workspace(content, job_id="abc-123")
        assert "migrated-from-workspace" in notes[0]["tags"]

    def test_no_config_tag_without_config_name(self):
        content = "## Notes\nSome body here"
        notes = convert_workspace(content)
        for tag in notes[0]["tags"]:
            assert not tag.startswith("agent-")

    def test_no_migrated_tag_without_job_id(self):
        content = "## Notes\nSome body here"
        notes = convert_workspace(content)
        assert "migrated-from-workspace" not in notes[0]["tags"]

    def test_infers_cross_reference_links(self):
        content = "## Auth System\nUses the database layer\n## Database Layer\nPostgreSQL setup"
        notes = convert_workspace(content)
        # Auth System references Database Layer
        auth_note = [n for n in notes if n["slug"] == "auth-system"][0]
        assert len(auth_note["links"]) > 0
        assert auth_note["links"][0]["target"] == "database-layer"

    def test_output_has_required_keys(self):
        content = "## Test\nBody text"
        notes = convert_workspace(content)
        note = notes[0]
        assert "title" in note
        assert "note_type" in note
        assert "content" in note
        assert "tags" in note
        assert "slug" in note
        assert "links" in note

    def test_output_does_not_have_body_key(self):
        # "body" is internal-only, should be stripped from final output
        content = "## Test\nBody text"
        notes = convert_workspace(content)
        assert "body" not in notes[0]

    def test_multiple_sections(self):
        content = "## One\nBody one\n## Two\nBody two\n## Three\nBody three"
        notes = convert_workspace(content)
        assert len(notes) == 3


# =============================================================================
# 14.7: convert_and_write()
# =============================================================================


class TestConvertAndWrite:
    """Tests for convert_and_write()."""

    @pytest.fixture
    def mock_kg(self):
        kg = MagicMock()
        kg.create_note.return_value = "test-slug"
        return kg

    @pytest.fixture
    def mock_ks(self):
        ks = AsyncMock()
        return ks

    @pytest.mark.asyncio
    async def test_returns_zero_for_empty_content(self, mock_kg, mock_ks):
        result = await convert_and_write(
            content="",
            project_id="proj-1",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_zero_for_no_sections(self, mock_kg, mock_ks):
        result = await convert_and_write(
            content="   \n\n  ",
            project_id="proj-1",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_writes_to_neo4j_first(self, mock_kg, mock_ks):
        content = "## Test Note\nBody content here"
        await convert_and_write(
            content=content,
            project_id="00000000-0000-0000-0000-000000000001",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        mock_kg.create_note.assert_called_once()
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["title"] == "Test Note"
        assert call_kwargs["content"] == "Body content here"

    @pytest.mark.asyncio
    async def test_writes_through_to_pgvector(self, mock_kg, mock_ks):
        content = "## Test Note\nBody content here"
        await convert_and_write(
            content=content,
            project_id="00000000-0000-0000-0000-000000000001",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        mock_ks.upsert_note.assert_called_once()

    @pytest.mark.asyncio
    async def test_pgvector_failure_is_nonfatal(self, mock_kg, mock_ks):
        mock_ks.upsert_note.side_effect = Exception("pgvector down")
        content = "## Test Note\nBody content here"
        result = await convert_and_write(
            content=content,
            project_id="00000000-0000-0000-0000-000000000001",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        # Neo4j note still counts as written
        assert result == 1

    @pytest.mark.asyncio
    async def test_neo4j_failure_is_nonfatal(self, mock_kg, mock_ks):
        mock_kg.create_note.side_effect = Exception("Neo4j down")
        content = "## Test Note\nBody content here"
        result = await convert_and_write(
            content=content,
            project_id="00000000-0000-0000-0000-000000000001",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_count_of_written_notes(self, mock_kg, mock_ks):
        content = "## One\nBody one\n## Two\nBody two\n## Three\nBody three"
        result = await convert_and_write(
            content=content,
            project_id="00000000-0000-0000-0000-000000000001",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        assert result == 3

    @pytest.mark.asyncio
    async def test_neo4j_partial_failure(self, mock_kg, mock_ks):
        # Second note fails in Neo4j
        mock_kg.create_note.side_effect = [
            "slug-1",
            Exception("Neo4j error"),
            "slug-3",
        ]
        content = "## One\nBody one\n## Two\nBody two\n## Three\nBody three"
        result = await convert_and_write(
            content=content,
            project_id="00000000-0000-0000-0000-000000000001",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        assert result == 2

    @pytest.mark.asyncio
    async def test_passes_job_id_to_create_note(self, mock_kg, mock_ks):
        content = "## Test\nBody text"
        await convert_and_write(
            content=content,
            project_id="00000000-0000-0000-0000-000000000001",
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            job_id="job-abc-123",
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["job_id"] == "job-abc-123"

    @pytest.mark.asyncio
    async def test_passes_project_id_to_create_note(self, mock_kg, mock_ks):
        content = "## Test\nBody text"
        pid = "00000000-0000-0000-0000-000000000001"
        await convert_and_write(
            content=content,
            project_id=pid,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["project_id"] == pid
