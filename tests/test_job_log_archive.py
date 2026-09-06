"""Tests for the job log archive read path (knowledge-base/knowledge/features/job_log_archive.md).

Covers the pure helpers behind GET /api/jobs/{id}/logs and
GET /api/persistent/threads/{id}/logs:
  - _scope_archived_lines(): disaggregate a shared pod log by id-tagged lines
  - _filter_log_lines(): level/grep filtering across text + JSON formats
  - _read_archived_agent_log(): stitch S3 blobs referenced by a row
  - format_thread_log()/format_job_log(): archived-log headers

The capture side (_archive_pod_logs) is covered in test_agent_provisioner.py.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from orchestrator.main import (
    _filter_log_lines,
    _read_archived_agent_log,
    _scope_archived_lines,
)
from shared.orch_surface.formatters import format_job_log, format_thread_log

JOB_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"


class TestScopeArchivedLines:
    def test_scopes_to_tagged_lines(self):
        text = "\n".join(
            [
                f'{{"level": "INFO", "job_id": "{JOB_ID}", "message": "mine"}}',
                f'{{"level": "INFO", "job_id": "{OTHER_ID}", "message": "other"}}',
                f'{{"level": "ERROR", "job_id": "{JOB_ID}", "message": "boom"}}',
            ]
        )
        lines = _scope_archived_lines(text, JOB_ID)
        assert len(lines) == 2
        assert all(JOB_ID in line for line in lines)

    def test_returns_whole_log_when_nothing_matches(self):
        # Text-format (or pre-Slice-0) pod log: no line carries the uuid —
        # return it whole rather than hide it.
        text = "plain line one\nplain line two"
        assert _scope_archived_lines(text, JOB_ID) == text.splitlines()


class TestFilterLogLines:
    def test_level_matches_text_format(self):
        lines = [
            "2026-07-15 10:00:00 - src.graph - ERROR - kaboom",
            "2026-07-15 10:00:01 - src.graph - INFO - fine",
        ]
        out, filtered = _filter_log_lines(lines, level="error", grep=None)
        assert filtered is True
        assert out == [lines[0]]

    def test_level_matches_json_format_with_kubelet_timestamp(self):
        lines = [
            '2026-07-15T10:00:00.0Z {"level": "ERROR", "message": "kaboom"}',
            '2026-07-15T10:00:01.0Z {"level": "INFO", "message": "fine"}',
        ]
        out, filtered = _filter_log_lines(lines, level="ERROR", grep=None)
        assert filtered is True
        assert out == [lines[0]]

    def test_grep_is_case_insensitive(self):
        lines = ["Alpha Beta", "gamma delta"]
        out, filtered = _filter_log_lines(lines, level=None, grep="ALPHA")
        assert filtered is True
        assert out == ["Alpha Beta"]

    def test_no_filters_passthrough(self):
        lines = ["a", "b"]
        out, filtered = _filter_log_lines(lines, level=None, grep=None)
        assert out == lines
        assert filtered is False

    def test_invalid_level_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            _filter_log_lines(["x"], level="LOUD", grep=None)
        assert exc.value.status_code == 400


class TestReadArchivedAgentLog:
    @pytest.mark.asyncio
    async def test_stitches_blobs_in_key_order(self):
        meta = {"log_archive_keys": ["agent_logs/p/1.log", "agent_logs/p/2.log"]}
        blobs = {"agent_logs/p/1.log": b"first", "agent_logs/p/2.log": b"second"}
        with patch(
            "orchestrator.main.snapshot_service.get_blob",
            new=AsyncMock(side_effect=lambda k: blobs.get(k)),
        ):
            text = await _read_archived_agent_log(meta)
        assert text == "first\nsecond"

    @pytest.mark.asyncio
    async def test_json_string_metadata_is_parsed(self):
        meta = '{"log_archive_keys": ["agent_logs/p/1.log"]}'
        with patch(
            "orchestrator.main.snapshot_service.get_blob",
            new=AsyncMock(return_value=b"content"),
        ):
            assert await _read_archived_agent_log(meta) == "content"

    @pytest.mark.asyncio
    async def test_none_when_no_keys(self):
        assert await _read_archived_agent_log(None) is None
        assert await _read_archived_agent_log({}) is None
        assert await _read_archived_agent_log({"log_archive_keys": []}) is None

    @pytest.mark.asyncio
    async def test_none_when_store_has_nothing(self):
        meta = {"log_archive_keys": ["agent_logs/p/1.log"]}
        with patch(
            "orchestrator.main.snapshot_service.get_blob",
            new=AsyncMock(return_value=None),
        ):
            assert await _read_archived_agent_log(meta) is None


class TestLogFormatters:
    def test_job_log_header_unchanged_for_live_logs(self):
        out = format_job_log(JOB_ID, {"lines": ["x"], "total_lines": 1})
        assert out.startswith(f"Log for job {JOB_ID} — showing 1 of 1 lines")

    def test_archived_flag_surfaces_in_header(self):
        out = format_thread_log(
            JOB_ID, {"lines": ["x"], "total_lines": 1, "archived": True}
        )
        assert "session" in out
        assert "archived" in out
