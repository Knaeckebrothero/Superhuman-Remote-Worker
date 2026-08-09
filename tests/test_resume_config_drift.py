"""POST /resume reports drifted config as 428 and accepts an acknowledgment.

A session's stored config (connector selection, project mounts, capability
grants) can drift out from under it while it is ended — a connector deleted, a
project membership revoked, a grant withdrawn. Before this, ``resume_thread``
ran the plain authorize-or-403 helpers (``_revalidate_thread_project_ids`` /
``_revalidate_thread_datasource_ids``), so a single drifted item made the
session permanently un-resumable with no way back (the live incident behind
docs/features/session_config_drift_resume.md). Now it reports every drifted
item as 428 and accepts an ``acknowledge`` list naming the ids the caller
accepts losing.

This repo has no HTTP test client for these routes. Mirrors the
patch-the-auth-resolver pattern from tests/test_thread_rename.py: call the
endpoint FUNCTION directly, with the auth resolver and ``postgres_db`` patched
via an ExitStack, reusing the conftest fixtures (``user_a``, ``thread_a``,
``fake_db``, ``fake_request``) rather than inventing new ones.

``_thread_config_drift`` — the real drift assembler that runs the Task 1/2/3/5
classifiers — is exercised on its own elsewhere; here it is patched per test so
each case controls exactly what "currently drifted" means without having to
fake out project/datasource/grant classification end to end.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from orchestrator.services.config_drift import DriftItem, drift_labels

DS_GONE = "d7555d5d-ce46-49e2-b1fa-8235d720badc"


def _patch_caller_and_db(user: dict, db):
    stack = ExitStack()
    stack.enter_context(
        patch("main.require_approved_user", AsyncMock(return_value=user))
    )
    stack.enter_context(
        patch(
            "security.access.require_approved_user",
            AsyncMock(return_value=user),
        )
    )
    stack.enter_context(patch("main.postgres_db", db))
    # Past the drift gate, resume_thread reprovisions an agent and
    # provisions/reuses the session's cloud folder — machinery this file has
    # no interest in exercising. Keep it inert and deterministic rather than
    # depending on whatever services.agent_provisioner detects (or not) about
    # the box the tests happen to run on.
    stack.enter_context(
        patch("main.agent_provisioner", SimpleNamespace(is_available=False))
    )
    stack.enter_context(
        patch("main.persistent_provisioner", SimpleNamespace(is_available=False))
    )
    stack.enter_context(patch("main.ensure_session_workspace", AsyncMock()))
    db.list_thread_mounts = AsyncMock(return_value=[])
    return stack


def _ended_thread(thread: dict) -> dict:
    """``thread_a``/``thread_b``, set to the 'ended' state POST /resume
    requires, with the cloud-folder handles already present so the
    (irrelevant, fire-and-forget) late-provision task never gets scheduled."""
    thread["status"] = "ended"
    thread["metadata"] = {}
    thread["main_cloud_session_handle"] = "existing-handle"
    thread["main_cloud_share_handle"] = "existing-share"
    return thread


def _fake_drift(items: list[DriftItem]) -> AsyncMock:
    """Stand-in for ``_thread_config_drift``: fixed drift for any thread/owner."""
    return AsyncMock(return_value=items)


class TestResumeConfigDrift:
    @pytest.mark.asyncio
    async def test_drift_returns_428_and_does_not_mutate(
        self, user_a, thread_a, fake_db, fake_request
    ):
        from main import resume_thread

        thread = _ended_thread(thread_a)
        drift = [
            DriftItem(f"connector:{DS_GONE}", "connector", "deleted", "KurortEngine")
        ]

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main._thread_config_drift", _fake_drift(drift)):
                with pytest.raises(HTTPException) as exc:
                    await resume_thread(str(thread["id"]), fake_request)

        assert exc.value.status_code == 428
        body = exc.value.detail
        assert body["code"] == "config_drift"
        assert body["detail"] == (
            "Parts of this session's configuration are no longer available"
        )
        assert body["drift"] == [
            {
                "id": f"connector:{DS_GONE}",
                "kind": "connector",
                "reason": "deleted",
                "label": "KurortEngine",
            }
        ]
        assert body["summary"] == drift_labels(drift)
        # Nothing was mutated: no write ever reached the thread.
        fake_db.resume_thread.assert_not_awaited()
        fake_db.record_thread_config_drift_ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_full_acknowledgment_resumes(
        self, user_a, thread_a, fake_db, fake_request
    ):
        from main import ThreadResumeRequest, resume_thread

        thread = _ended_thread(thread_a)
        thread_id = str(thread["id"])
        drift = [
            DriftItem(f"connector:{DS_GONE}", "connector", "deleted", "KurortEngine")
        ]

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main._thread_config_drift", _fake_drift(drift)):
                result = await resume_thread(
                    thread_id,
                    fake_request,
                    ThreadResumeRequest(acknowledge=[f"connector:{DS_GONE}"]),
                )

        assert result == {"status": "created", "thread_id": thread_id}
        fake_db.resume_thread.assert_awaited_once_with(thread_id)
        fake_db.record_thread_config_drift_ack.assert_awaited_once_with(
            thread_id, {f"connector:{DS_GONE}": "deleted"}
        )

    @pytest.mark.asyncio
    async def test_partial_acknowledgment_is_rejected(
        self, user_a, thread_a, fake_db, fake_request
    ):
        from main import ThreadResumeRequest, resume_thread

        thread = _ended_thread(thread_a)
        drift = [
            DriftItem(f"connector:{DS_GONE}", "connector", "deleted", "KurortEngine"),
            DriftItem("grant:shell_tools", "grant", "revoked", "shell tools"),
        ]

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main._thread_config_drift", _fake_drift(drift)):
                with pytest.raises(HTTPException) as exc:
                    await resume_thread(
                        str(thread["id"]),
                        fake_request,
                        ThreadResumeRequest(acknowledge=[f"connector:{DS_GONE}"]),
                    )

        assert exc.value.status_code == 428
        # Only the still-outstanding item is named, not the acked one.
        assert exc.value.detail["drift"] == [
            {
                "id": f"connector:{DS_GONE}",
                "kind": "connector",
                "reason": "deleted",
                "label": "KurortEngine",
            },
            {
                "id": "grant:shell_tools",
                "kind": "grant",
                "reason": "revoked",
                "label": "shell tools",
            },
        ]
        fake_db.resume_thread.assert_not_awaited()
        fake_db.record_thread_config_drift_ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_superset_acknowledgment_is_accepted(
        self, user_a, thread_a, fake_db, fake_request
    ):
        """An item that recovered between prompt and confirm must not force a
        pointless re-prompt: acknowledging it anyway is harmless — the subset
        rule, not equality."""
        from main import ThreadResumeRequest, resume_thread

        thread = _ended_thread(thread_a)
        thread_id = str(thread["id"])
        drift = [DriftItem("grant:shell_tools", "grant", "revoked", "shell")]

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main._thread_config_drift", _fake_drift(drift)):
                result = await resume_thread(
                    thread_id,
                    fake_request,
                    ThreadResumeRequest(
                        acknowledge=["grant:shell_tools", f"connector:{DS_GONE}"]
                    ),
                )

        assert result == {"status": "created", "thread_id": thread_id}
        fake_db.resume_thread.assert_awaited_once_with(thread_id)
        # The stray extra ack id is stored too, harmlessly: only currently
        # drifting items were ever required, but the ack writer's contract is
        # "record what was acknowledged", not "record what mattered".
        fake_db.record_thread_config_drift_ack.assert_awaited_once_with(
            thread_id, {"grant:shell_tools": "revoked"}
        )

    @pytest.mark.asyncio
    async def test_no_drift_resumes_exactly_as_before(
        self, user_a, thread_a, fake_db, fake_request
    ):
        from main import resume_thread

        thread = _ended_thread(thread_a)
        thread_id = str(thread["id"])

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main._thread_config_drift", _fake_drift([])):
                result = await resume_thread(thread_id, fake_request)

        assert result == {"status": "created", "thread_id": thread_id}
        fake_db.resume_thread.assert_awaited_once_with(thread_id)
        # Nothing to acknowledge -> the ack writer is never even called.
        fake_db.record_thread_config_drift_ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_ended_thread_still_409s(
        self, user_a, thread_a, fake_db, fake_request
    ):
        """Pre-existing behaviour, unchanged: a double-click / already-active
        thread still 409s, and now does so BEFORE drift is even computed."""
        from main import resume_thread

        thread_a["status"] = "created"
        drift_probe = _fake_drift([])

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main._thread_config_drift", drift_probe):
                with pytest.raises(HTTPException) as exc:
                    await resume_thread(str(thread_a["id"]), fake_request)

        assert exc.value.status_code == 409
        drift_probe.assert_not_awaited()
        fake_db.resume_thread.assert_not_awaited()
