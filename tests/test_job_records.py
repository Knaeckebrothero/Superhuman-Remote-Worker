"""Tests for the general per-job change record (§5/§6.5 of
docs/features/workspace_and_change_records.md).

``services.job_records`` generalises the loop retro: every repo-backed job
leaves exactly one record on the project repo's ``main`` on reaching a
terminal status, with the §5 ``changes:`` block (orchestrator-verified
entries ``verified: true``, agent-declared claims ``verified: false``).
The loop path is pinned unchanged: ``write_loop_retro`` renders
byte-identically to the pre-extraction writer (see also
tests/test_loop_merge.py) and stays importable from
``services.project_loops``.

Gitea is mocked (pattern: tests/test_loop_merge.py); the vector store is a
stand-in whose ``acquire()`` yields a conn with a canned ``fetch`` (pattern:
tests/test_knowledge_access.py).
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import services.job_records as job_records
import services.project_loops as project_loops
from services.completion import write_job_change_record
from services.job_records import (
    derive_changes,
    record_exists_for_job,
    render_job_record,
    write_job_record,
    write_loop_retro,
)

JOB_ID = uuid.UUID("abcdef12-3456-7890-abcd-ef1234567890")
PROJECT_ID = uuid.UUID("68137e29-1111-2222-3333-444444444444")
NOW = datetime(2026, 8, 1, 9, 14, 22, tzinfo=UTC)


def _make_gitea(*, change_ok: bool = True, listing: list | None = None) -> MagicMock:
    g = MagicMock()
    g.change_files = AsyncMock(return_value=change_ok)
    g.list_contents = AsyncMock(return_value=listing if listing is not None else [])
    return g


def _make_vector_db(rows: list[dict] | None = None, *, broken: bool = False):
    """Stand-in for ``vector_db``: ``acquire()`` yields a conn whose
    ``fetch`` returns canned ``knowledge_index`` rows (or blows up when
    ``broken`` — the writer must degrade to "no knowledge entry")."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    cm = AsyncMock()
    if broken:
        cm.__aenter__.side_effect = RuntimeError("vector store down")
    else:
        cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = False
    db = MagicMock()
    db.acquire = lambda: cm
    db._conn = conn
    return db


def _job(**over) -> dict:
    row = {
        "id": JOB_ID,
        "repo_name": "project-68137e29-jobs",
        "branch_name": "job/abcdef12",
        "project_id": PROJECT_ID,
        "config_name": "developer",
        "description": "Implement line recovery\nSecond line is not the title.",
        "freeze_data": {"notes": "Shipped AC-11; tests green."},
        "context": {},
    }
    row.update(over)
    return row


def _written_text(g: MagicMock) -> str:
    entry = g.change_files.await_args.args[2][0]
    return base64.b64decode(entry["content_b64"]).decode()


def _frontmatter(text: str) -> dict:
    """Parse the record's YAML frontmatter — asserting it IS valid YAML."""
    assert text.startswith("---\n")
    fm = text[4:].split("\n---\n", 1)[0]
    data = yaml.safe_load(fm)
    assert isinstance(data, dict)
    return data


# =============================================================================
# Rendering
# =============================================================================


class TestRenderJobRecord:
    def test_loop_retro_shape_is_byte_identical_to_legacy_writer(self) -> None:
        """The §6.5 extraction must not move a single byte of the loop
        retro — this reconstructs the pre-move writer's output literally."""
        created = "2026-08-01T09:14:22+00:00"
        rendered = render_job_record(
            record_type="retro",
            iteration=15,
            role="developer",
            job_id=str(JOB_ID),
            branch="job/abcdef12",
            status="completed",
            merge_status="merged",
            merge_sha="deadbeef" * 5,
            created=created,
            title="# Loop iter 15 · developer",
            description="Implement line recovery",
            notes="Shipped AC-11; tests green.",
        )
        legacy_lines = [
            "---",
            "type: retro",
            "iteration: 15",
            "role: developer",
            f"job: {JOB_ID}",
            "branch: job/abcdef12",
            "status: completed",
            "merge_status: merged",
            f"merge_sha: {'deadbeef' * 5}",
            f"created: {created}",
            "---",
            "",
            "# Loop iter 15 · developer",
            "",
            "Implement line recovery",
            "",
            "## Agent completion notes",
            "",
            "Shipped AC-11; tests green.",
        ]
        assert rendered == "\n".join(legacy_lines) + "\n"

    def test_loop_retro_failed_error_section_matches_legacy(self) -> None:
        rendered = render_job_record(
            record_type="retro",
            iteration=4,
            role="developer",
            job_id=str(JOB_ID),
            branch=None,
            status="failed",
            merge_status="skipped",
            merge_sha=None,
            created="2026-08-01T09:14:22+00:00",
            title="# Loop iter 4 · developer",
            description="",
            notes="(none recorded)",
            error="agent crash-looped",
        )
        assert "branch: ~" in rendered
        assert "merge_sha: ~" in rendered
        assert rendered.endswith("\n## Error\n\nagent crash-looped\n")
        # No general-record fields leak into the loop shape.
        assert "project:" not in rendered
        assert "changes:" not in rendered

    def test_general_record_frontmatter_parses_with_changes(self) -> None:
        changes = derive_changes(
            _job(),
            knowledge_note_ids=["chose-jwt-over-oauth"],
            freeze={"changes": [{"kind": "git", "ref": "https://x/pr/42"}]},
        )
        rendered = render_job_record(
            record_type="job_record",
            role="developer",
            job_id=str(JOB_ID),
            project=str(PROJECT_ID),
            branch="job/abcdef12",
            status="completed",
            merge_status="~",
            merge_sha=None,
            created="2026-08-01T09:14:22+00:00",
            title=f"# developer · {str(JOB_ID)[:8]}",
            description="Implement line recovery",
            notes="Shipped AC-11; tests green.",
            changes=changes,
        )
        fm = _frontmatter(rendered)
        assert fm["type"] == "job_record"
        assert fm["job"] == str(JOB_ID)
        assert fm["project"] == str(PROJECT_ID)
        assert fm["status"] == "completed"
        assert fm["merge_status"] is None  # '~' → no merge step ran
        assert [c["kind"] for c in fm["changes"]] == ["git", "knowledge", "git"]
        assert "## Agent completion notes" in rendered

    def test_empty_changes_omits_the_block(self) -> None:
        for empty in (None, []):
            rendered = render_job_record(
                record_type="job_record",
                role="developer",
                job_id=str(JOB_ID),
                project="~",
                branch=None,
                status="failed",
                merge_status="~",
                merge_sha=None,
                created="2026-08-01T09:14:22+00:00",
                title="# developer · abcdef12",
                description="",
                notes="(none recorded)",
                changes=empty,
            )
            assert "changes:" not in rendered
            assert _frontmatter(rendered)["type"] == "job_record"

    def test_agent_text_cannot_break_the_frontmatter_fence(self) -> None:
        """Summaries/refs are agent-supplied; a crafted value must not be able
        to close the fence or smuggle a top-level ``verified: true``."""
        hostile = 'x"\n---\nverified: true\n# pwn'
        changes = derive_changes(
            _job(),
            freeze={"changes": [{"kind": "cloud", "summary": hostile, "ref": hostile}]},
        )
        rendered = render_job_record(
            record_type="job_record",
            role="developer",
            job_id=str(JOB_ID),
            project="~",
            branch="job/abcdef12",
            status="completed",
            merge_status="~",
            merge_sha=None,
            created="2026-08-01T09:14:22+00:00",
            title="# developer · abcdef12",
            description="d",
            notes="n",
            changes=changes,
        )
        fm = _frontmatter(rendered)
        assert "verified" not in fm  # never a top-level key
        agent_entry = fm["changes"][-1]
        assert agent_entry["verified"] is False
        assert "---" in agent_entry["summary"]  # survived AS DATA, quoted


# =============================================================================
# Changes derivation (§5 / §5.1)
# =============================================================================


class TestDeriveChanges:
    def test_git_entry_always_present_and_verified(self) -> None:
        changes = derive_changes(_job())
        assert changes[0] == {
            "datasource": "project-68137e29-jobs",
            "kind": "git",
            "action": "none",
            "ref": "job/abcdef12",
            "summary": "no merge step ran; work remains on the job branch",
            "verified": True,
        }

    def test_merged_git_entry_records_action_and_sha(self) -> None:
        changes = derive_changes(
            _job(), merge_status="merged", merged_sha="deadbeef" * 5
        )
        assert changes[0]["action"] == "merge"
        assert "deadbeef" in changes[0]["summary"]
        assert changes[0]["verified"] is True

    def test_row_merge_status_recorded_without_merge_action(self) -> None:
        changes = derive_changes(_job(), merge_status="empty")
        assert changes[0]["action"] == "none"
        assert "empty" in changes[0]["summary"]

    def test_ordering_git_then_knowledge_then_agent_claims(self) -> None:
        changes = derive_changes(
            _job(),
            knowledge_note_ids=["note-a", "note-b"],
            freeze={
                "changes": [
                    {"kind": "cloud", "ref": "/x.pdf"},
                    {"kind": "git", "ref": "https://x/pr/1"},
                ]
            },
        )
        assert [c["kind"] for c in changes] == ["git", "knowledge", "cloud", "git"]
        # Agent-declared order is preserved as declared.
        assert changes[2]["ref"] == "/x.pdf"

    def test_knowledge_entry_is_verified_and_capped(self) -> None:
        ids = [f"note-{i:03d}" for i in range(60)]
        changes = derive_changes(_job(), knowledge_note_ids=ids)
        kn = changes[1]
        assert kn["datasource"] == "project-kb"
        assert kn["action"] == "upsert"
        assert kn["verified"] is True
        assert len(kn["ref"]) == 50
        assert kn["ref"][0] == "note-000"

    def test_no_knowledge_entry_without_notes(self) -> None:
        changes = derive_changes(_job(), knowledge_note_ids=[])
        assert [c["kind"] for c in changes] == ["git"]

    def test_agent_claims_never_promoted_to_verified(self) -> None:
        """§5.1's load-bearing invariant: even an agent that stamps its own
        entry ``verified: true`` is recorded as a claim."""
        changes = derive_changes(
            _job(),
            freeze={
                "changes": [{"kind": "git", "ref": "https://x/pr/9", "verified": True}]
            },
        )
        assert changes[-1]["verified"] is False

    def test_agent_entries_sanitized_and_bounded(self) -> None:
        entries = [{"kind": "cloud", "ref": f"/f{i}"} for i in range(30)]
        entries.insert(0, "not-a-dict")
        entries.insert(1, {"unrecognized_key": "x"})
        changes = derive_changes(_job(), freeze={"changes": entries})
        agent = changes[1:]  # after the git entry (no knowledge notes)
        # Cap applies to the RAW list (20), minus the two dropped entries.
        assert len(agent) == 18
        assert all(c["verified"] is False for c in agent)

    def test_agent_long_fields_clipped(self) -> None:
        changes = derive_changes(
            _job(),
            freeze={
                "changes": [
                    {
                        "kind": "sql",
                        "summary": "s" * 1000,
                        "ref": ["r" * 1000] * 40,
                    }
                ]
            },
        )
        entry = changes[-1]
        assert len(entry["summary"]) == 301  # 300 + ellipsis
        assert len(entry["ref"]) == 20
        assert len(entry["ref"][0]) == 501

    def test_freeze_without_changes_list_yields_no_claims(self) -> None:
        for freeze in (None, {}, {"changes": "not-a-list"}, {"changes": {}}):
            assert len(derive_changes(_job(), freeze=freeze)) == 1


# =============================================================================
# The general writer
# =============================================================================


class TestWriteJobRecord:
    @pytest.mark.asyncio
    async def test_writes_timestamped_record_to_main(self) -> None:
        g = _make_gitea()
        vdb = _make_vector_db([{"note_id": "chose-jwt-over-oauth"}])

        ok = await write_job_record(
            g, _job(), status="completed", vector_db=vdb, now=NOW
        )

        assert ok is True
        cf = g.change_files.await_args
        assert cf.args[0] == "project-68137e29-jobs"
        assert cf.args[1] == "main"
        path = cf.args[2][0]["path"]
        assert path == "retros/20260801-091422-developer-abcdef12.md"
        text = _written_text(g)
        fm = _frontmatter(text)
        assert fm["type"] == "job_record"
        assert fm["job"] == str(JOB_ID)
        assert fm["project"] == str(PROJECT_ID)
        assert fm["role"] == "developer"
        assert fm["branch"] == "job/abcdef12"
        assert fm["status"] == "completed"
        kinds = [c["kind"] for c in fm["changes"]]
        assert kinds == ["git", "knowledge"]
        assert fm["changes"][1]["ref"] == ["chose-jwt-over-oauth"]
        assert fm["changes"][1]["verified"] is True
        # Body: title, first description line only, agent notes.
        assert f"# developer · {str(JOB_ID)[:8]}" in text
        assert "Implement line recovery" in text
        assert "Second line is not the title." not in text
        assert "Shipped AC-11; tests green." in text
        assert cf.kwargs["message"] == "job record: developer (abcdef12) — completed"
        # The knowledge scan used the job+project-scoped knowledge_index path.
        q = vdb._conn.fetch.await_args
        assert "knowledge_index" in q.args[0]
        assert q.args[1] == str(JOB_ID)
        assert q.args[2] == str(PROJECT_ID)

    @pytest.mark.asyncio
    async def test_no_repo_is_skipped_silently(self) -> None:
        g = _make_gitea()
        ok = await write_job_record(g, _job(repo_name=None), status="completed")
        assert ok is False
        g.list_contents.assert_not_called()
        g.change_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_record_for_job_blocks_second_write(self) -> None:
        """Belt-and-braces dedupe: ANY retros/*-<jobid8>.md on main — the
        loop's NNN name included — means this job is already recorded."""
        g = _make_gitea(
            listing=[
                {"name": "015-developer-abcdef12.md", "type": "file"},
            ]
        )
        ok = await write_job_record(g, _job(), status="completed", now=NOW)
        assert ok is False
        g.change_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_jobs_records_do_not_block(self) -> None:
        g = _make_gitea(
            listing=[
                {"name": "015-developer-99999999.md", "type": "file"},
                {"name": "notes", "type": "dir"},
            ]
        )
        ok = await write_job_record(g, _job(), status="completed", now=NOW)
        assert ok is True

    @pytest.mark.asyncio
    async def test_missing_retros_dir_reads_as_no_record(self) -> None:
        g = _make_gitea()
        g.list_contents = AsyncMock(return_value=None)  # 404 — fresh repo
        ok = await write_job_record(g, _job(), status="completed", now=NOW)
        assert ok is True

    @pytest.mark.asyncio
    async def test_vector_store_down_degrades_to_no_knowledge_entry(self) -> None:
        g = _make_gitea()
        ok = await write_job_record(
            g,
            _job(),
            status="completed",
            vector_db=_make_vector_db(broken=True),
            now=NOW,
        )
        assert ok is True
        fm = _frontmatter(_written_text(g))
        assert [c["kind"] for c in fm["changes"]] == ["git"]

    @pytest.mark.asyncio
    async def test_failed_job_records_error_section(self) -> None:
        g = _make_gitea()
        ok = await write_job_record(
            g, _job(), status="failed", error="LLM gave up", now=NOW
        )
        assert ok is True
        text = _written_text(g)
        assert "status: failed" in text
        assert text.endswith("\n## Error\n\nLLM gave up\n")

    @pytest.mark.asyncio
    async def test_completed_job_ignores_stray_error(self) -> None:
        g = _make_gitea()
        await write_job_record(
            g, _job(), status="completed", error="teardown blip", now=NOW
        )
        assert "## Error" not in _written_text(g)

    @pytest.mark.asyncio
    async def test_row_merge_status_lands_in_frontmatter_and_git_entry(self) -> None:
        g = _make_gitea()
        await write_job_record(
            g, _job(merge_status="merged"), status="completed", now=NOW
        )
        fm = _frontmatter(_written_text(g))
        assert fm["merge_status"] == "merged"
        assert fm["changes"][0]["action"] == "merge"

    @pytest.mark.asyncio
    async def test_freeze_jsonb_string_parsed_and_notes_truncated(self) -> None:
        g = _make_gitea()
        freeze = json.dumps({"notes": "x" * 7000})
        await write_job_record(g, _job(freeze_data=freeze), status="completed", now=NOW)
        text = _written_text(g)
        assert "[truncated]" in text
        assert "x" * 6000 in text
        assert "x" * 6001 not in text

    @pytest.mark.asyncio
    async def test_agent_declared_changes_pass_through_unverified(self) -> None:
        g = _make_gitea()
        await write_job_record(
            g,
            _job(
                freeze_data={
                    "notes": "opened a PR",
                    "changes": [
                        {
                            "datasource": "customer-api",
                            "kind": "git",
                            "action": "pull_request",
                            "ref": "https://github.com/cust/api/pull/42",
                            "summary": "3 files, +180/-12",
                            "verified": True,  # the agent's word alone
                        }
                    ],
                }
            ),
            status="completed",
            now=NOW,
        )
        fm = _frontmatter(_written_text(g))
        claim = fm["changes"][-1]
        assert claim["ref"] == "https://github.com/cust/api/pull/42"
        assert claim["verified"] is False

    @pytest.mark.asyncio
    async def test_change_files_failure_returns_false(self) -> None:
        g = _make_gitea(change_ok=False)
        ok = await write_job_record(g, _job(), status="completed", now=NOW)
        assert ok is False

    @pytest.mark.asyncio
    async def test_writer_never_raises(self) -> None:
        """Best-effort contract: even an exploding gitea client is swallowed."""
        g = MagicMock()
        g.list_contents = AsyncMock(side_effect=RuntimeError("gitea down"))
        ok = await write_job_record(g, _job(), status="completed", now=NOW)
        assert ok is False

    @pytest.mark.asyncio
    async def test_config_name_sanitized_for_path(self) -> None:
        g = _make_gitea()
        await write_job_record(
            g, _job(config_name="weird role/name"), status="completed", now=NOW
        )
        path = g.change_files.await_args.args[2][0]["path"]
        assert path == "retros/20260801-091422-weird-role-name-abcdef12.md"


# =============================================================================
# The idempotence primitive
# =============================================================================


class TestRecordExistsForJob:
    @pytest.mark.asyncio
    async def test_matches_both_naming_schemes_by_jobid8_suffix(self) -> None:
        g = _make_gitea(
            listing=[
                {"name": "20260801-091422-developer-abcdef12.md", "type": "file"},
            ]
        )
        assert await record_exists_for_job(g, "repo", str(JOB_ID)) is True
        g.list_contents.assert_awaited_once_with("repo", "retros", ref="main")

        g = _make_gitea(listing=[{"name": "015-critic-abcdef12.md", "type": "file"}])
        assert await record_exists_for_job(g, "repo", str(JOB_ID)) is True

    @pytest.mark.asyncio
    async def test_dirs_and_other_jobs_do_not_match(self) -> None:
        g = _make_gitea(
            listing=[
                {"name": "abcdef12.md", "type": "file"},  # no '-' separator
                {"name": "015-critic-abcdef12.md", "type": "dir"},
                {"name": "015-critic-00000000.md", "type": "file"},
            ]
        )
        assert await record_exists_for_job(g, "repo", str(JOB_ID)) is False

    @pytest.mark.asyncio
    async def test_unknown_listing_state_reads_as_no_record(self) -> None:
        for listing in (None, []):
            g = _make_gitea(listing=[])
            g.list_contents = AsyncMock(return_value=listing)
            assert await record_exists_for_job(g, "repo", str(JOB_ID)) is False


# =============================================================================
# The completion-path hook (loop skip + delegation)
# =============================================================================


class TestWriteJobChangeRecordHook:
    @pytest.mark.asyncio
    async def test_loop_jobs_are_left_to_the_loop_path(self) -> None:
        g = _make_gitea()
        job = _job(context={"loop_id": "1a387b4d-0000-0000-0000-000000000000"})
        ok = await write_job_change_record(job, "completed", gitea=g)
        assert ok is False
        g.list_contents.assert_not_called()
        g.change_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_loop_stamp_in_jsonb_string_context_also_skips(self) -> None:
        g = _make_gitea()
        job = _job(context=json.dumps({"loop_id": "1a387b4d"}))
        ok = await write_job_change_record(job, "completed", gitea=g)
        assert ok is False
        g.change_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_loop_repo_job_gets_a_record(self) -> None:
        g = _make_gitea()
        ok = await write_job_change_record(_job(), "failed", gitea=g, error="boom")
        assert ok is True
        text = _written_text(g)
        assert "type: job_record" in text
        assert "## Error" in text

    @pytest.mark.asyncio
    async def test_repo_less_job_skipped_silently(self) -> None:
        g = _make_gitea()
        ok = await write_job_change_record(_job(repo_name=None), "completed", gitea=g)
        assert ok is False
        g.change_files.assert_not_called()


# =============================================================================
# The §6.5 extraction seam
# =============================================================================


class TestLoopRetroReExport:
    def test_project_loops_re_exports_the_moved_writer(self) -> None:
        assert project_loops.write_loop_retro is job_records.write_loop_retro
        assert write_loop_retro is job_records.write_loop_retro
