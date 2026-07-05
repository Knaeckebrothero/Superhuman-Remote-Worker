"""Tests for OKF KB slice 3 PR2 — heading-aware chunker + embed pipeline.

Design: docs/features/okf_knowledge_base.md §5.1 / §11 slice-3 PR2.

PR2 is pure (no DB): a heading-aware structural chunker (~400-512-token target,
sibling merge-up, 10-15% overlap only on forced mid-section splits), a breadcrumb
embed-text builder (title/type/tags/heading-path as natural text, never raw YAML),
`embedding_version` stamping, and the async embed pipeline that wires embed_batch.

These tests inject a deterministic word-counter (``_wc``) so chunk sizing is exact
and independent of tiktoken's presence.
"""

import pytest

from unittest.mock import AsyncMock, MagicMock

from src.tools.knowledge.chunker import (
    CHUNKER_VERSION,
    NoteChunk,
    build_embed_text,
    chunk_note,
    embed_note_chunks,
    embedding_version,
)


def _wc(text: str) -> int:
    """Deterministic token proxy: whitespace word count."""
    return len(text.split())


def _make_embedding_service(model="qwen3-embedding-8b", dims=4096):
    """Mock EmbeddingService whose embed_batch echoes one vector per text."""
    svc = MagicMock()
    svc.model = model
    svc.expected_dimensions = dims

    async def _batch(texts):
        return [[float(i)] for i in range(len(texts))]

    svc.embed_batch = AsyncMock(side_effect=_batch)
    return svc


# =============================================================================
# chunk_note — structural, heading-aware
# =============================================================================


class TestChunkNoteShortNote:
    def test_short_note_is_a_single_chunk(self):
        body = "# Chose JWT\n\nWe evaluated both and picked JWT."
        chunks = chunk_note(body, target_tokens=100, token_counter=_wc)
        assert len(chunks) == 1
        assert isinstance(chunks[0], NoteChunk)
        assert chunks[0].chunk_ix == 0
        assert chunks[0].heading_path == "Chose JWT"
        # whole body preserved
        assert "We evaluated both" in chunks[0].content

    def test_no_heading_preamble_has_none_path(self):
        body = "just some text with no heading at all"
        chunks = chunk_note(body, target_tokens=100, token_counter=_wc)
        assert len(chunks) == 1
        assert chunks[0].heading_path is None

    def test_empty_body_yields_no_chunks(self):
        assert chunk_note("", target_tokens=100, token_counter=_wc) == []
        assert chunk_note("   \n\n  ", target_tokens=100, token_counter=_wc) == []


class TestChunkNoteHeadingPaths:
    def _nested(self):
        return "# A\naaa\n## B\nbbb\n### C\nccc"

    def test_nested_headings_build_breadcrumbs_when_forced_apart(self):
        # target 3 words → each section (heading line + one word == 3 tokens)
        # is its own chunk, so the breadcrumb path is visible per section.
        chunks = chunk_note(self._nested(), target_tokens=3, token_counter=_wc)
        paths = [c.heading_path for c in chunks]
        assert paths == ["A", "A > B", "A > B > C"]

    def test_sibling_pop_resets_deeper_levels(self):
        body = "# A\naaa\n## B\nbbb\n## C\nccc"
        chunks = chunk_note(body, target_tokens=3, token_counter=_wc)
        paths = [c.heading_path for c in chunks]
        assert paths == ["A", "A > B", "A > C"]

    def test_chunk_ix_is_zero_based_sequential(self):
        chunks = chunk_note(self._nested(), target_tokens=3, token_counter=_wc)
        assert [c.chunk_ix for c in chunks] == [0, 1, 2]


class TestChunkNoteMerge:
    def test_small_adjacent_sections_merge_up(self):
        # large target → the whole note collapses into one chunk (the common
        # case for short notes), breadcrumb taken from the first section.
        body = "# A\naaa\n## B\nbbb\n### C\nccc"
        chunks = chunk_note(body, target_tokens=100, token_counter=_wc)
        assert len(chunks) == 1
        assert chunks[0].heading_path == "A"
        for token in ("aaa", "bbb", "ccc"):
            assert token in chunks[0].content


class TestChunkNoteOversizedSplit:
    def test_oversized_section_splits_into_multiple_chunks(self):
        body = "\n\n".join(f"p{i} x x" for i in range(30))  # 30 paras, 3 words each
        chunks = chunk_note(body, target_tokens=30, token_counter=_wc)
        assert len(chunks) > 1
        assert [c.chunk_ix for c in chunks] == list(range(len(chunks)))

    def test_forced_split_pieces_overlap(self):
        # overlap_tokens = int(30 * 0.12) = 3 == one 3-word paragraph, so the
        # last paragraph of a piece seeds the next one.
        body = "\n\n".join(f"p{i} x x" for i in range(30))
        chunks = chunk_note(body, target_tokens=30, token_counter=_wc)
        assert len(chunks) >= 2
        first_tail = chunks[0].content.split("\n\n")[-1]
        second_head = chunks[1].content.split("\n\n")[0]
        assert first_tail == second_head

    def test_merged_sections_do_not_overlap(self):
        # clean structural boundaries: adjacent same-size sections that each fit
        # never duplicate content across the boundary.
        body = "## A\na a a\n## B\nb b b"
        # each section is 5 tokens ("##", "A", "a", "a", "a"); target 5 fits one
        # section per chunk, so they split on the clean heading boundary.
        chunks = chunk_note(body, target_tokens=5, token_counter=_wc)
        assert len(chunks) == 2
        assert "b" not in chunks[0].content
        assert chunks[0].content.count("a") >= 3


# =============================================================================
# build_embed_text — breadcrumb prefix (natural text, never raw YAML)
# =============================================================================


class TestBuildEmbedText:
    def test_prepends_title_type_tags_and_heading_path(self):
        out = build_embed_text(
            content="JWT is stateless.",
            heading_path="Chose JWT > Reasoning",
            title="Chose JWT over OAuth",
            note_type="decision",
            tags=["authentication", "security"],
        )
        assert "Chose JWT over OAuth" in out
        assert "decision" in out
        assert "authentication, security" in out  # comma-joined
        assert "Chose JWT > Reasoning" in out
        assert "JWT is stateless." in out
        # the content comes after the breadcrumb header
        assert out.index("Chose JWT over OAuth") < out.index("JWT is stateless.")

    def test_never_emits_raw_yaml(self):
        out = build_embed_text(
            content="body",
            heading_path="A > B",
            title="T",
            note_type="learning",
            tags=["x", "y"],
        )
        assert "---" not in out
        assert "{" not in out and "}" not in out
        assert "tags:" not in out  # no YAML key syntax

    def test_omits_empty_tags_and_missing_heading_path(self):
        out = build_embed_text(
            content="body",
            heading_path=None,
            title="T",
            note_type="state",
            tags=[],
        )
        assert "Tags" not in out
        assert "Section" not in out
        assert "body" in out
        assert "T" in out

    def test_content_is_preserved_verbatim(self):
        content = "line one\n\nline two with detail"
        out = build_embed_text(
            content=content,
            heading_path=None,
            title="T",
            note_type="code",
            tags=[],
        )
        assert content in out


# =============================================================================
# embedding_version — pipeline stamp (model : dims : chunker version)
# =============================================================================


class TestEmbeddingVersion:
    def test_stamps_model_dims_and_chunker_version(self):
        assert (
            embedding_version("qwen3-embedding-8b", 4096, "c1")
            == "qwen3-embedding-8b:4096:c1"
        )

    def test_defaults_to_module_chunker_version(self):
        stamp = embedding_version("qwen3-embedding-8b", 4096)
        assert stamp.endswith(f":{CHUNKER_VERSION}")


# =============================================================================
# embed_note_chunks — async pipeline wiring the bulk embed_batch
# =============================================================================


class TestEmbedNoteChunks:
    @pytest.mark.asyncio
    async def test_embeds_each_chunk_via_bulk_embed_batch(self):
        svc = _make_embedding_service()
        body = "# A\naaa\n## B\nbbb\n### C\nccc"
        rows, _ = await embed_note_chunks(
            body,
            title="A",
            note_type="decision",
            tags=["auth"],
            embedding_service=svc,
            token_counter=_wc,
            target_tokens=3,  # forces 3 chunks
        )
        # one bulk call, not one-per-chunk
        assert svc.embed_batch.await_count == 1
        assert len(rows) == 3
        # each row carries the vector embed_batch returned for its position
        assert rows[0]["embedding"] == [0.0]
        assert rows[2]["embedding"] == [2.0]

    @pytest.mark.asyncio
    async def test_row_shape_matches_replace_note_chunks(self):
        svc = _make_embedding_service()
        rows, _ = await embed_note_chunks(
            "# A\n\nshort body",
            title="A",
            note_type="learning",
            tags=[],
            embedding_service=svc,
        )
        assert len(rows) == 1
        assert set(rows[0].keys()) == {
            "chunk_ix",
            "heading_path",
            "content",
            "embedding",
        }
        assert rows[0]["chunk_ix"] == 0
        assert rows[0]["heading_path"] == "A"

    @pytest.mark.asyncio
    async def test_embed_texts_carry_the_breadcrumb(self):
        svc = _make_embedding_service()
        await embed_note_chunks(
            "# A\n\nJWT is stateless.",
            title="Chose JWT",
            note_type="decision",
            tags=["auth"],
            embedding_service=svc,
        )
        (texts,) = svc.embed_batch.await_args[0]
        assert "Chose JWT" in texts[0]  # breadcrumb prefix, not raw content
        assert "decision" in texts[0]
        assert "JWT is stateless." in texts[0]

    @pytest.mark.asyncio
    async def test_returns_embedding_version_from_service(self):
        svc = _make_embedding_service(model="qwen3-embedding-8b", dims=4096)
        _, version = await embed_note_chunks(
            "# A\n\nbody",
            title="A",
            note_type="learning",
            tags=[],
            embedding_service=svc,
        )
        assert version == f"qwen3-embedding-8b:4096:{CHUNKER_VERSION}"

    @pytest.mark.asyncio
    async def test_empty_body_makes_no_embed_call(self):
        svc = _make_embedding_service()
        rows, version = await embed_note_chunks(
            "   ",
            title="A",
            note_type="learning",
            tags=[],
            embedding_service=svc,
        )
        assert rows == []
        assert version == f"qwen3-embedding-8b:4096:{CHUNKER_VERSION}"
        svc.embed_batch.assert_not_awaited()
