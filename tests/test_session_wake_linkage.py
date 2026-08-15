"""Session ↔ job linkage and the cross-tenant hole adding it opened.

``jobs.created_by_thread_id`` is what the completion wake routes on, so how the
value gets there is a security boundary, not bookkeeping:

* On the **internal** path the thread id is authenticated —
  ``_resolve_internal_job_creation_scope`` fetches the thread and 403s when it
  is missing or owned by someone else.
* On the **public** path it was never checked at all. That was harmless while
  the value was a datasource-inheritance hint whose lookup failures are
  swallowed. The moment it is persisted and woken on, an unchecked body field
  lets a caller name a victim's live session and have a completion payload
  POSTed into it — ``/api/input`` on the agent pod has no authentication of any
  kind. Hence the strip, and hence these tests.

Design: docs/features/session_wake_on_job_completion.md.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

USER_ID = str(uuid.uuid4())
THREAD_ID = str(uuid.uuid4())
VICTIM_THREAD_ID = str(uuid.uuid4())
JOB_ID = str(uuid.uuid4())


# --------------------------------------------------------------------------
# The strip
# --------------------------------------------------------------------------


class TestPublicPathStripsThreadId:
    def test_thread_id_is_stripped_from_a_public_payload(self):
        from main import JobCreate, _strip_public_job_reserved_markers

        job = JobCreate(description="d", thread_id=VICTIM_THREAD_ID)
        _strip_public_job_reserved_markers(job)

        assert job.thread_id is None, (
            "a cockpit caller must not be able to name a thread — persisting an "
            "unvalidated one turns the wake into cross-tenant message injection"
        )

    def test_thread_id_joins_the_other_system_only_markers(self):
        """It is stripped for the same reason parent_job_id is: derived, never
        submitted."""
        from main import JobCreate, _strip_public_job_reserved_markers

        job = JobCreate(
            description="d",
            thread_id=VICTIM_THREAD_ID,
            parent_job_id=str(uuid.uuid4()),
            creation_order=3,
            worktree_path="/tmp/x",
            delegation_context="ctx",
        )
        _strip_public_job_reserved_markers(job)

        assert (
            job.thread_id,
            job.parent_job_id,
            job.creation_order,
            job.worktree_path,
            job.delegation_context,
        ) == (None, None, None, None, None)


# --------------------------------------------------------------------------
# Persistence through create_job
# --------------------------------------------------------------------------


@pytest.fixture
def fake_request():
    return SimpleNamespace(headers={"X-Internal-Key": "secret"}, query_params={})


@pytest.fixture
def linkage_db():
    db = MagicMock()
    db.get_thread = AsyncMock(
        return_value={"id": THREAD_ID, "user_id": USER_ID, "project_id": None}
    )
    db.get_user = AsyncMock(return_value={"id": USER_ID, "is_admin": False})
    db.get_project = AsyncMock(return_value=None)
    db.create_job = AsyncMock(return_value={"id": JOB_ID, "status": "created"})
    db.get_job = AsyncMock(return_value=None)
    db.get_datasource = AsyncMock(return_value=None)
    db.link_datasource_to_job = AsyncMock()
    return db


def _patched(db):
    """Stub every collaborator create_job reaches after scope resolution.

    Deliberately does NOT stub _resolve_internal_job_creation_scope: that is the
    function doing the authentication the strip complements, so the test would
    lose its point if it were mocked away.
    """
    return [
        patch("main.postgres_db", db),
        patch("main._enforce_readiness_gate", AsyncMock(return_value=None)),
        patch(
            "main._thread_project_ids",
            AsyncMock(
                side_effect=lambda _thread_id: (
                    [str(db.get_thread.return_value["project_id"])]
                    if db.get_thread.return_value.get("project_id")
                    else []
                )
            ),
        ),
        patch(
            "main._revalidate_thread_project_ids",
            AsyncMock(side_effect=lambda _thread, project_ids: project_ids),
        ),
        patch("main._require_job_project_access", AsyncMock(return_value=None)),
        patch("main._is_experts_db_enabled", MagicMock(return_value=False)),
        patch("main._inherit_parent_datasource_ids", AsyncMock(return_value=[])),
        patch("main._authorize_thread_datasource_ids", AsyncMock(return_value=[])),
        patch("main._enforce_job_create_grants", AsyncMock(return_value=None)),
        patch("services.job_provisioning.provision_job_repo", AsyncMock()),
        patch("main._spawn_scholar_subjob", AsyncMock(return_value=None)),
        patch("main._trigger_dispatch", MagicMock()),
    ]


async def _create(db, fake_request, body):
    from contextlib import ExitStack

    import security.access as access_module
    from main import create_job

    with ExitStack() as stack:
        stack.enter_context(patch.object(access_module, "_INTERNAL_KEY", "secret"))
        for p in _patched(db):
            stack.enter_context(p)
        return await create_job(fake_request, body)


class TestLinkagePersistence:
    @pytest.mark.asyncio
    async def test_session_created_job_carries_the_backref_and_opts_into_wake(
        self, linkage_db, fake_request
    ):
        from main import JobCreate

        await _create(
            linkage_db, fake_request, JobCreate(description="d", thread_id=THREAD_ID)
        )

        kwargs = linkage_db.create_job.await_args.kwargs
        assert kwargs["created_by_thread_id"] == THREAD_ID
        # Set server-side, not by the model: an opt-in flag the agent must
        # remember fails silently — it forgets, then never learns the job
        # finished, which is exactly the bug this feature removes.
        assert kwargs["wake_on_complete"] is True

    @pytest.mark.asyncio
    async def test_job_without_a_thread_gets_no_backref(self, linkage_db, fake_request):
        """A cockpit/automation job has nobody to wake."""
        from main import JobCreate

        fake_request.headers = {"X-Internal-Key": "secret", "X-MCP-User-Id": USER_ID}
        parent = str(uuid.uuid4())
        linkage_db.get_job = AsyncMock(
            return_value={"id": parent, "user_id": USER_ID, "project_id": None}
        )

        await _create(
            linkage_db,
            fake_request,
            JobCreate(description="d", parent_job_id=parent),
        )

        kwargs = linkage_db.create_job.await_args.kwargs
        assert kwargs["created_by_thread_id"] is None
        assert kwargs["wake_on_complete"] is False

    @pytest.mark.asyncio
    async def test_worker_child_does_not_inherit_the_backref(
        self, linkage_db, fake_request
    ):
        """A subjob inherits thread scope for datasources but its completion is
        the parent job's business. Waking the session per subjob would turn one
        delegation into a status feed."""
        from main import JobCreate

        parent = str(uuid.uuid4())
        linkage_db.get_job = AsyncMock(
            return_value={"id": parent, "user_id": USER_ID, "project_id": None}
        )

        await _create(
            linkage_db,
            fake_request,
            JobCreate(description="d", thread_id=THREAD_ID, parent_job_id=parent),
        )

        kwargs = linkage_db.create_job.await_args.kwargs
        assert kwargs["created_by_thread_id"] is None
        assert kwargs["wake_on_complete"] is False

    @pytest.mark.asyncio
    async def test_officer_manual_create_uses_authoritative_post_funnel(
        self, linkage_db, fake_request
    ):
        """Manual REST creation and the backlog tick call the same final
        admission-and-INSERT service; the endpoint never performs a second
        unlocked ``create_job`` after checking the slot."""
        from main import JobCreate
        from services.officer_admission import OfficerAdmissionPreparation

        project_id = str(uuid.uuid4())
        linkage_db.get_thread.return_value = {
            "id": THREAD_ID,
            "user_id": USER_ID,
            "project_id": project_id,
            "status": "active",
            "metadata": {
                "config_override": {
                    "officer": {
                        "enabled": True,
                        "slots": {"line": {"count": 1}},
                    }
                }
            },
        }
        linkage_db.get_project.return_value = {
            "id": project_id,
            "default_config_override": None,
            "default_config_name": None,
        }
        linkage_db.get_project_officer_lineage = AsyncMock(return_value=[THREAD_ID])
        preparation = OfficerAdmissionPreparation(
            project_id=project_id,
            thread_id=THREAD_ID,
            requested_slot="line",
            slot_name="line",
            slot_patch={},
            category=None,
            config_fingerprint="proof",
            incarnation=0,
            owner_user_id=USER_ID,
            require_auto_pull=False,
        )
        prepare = AsyncMock(return_value=preparation)
        admit = AsyncMock(return_value={"id": JOB_ID, "status": "created"})

        with (
            patch("services.officer_admission.prepare_officer_admission", prepare),
            patch("services.officer_admission.admit_and_create_job", admit),
        ):
            await _create(
                linkage_db,
                fake_request,
                JobCreate(
                    description="manual officer work",
                    thread_id=THREAD_ID,
                    context={"officer_slot": "line"},
                    ticket="feature-proof",
                ),
            )

        prepare.assert_awaited_once_with(
            linkage_db,
            project_id=project_id,
            thread_id=THREAD_ID,
            requested_slot="line",
        )
        admit.assert_awaited_once()
        assert admit.await_args.kwargs["preparation"] is preparation
        assert admit.await_args.kwargs["ticket_note_id"] == "feature-proof"
        assert (
            admit.await_args.kwargs["job_kwargs"]["created_by_thread_id"] == THREAD_ID
        )
        linkage_db.create_job.assert_not_awaited()
