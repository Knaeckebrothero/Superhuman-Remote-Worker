"""E4 — evidence manifest security matrix (officer_supervision_surface §3.3).

The gate: path-traversal, current-revision, and oversize denials, plus the
route-level project authorization on every read. Evidence is a manifest, not
a filesystem browser — nothing in here may hand the caller a path they chose.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from services.job_evidence import (
    DIFF_LINE_LIMIT,
    MAX_IMAGES_PER_JOB,
    READ_PAGE_CHARS,
    TEXT_LIMIT_BYTES,
    build_evidence_manifest,
    find_entry,
    parse_manifest,
    public_manifest,
    read_evidence_entry,
)

JOB_ID = "11111111-2222-3333-4444-555555555555"
HEAD_SHA = "abc123def4567890abc123def4567890abc123de"


def _job(**overrides) -> dict:
    job = {
        "id": JOB_ID,
        "status": "processing",
        "repo_name": "job-repo",
        "branch_name": None,
        "project_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "context": {},
        "freeze_data": None,
    }
    job.update(overrides)
    return job


def _result(evidence=None, summary="did the thing") -> dict:
    freeze: dict = {
        "freeze_type": "job_complete",
        "summary": summary,
        "deliverables": ["output/report.md"],
        "confidence": 0.9,
    }
    if evidence is not None:
        freeze["evidence"] = evidence
    return {"should_stop": True, "goal_achieved": True, "freeze_data": freeze}


def _gitea(files: dict[tuple[str, str], bytes] | None = None, head=HEAD_SHA):
    gitea = SimpleNamespace()
    gitea.is_initialized = True
    gitea.get_branch_head_sha = AsyncMock(return_value=head)

    async def get_file_bytes(repo, path, ref=None):
        return (files or {}).get((path, ref))

    gitea.get_file_bytes = AsyncMock(side_effect=get_file_bytes)
    return gitea


def _db():
    db = SimpleNamespace()
    db.get_job = AsyncMock(return_value=None)
    return db


# ---------------------------------------------------------------------------
# Manifest construction
# ---------------------------------------------------------------------------


class TestManifestBuild:
    @pytest.mark.asyncio
    async def test_server_entries_report_and_gate_stamp(self):
        manifest = await build_evidence_manifest(
            _job(
                context={"deliverable_gate": {"passed": True, "commit_sha": HEAD_SHA}}
            ),
            _result(),
            db=_db(),
            gitea=_gitea(),
        )
        kinds = [entry["kind"] for entry in manifest["entries"]]
        assert kinds == ["completion_report", "deliverable_check"]
        report = manifest["entries"][0]
        assert report["producer"] == "server"
        assert report["byte_size"] > 0
        assert report["sha256"]
        assert json.loads(report["inline_content"])["summary"] == "did the thing"
        assert manifest["source_revision"] == HEAD_SHA

    @pytest.mark.asyncio
    async def test_worker_entry_resolved_measured_and_pinned(self):
        content = b"1 passed in 0.1s\n"
        manifest = await build_evidence_manifest(
            _job(),
            _result(
                evidence=[
                    {
                        "kind": "test_report",
                        "label": "pytest run",
                        "media_type": "text/plain",
                        "source": "output/pytest.txt",
                    }
                ]
            ),
            db=_db(),
            gitea=_gitea(files={("output/pytest.txt", HEAD_SHA): content}),
        )
        entry = manifest["entries"][-1]
        assert entry["kind"] == "test_report"
        assert entry["producer"] == "worker"
        assert entry["availability"] == "available"
        assert entry["byte_size"] == len(content)
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["source"]["revision"] == HEAD_SHA
        assert entry["id"].startswith("ev_")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_path",
        [
            "../secrets.env",
            "/etc/passwd",
            "a/../../b.txt",
            "C:\\windows\\system32",
            "notes\\..\\..\\x",
            "~/.ssh/id_rsa",
            "",
            None,
        ],
    )
    async def test_path_traversal_declarations_are_rejected(self, bad_path):
        manifest = await build_evidence_manifest(
            _job(),
            _result(
                evidence=[
                    {
                        "kind": "test_report",
                        "label": "bad",
                        "media_type": "text/plain",
                        "source": bad_path,
                    }
                ]
            ),
            db=_db(),
            gitea=_gitea(),
        )
        entry = manifest["entries"][-1]
        assert entry["availability"] == "rejected_path"
        # Gitea is never consulted for a rejected path.
        assert entry["byte_size"] is None

    @pytest.mark.asyncio
    async def test_worker_cannot_forge_server_kinds(self):
        manifest = await build_evidence_manifest(
            _job(),
            _result(
                evidence=[
                    {
                        "kind": "completion_report",
                        "label": "forged",
                        "media_type": "application/json",
                        "source": "output/fake.json",
                    }
                ]
            ),
            db=_db(),
            gitea=_gitea(),
        )
        forged = manifest["entries"][-1]
        assert forged["availability"] == "rejected_kind"

    @pytest.mark.asyncio
    async def test_oversize_text_and_diff_line_bounds(self):
        big = b"x" * (TEXT_LIMIT_BYTES + 1)
        long_diff = b"\n".join(b"+line" for _ in range(DIFF_LINE_LIMIT + 1))
        manifest = await build_evidence_manifest(
            _job(),
            _result(
                evidence=[
                    {
                        "kind": "test_report",
                        "label": "huge",
                        "media_type": "text/plain",
                        "source": "output/huge.txt",
                    },
                    {
                        "kind": "change_summary",
                        "label": "long diff",
                        "media_type": "text/x-diff",
                        "source": "output/change.diff",
                    },
                ]
            ),
            db=_db(),
            gitea=_gitea(
                files={
                    ("output/huge.txt", HEAD_SHA): big,
                    ("output/change.diff", HEAD_SHA): long_diff,
                }
            ),
        )
        huge, diff = manifest["entries"][-2:]
        assert huge["availability"] == "oversize"
        assert diff["availability"] == "oversize"
        assert str(DIFF_LINE_LIMIT) in diff["availability_reason"]

    @pytest.mark.asyncio
    async def test_image_cap_is_five_per_job(self):
        files = {
            (f"shots/{i}.png", HEAD_SHA): b"\x89PNG" + bytes([i])
            for i in range(MAX_IMAGES_PER_JOB + 1)
        }
        manifest = await build_evidence_manifest(
            _job(),
            _result(
                evidence=[
                    {
                        "kind": "screenshot",
                        "label": f"shot {i}",
                        "media_type": "image/png",
                        "source": f"shots/{i}.png",
                    }
                    for i in range(MAX_IMAGES_PER_JOB + 1)
                ]
            ),
            db=_db(),
            gitea=_gitea(files=files),
        )
        screenshots = [e for e in manifest["entries"] if e["kind"] == "screenshot"]
        assert len(screenshots) == MAX_IMAGES_PER_JOB + 1
        assert [e["availability"] for e in screenshots].count("available") == (
            MAX_IMAGES_PER_JOB
        )
        assert screenshots[-1]["availability"] == "rejected_image_cap"

    @pytest.mark.asyncio
    async def test_unresolvable_declaration_is_recorded_honestly(self):
        manifest = await build_evidence_manifest(
            _job(),
            _result(
                evidence=[
                    {
                        "kind": "test_report",
                        "label": "ghost",
                        "media_type": "text/plain",
                        "source": "output/never-pushed.txt",
                    }
                ]
            ),
            db=_db(),
            gitea=_gitea(files={}),
        )
        entry = manifest["entries"][-1]
        assert entry["availability"] == "unresolved"
        assert "pinned completion revision" in entry["availability_reason"]


# ---------------------------------------------------------------------------
# Reads — pinned revision, tamper detection, bounds
# ---------------------------------------------------------------------------


def _available_entry(content: bytes, *, kind="test_report", media="text/plain") -> dict:
    return {
        "id": "ev_abc123abc123",
        "kind": kind,
        "label": "entry",
        "media_type": media,
        "byte_size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "source": {
            "type": "job_repo",
            "repo": "job-repo",
            "path": "output/pytest.txt",
            "ref": "main",
            "revision": HEAD_SHA,
        },
        "captured_at": "2026-08-14T12:00:00+00:00",
        "producer": "worker",
        "availability": "available",
    }


class TestEvidenceRead:
    @pytest.mark.asyncio
    async def test_read_resolves_only_the_pinned_revision(self):
        """The branch has moved on — the read must fetch the pinned sha, and
        the caller cannot switch revisions (there is no ref parameter)."""
        old = b"old pinned content"
        gitea = _gitea(
            files={
                ("output/pytest.txt", HEAD_SHA): old,
                ("output/pytest.txt", "newer-sha"): b"NEW head content",
            }
        )
        result = await read_evidence_entry(
            _job(), _available_entry(old), offset=0, gitea=gitea
        )
        assert result["content"] == old.decode()
        requested_refs = [
            call.kwargs.get("ref") or call.args[2]
            if len(call.args) > 2
            else call.kwargs.get("ref")
            for call in gitea.get_file_bytes.await_args_list
        ]
        assert requested_refs == [HEAD_SHA]

    @pytest.mark.asyncio
    async def test_sha_mismatch_refuses_content_as_tampered(self):
        gitea = _gitea(files={("output/pytest.txt", HEAD_SHA): b"rewritten bytes"})
        entry = _available_entry(b"the original bytes")
        result = await read_evidence_entry(_job(), entry, offset=0, gitea=gitea)
        assert "content" not in result
        assert "tampered" in result["note"]

    @pytest.mark.asyncio
    async def test_oversize_entry_is_denied_with_honest_metadata(self):
        entry = _available_entry(b"irrelevant")
        entry["availability"] = "oversize"
        entry["availability_reason"] = "too big"
        gitea = _gitea(files={("output/pytest.txt", HEAD_SHA): b"irrelevant"})
        result = await read_evidence_entry(_job(), entry, offset=0, gitea=gitea)
        assert "content" not in result
        assert "availability=oversize" in result["note"]
        gitea.get_file_bytes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_binary_screenshot_returns_safe_view_not_bytes(self):
        content = b"\x89PNG fake image bytes"
        gitea = _gitea(files={("output/pytest.txt", HEAD_SHA): content})
        entry = _available_entry(content, kind="screenshot", media="image/png")
        result = await read_evidence_entry(_job(), entry, offset=0, gitea=gitea)
        assert "content" not in result
        assert result["view"] == {
            "type": "job_repo_file",
            "path": "output/pytest.txt",
            "ref": HEAD_SHA,
        }

    @pytest.mark.asyncio
    async def test_text_reads_paginate_with_offset(self):
        content = ("A" * READ_PAGE_CHARS + "B" * 10).encode()
        gitea = _gitea(files={("output/pytest.txt", HEAD_SHA): content})
        entry = _available_entry(content)
        first = await read_evidence_entry(_job(), entry, offset=0, gitea=gitea)
        assert len(first["content"]) == READ_PAGE_CHARS
        assert first["truncated"] is True
        second = await read_evidence_entry(
            _job(), entry, offset=READ_PAGE_CHARS, gitea=gitea
        )
        assert second["content"] == "B" * 10
        assert second["truncated"] is False

    @pytest.mark.asyncio
    async def test_inline_entries_read_without_gitea(self):
        entry = {
            "id": "ev_inline000001",
            "kind": "completion_report",
            "label": "report",
            "media_type": "application/json",
            "availability": "available",
            "inline_content": json.dumps({"summary": "done"}),
            "source": {"type": "inline", "revision": HEAD_SHA},
        }
        result = await read_evidence_entry(_job(), entry, offset=0, gitea=None)
        assert json.loads(result["content"]) == {"summary": "done"}
        # inline payloads never leak through the public entry echo
        assert "inline_content" not in result["entry"]

    def test_public_manifest_strips_inline_payloads(self):
        manifest = {
            "recorded_at": "t",
            "entries": [{"id": "ev_1", "inline_content": "secret", "kind": "x"}],
        }
        assert "inline_content" not in public_manifest(manifest)["entries"][0]

    def test_find_entry_only_matches_this_jobs_manifest(self):
        manifest = {"entries": [{"id": "ev_1"}]}
        assert find_entry(manifest, "ev_1") == {"id": "ev_1"}
        assert find_entry(manifest, "ev_2") is None
        assert parse_manifest({"context": "not-json"}) is None


# ---------------------------------------------------------------------------
# Route authorization — (caller project, job project, evidence job)
# ---------------------------------------------------------------------------


def _patch_caller_and_db(user: dict, db):
    stack = ExitStack()
    stack.enter_context(
        patch("main.require_approved_user", AsyncMock(return_value=user))
    )
    stack.enter_context(
        patch("security.access.require_approved_user", AsyncMock(return_value=user))
    )
    stack.enter_context(patch("main.postgres_db", db))
    return stack


def _scoped(user: dict, scope: str) -> dict:
    out = dict(user)
    out["scopes"] = [scope]
    out["auth_method"] = "mcp"
    return out


class TestEvidenceRouteAuthorization:
    @pytest.mark.asyncio
    async def test_cross_project_scope_denied_even_with_valid_id(
        self, user_a, fake_db, fake_request, job_a, project_b
    ):
        """A guessed/leaked evidence ID from another project is denied by the
        server scope gate before the manifest is even parsed."""
        from main import read_job_evidence_route

        job_a["context"] = {
            "evidence_manifest": {"recorded_at": "t", "entries": [{"id": "ev_1"}]}
        }
        scoped_user = _scoped(user_a, f"project:{project_b['id']}")
        with _patch_caller_and_db(scoped_user, fake_db):
            with pytest.raises(HTTPException) as excinfo:
                await read_job_evidence_route(
                    fake_request, str(job_a["id"]), "ev_1", offset=0
                )
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_non_member_denied_listing(
        self, user_b, fake_db, fake_request, job_a
    ):
        from main import list_job_evidence_route

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as excinfo:
                await list_job_evidence_route(fake_request, str(job_a["id"]))
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_owner_reads_manifest_and_unknown_id_404s(
        self, user_a, fake_db, fake_request, job_a
    ):
        from main import list_job_evidence_route, read_job_evidence_route

        job_a["context"] = {
            "evidence_manifest": {
                "recorded_at": "t",
                "job_id": str(job_a["id"]),
                "entries": [
                    {
                        "id": "ev_1",
                        "kind": "completion_report",
                        "availability": "available",
                        "inline_content": "{}",
                    }
                ],
            }
        }
        with _patch_caller_and_db(user_a, fake_db):
            listing = await list_job_evidence_route(fake_request, str(job_a["id"]))
            assert [e["id"] for e in listing["entries"]] == ["ev_1"]
            assert "inline_content" not in listing["entries"][0]
            with pytest.raises(HTTPException) as excinfo:
                await read_job_evidence_route(
                    fake_request, str(job_a["id"]), "ev_does_not_exist", offset=0
                )
        assert excinfo.value.status_code == 404

    @pytest.mark.asyncio
    async def test_completion_report_route_404_when_absent(
        self, user_a, fake_db, fake_request, job_a
    ):
        from main import get_job_completion_report_route

        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as excinfo:
                await get_job_completion_report_route(fake_request, str(job_a["id"]))
        assert excinfo.value.status_code == 404
