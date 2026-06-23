"""
Async PostgreSQL round-trip for the SRW-native citation engine.

This is the canonical integration test for the citation engine after the native
integration (docs/features/citation_engine_integration.md): the engine is async
and runs against SRW's vector store through a ``PostgresDB`` pool. With SQLite /
``mode="basic"`` removed (decision D3), this Postgres round-trip is the engine's
primary automated coverage — the fast in-process unit tests are gone.

It is **skip-guarded** behind ``RUN_POSTGRES_TESTS=true`` and never runs in CI's
default ``pytest tests/`` sweep. Run it against the k3d/dev vector DB — see
``tests/citation_engine/README.md`` for the port-forward + env recipe.

The round-trip exercises every asyncpg-strictness point the rewrite had to get
right: ``job_id`` UUID coercion, JSONB encode/decode, enum casts, ``vector``
columns, ``content_hash`` dedup, FTS keyword search, the verification-reset on
edit, and job-scoped isolation. Verification (Phase 2) runs on SRW's
auxiliary-LLM service: without a verifier wired, ``cite_*`` returns ``pending``
(no model endpoint needed); the async write-back path is covered separately
with a mocked auxiliary verdict.
"""

import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_TESTS", "").lower() != "true",
        reason="Set RUN_POSTGRES_TESTS=true (+ CITATION_DB_URL or VECTOR_POSTGRES_*) to run.",
    ),
]


def _vector_url() -> str | None:
    """Resolve the vector-store DSN (citations live in srw_vector)."""
    url = os.getenv("CITATION_DB_URL")
    if url:
        return url
    from src.utils.db_url import build_postgres_url

    return build_postgres_url("VECTOR_POSTGRES", fallback_env="VECTOR_DB_URL")


@pytest_asyncio.fixture
async def engine_ctx():
    """Yield (engine, db, job_id) bound to the vector store; clean up after."""
    from src.citation_engine import CitationContext, CitationEngine
    from src.database.postgres_db import PostgresDB

    url = _vector_url()
    if not url:
        pytest.skip("No vector DB URL (set CITATION_DB_URL or VECTOR_POSTGRES_*).")

    # No verifier wired on this engine — cite_* returns 'pending' without an
    # LLM. The async verdict write-back is covered in its own test below.
    db = PostgresDB(connection_string=url)
    await db.connect()

    job_id = uuid.uuid4()
    ctx = CitationContext(session_id=str(job_id), agent_id="pg-itest")
    engine = CitationEngine(db=db, context=ctx)

    try:
        yield engine, db, job_id
    finally:
        # Clean up this job's rows in FK order. The test content is unique
        # (it embeds the job_id), so its sources are exclusive to this job.
        async with db.acquire() as conn:
            src_ids = [
                r["source_id"]
                for r in await conn.fetch(
                    "SELECT source_id FROM job_sources WHERE job_id = $1", job_id
                )
            ]
            await conn.execute("DELETE FROM citations WHERE job_id = $1", job_id)
            await conn.execute(
                "DELETE FROM source_annotations WHERE job_id = $1", job_id
            )
            await conn.execute("DELETE FROM source_tags WHERE job_id = $1", job_id)
            await conn.execute(
                "DELETE FROM source_embeddings WHERE job_id = $1", job_id
            )
            await conn.execute("DELETE FROM job_sources WHERE job_id = $1", job_id)
            for sid in src_ids:
                shared = await conn.fetchval(
                    "SELECT COUNT(*) FROM job_sources WHERE source_id = $1", sid
                )
                if not shared:
                    await conn.execute(
                        "DELETE FROM citations WHERE source_id = $1", sid
                    )
                    await conn.execute(
                        "DELETE FROM source_embeddings WHERE source_id = $1", sid
                    )
                    await conn.execute(
                        "DELETE FROM source_annotations WHERE source_id = $1", sid
                    )
                    await conn.execute(
                        "DELETE FROM source_tags WHERE source_id = $1", sid
                    )
                    await conn.execute("DELETE FROM sources WHERE id = $1", sid)
        await db.close()


@pytest.mark.asyncio
async def test_source_registration_and_dedup(engine_ctx):
    engine, _db, job_id = engine_ctx
    content = f"Companies must retain records for 10 years. marker={job_id}"

    source = await engine.add_custom_source(
        name="GoBD note", content=content, description="test"
    )
    assert source.id
    assert source.type.value == "custom"
    assert source.content_hash

    # content_hash dedup: identical content reuses the same source row.
    dup = await engine.add_custom_source(name="dup", content=content)
    assert dup.id == source.id


@pytest.mark.asyncio
async def test_cite_read_edit_and_isolation(engine_ctx):
    engine, _db, job_id = engine_ctx
    content = f"Companies must retain records for 10 years. marker={job_id}"
    source = await engine.add_custom_source(name="GoBD note", content=content)

    # No verifier wired → cite_* returns immediately with status=pending.
    result = await engine.cite_custom(
        claim="Records retained 10 years",
        source_id=source.id,
        quote_context=content,
        relevance_reasoning="states retention period",
    )
    assert result.citation_id
    assert result.verification_status.value == "pending"

    cit = await engine.get_citation(result.citation_id)
    assert cit is not None
    assert cit.source_id == source.id

    assert any(s.id == source.id for s in await engine.list_sources())
    assert any(c.id == result.citation_id for c in await engine.list_citations())

    # Editing a content field resets verification to pending.
    await engine.edit_citation(
        result.citation_id, claim="Records retained for ten years"
    )
    edited = await engine.get_citation(result.citation_id)
    assert edited.claim == "Records retained for ten years"
    assert edited.verification_status.value == "pending"

    # Job-scoped isolation: another job cannot read this citation.
    other = await engine.get_citation(result.citation_id, job_id=str(uuid.uuid4()))
    assert other is None


@pytest.mark.asyncio
async def test_annotations_tags_and_keyword_search(engine_ctx):
    engine, _db, job_id = engine_ctx
    content = f"Companies must retain records for 10 years. marker={job_id}"
    source = await engine.add_custom_source(name="GoBD note", content=content)

    ann = await engine.annotate_source(source.id, "important", annotation_type="note")
    assert ann.id
    assert any(a.id == ann.id for a in await engine.get_annotations(source.id))

    tags = await engine.tag_source(source.id, ["gobd", "retention"])
    assert "gobd" in tags and "retention" in tags
    assert set(await engine.get_tags(source.id)) >= {"gobd", "retention"}

    # Keyword (FTS) search — no embedding endpoint required.
    results = await engine.search_library("retain records", mode="keyword")
    assert any(r.source_id == source.id for r in results.results)


@pytest.mark.asyncio
async def test_cloud_anchor_metadata_persists(engine_ctx, tmp_path):
    """A cloud-document source persists its snapshot-anchor on metadata.cloud.

    Phase 3 (D7): when a cited document was read from a user's cloud, the engine
    stores the drift fingerprint (etag + raw-bytes hash) and best-effort live
    pointer (backend / path / webdav_url) inside the source's JSONB metadata.
    This exercises the real add_doc_source(metadata=...) → JSONB → get_source
    round-trip on the live schema (no new column needed).
    """
    doc = tmp_path / "cloud_doc.txt"
    engine, _db, job_id = engine_ctx
    doc.write_text(f"Cloud retention policy: 10 years. marker={job_id}")

    anchor = {
        "backend": "webdav",
        "path": "/Documents/cloud_doc.txt",
        "webdav_url": "https://cloud.example/remote.php/dav/Documents/cloud_doc.txt",
        "etag": '"abc123"',
        "file_sha256": "deadbeefcafe",
        "captured_at": "2026-06-20T00:00:00+00:00",
    }
    source = await engine.add_doc_source(str(doc), metadata={"cloud": anchor})
    assert source.id

    fetched = await engine.get_source(source.id)
    assert fetched is not None and fetched.metadata is not None
    cloud = fetched.metadata.get("cloud")
    assert cloud is not None
    assert cloud["backend"] == "webdav"
    assert cloud["etag"] == '"abc123"'
    assert cloud["file_sha256"] == "deadbeefcafe"
    assert cloud["webdav_url"].endswith("cloud_doc.txt")


@pytest.mark.asyncio
async def test_async_verification_writeback(engine_ctx):
    """cite_* schedules verification; the verdict is written back async (Phase 2).

    Uses a mocked auxiliary verdict so no model endpoint is needed, but exercises
    the real path: engine self-schedules ``verify_and_store_citation`` →
    ``AuxiliaryLLM.chain`` → ``_update_verification_status`` on the live schema.
    """
    from src.citation_engine import CitationEngine
    from src.services.auxiliary import CitationVerdict

    engine, db, job_id = engine_ctx
    content = f"Companies must retain records for 10 years. marker={job_id}"

    # Mock the citation-verification AuxiliaryLLM: .chain() returns a verdict,
    # .health records outcomes.
    mock_aux = MagicMock()
    mock_aux.chain = AsyncMock(
        return_value=CitationVerdict(
            verified=True,
            similarity_score=0.95,
            matched_text="retain records for 10 years",
            reasoning="exact phrase present in source",
        )
    )
    mock_aux.health = MagicMock()

    # A second engine on the SAME job (same context → same job_id, so the
    # fixture cleanup collects its rows) but WITH a verifier wired.
    verify_engine = CitationEngine(
        db=db,
        context=engine.context,
        verify_aux=mock_aux,
        verify_prompt="verify the citation",
    )

    source = await verify_engine.add_custom_source(name="GoBD note", content=content)
    result = await verify_engine.cite_custom(
        claim="Records retained 10 years",
        source_id=source.id,
        quote_context=content,
        relevance_reasoning="states retention period",
    )
    # Returns immediately as pending (D2 — eventually-consistent).
    assert result.verification_status.value == "pending"

    # Let the background verification land.
    await verify_engine.await_pending_verifications(timeout=10)

    mock_aux.chain.assert_awaited_once()
    mock_aux.health.record_success.assert_called_with("citation_verification")

    cit = await verify_engine.get_citation(result.citation_id)
    assert cit is not None
    assert cit.verification_status.value == "verified"
    assert cit.similarity_score == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_failed_verdict_surfaces_for_feedback(engine_ctx):
    """A negative verdict → 'failed' → queryable for the Phase 2b feedback inject.

    This is the exact path the worker execute node uses to surface failed
    citations back to the agent (``list_citations(verification_status='failed')``
    → ``format_failed_citations``).
    """
    from src.citation_engine import CitationEngine
    from src.core.citation_feedback_injection import format_failed_citations
    from src.services.auxiliary import CitationVerdict

    engine, db, job_id = engine_ctx
    content = f"Unrelated text about the weather. marker={job_id}"

    mock_aux = MagicMock()
    mock_aux.chain = AsyncMock(
        return_value=CitationVerdict(
            verified=False,
            similarity_score=0.1,
            matched_text=None,
            reasoning="the quote does not appear in the source",
        )
    )
    mock_aux.health = MagicMock()

    verify_engine = CitationEngine(
        db=db, context=engine.context, verify_aux=mock_aux, verify_prompt="verify"
    )
    source = await verify_engine.add_custom_source(name="weather note", content=content)
    result = await verify_engine.cite_custom(
        claim="Companies must retain records for 10 years",
        source_id=source.id,
        quote_context="records must be retained for 10 years",
    )
    await verify_engine.await_pending_verifications(timeout=10)

    failed = await verify_engine.list_citations(verification_status="failed")
    assert any(c.id == result.citation_id for c in failed)

    block = format_failed_citations(failed)
    assert f"[{result.citation_id}]" in block
    assert "edit_citation" in block
