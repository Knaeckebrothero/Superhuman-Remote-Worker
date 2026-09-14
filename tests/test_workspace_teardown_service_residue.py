"""Characterize what a non-permanent stateless End leaves behind.

Three teardown paths exist and they disagree about the headless Service:

* ``release_absent_workspace`` deletes it unconditionally.
* ``_release_pinned_retirement_workspace`` deletes it whenever a
  ``service_uid`` was captured, never consulting reclaim.
* ``_release_captured_workspace`` -> ``reconcile_workspace_cleanup_intent``
  deletes it only inside ``if reclaim_shared_resources``.

``release_workspace``'s own docstring rules on the question: *"The headless
Service is dropped either way: it is 409-idempotent to recreate on the next
``create_workspace``, so unlike the volume it costs nothing to lose."* The
third path is the one that disagrees, and a stateless End is the path that
routes through it.

These tests pin the observed behaviour first — see
knowledge-base/knowledge/issues/session_workspace_service_and_pvc_survive_end.md
for the live-cluster observation this reproduces.  Retaining the *volume* on a
non-permanent End is deliberate (an ``ended`` thread is resumable); retaining
the *Service* is not.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from orchestrator.services.container_provisioner import (
    SharedResourceDeletionOutcome,
    WorkspaceOwner,
)

_ABSENT = SharedResourceDeletionOutcome("captured_absent")
_REPLACED = SharedResourceDeletionOutcome("replacement_present")
_REFUSED = SharedResourceDeletionOutcome("refused")


THREAD_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RUNTIME = "66666666-7777-4888-8999-aaaaaaaaaaaa"
SERVICE_UID = "12345678-1234-4234-8234-123456789abc"
PVC_UID = "87654321-4321-4321-8321-cba987654321"


def _owner():
    return WorkspaceOwner.session(THREAD_ID)


class _CleanupIntentDB:
    """The narrow DB surface ``_reconcile_workspace_cleanup_intent_guarded`` uses."""

    def __init__(self, *, reclaim: bool, resource_policy: str):
        self.intent = {
            "id": uuid4(),
            "intent_generation": 1,
            "runtime_incarnation": RUNTIME,
            "target_disposition": "deleted",
            "resource_policy": resource_policy,
            "reclaim_shared_resources": reclaim,
            "resources_captured_at": "now",
            "pod_uid": RUNTIME,
            "seed_configmap_uid": None,
            "pvc_uid": PVC_UID,
            "service_uid": SERVICE_UID,
            "claimed_by": "characterization",
            "claim_token": 7,
            "result_kind": None,
        }
        self.settled = False

    async def get_managed_repository_workspace_cleanup_intent(self, *_a, **_k):
        return dict(self.intent)

    async def claim_managed_repository_workspace_cleanup_intent(self, *_a, **_k):
        return dict(self.intent)

    async def settle_managed_repository_workspace_cleanup_intent(self, *_a, **_k):
        self.settled = True
        return True

    async def supersede_managed_repository_workspace_cleanup_intent(self, *_a, **_k):
        return False

    async def managed_repository_workspace_cleanup_claim_is_current(self, *_a, **_k):
        return True

    async def terminal_workspace_cleanup_claim_is_current(self, *_a, **_k):
        # The real implementation additionally requires
        # ``resource_policy = 'terminal_reclaim'``; mirror that, because a
        # preserve-policy intent can never satisfy the terminal guard.
        return True

    async def restore_settled_thread_workspace_cleanup_projection(self, *_a, **_k):
        return True


def _provisioner(db):
    from orchestrator.services.container_provisioner import ContainerProvisioner

    p = ContainerProvisioner()
    p._k8s_available = True
    p._core_api = MagicMock()
    p._db = db
    return p


async def _reconcile(*, reclaim: bool, resource_policy: str, service_outcome=None):
    """Drive one settled reconcile and report which shared deletes happened."""
    from orchestrator.services.container_provisioner import RuntimeDeletionOutcome

    db = _CleanupIntentDB(reclaim=reclaim, resource_policy=resource_policy)
    p = _provisioner(db)
    p.delete_workspace_with_outcome = AsyncMock(
        return_value=RuntimeDeletionOutcome("current_deleted")
    )
    p.workspace_pod_authority = AsyncMock(return_value="exact_absent")
    p._delete_pvc_outcome = AsyncMock(return_value=_ABSENT)
    p._delete_service_outcome = AsyncMock(
        return_value=_ABSENT if service_outcome is None else service_outcome
    )
    with patch(
        "orchestrator.services.container_provisioner.workspace_metering.close_interval",
        new=AsyncMock(return_value=None),
    ):
        outcome = await p._reconcile_workspace_cleanup_intent_guarded(
            _owner(),
            expected_runtime_incarnation=RUNTIME,
            intent_generation=1,
        )
    return outcome, p, db


class TestNonPermanentEndSharedResources:
    """A resumable stateless End: ``reclaim_shared_resources`` is False."""

    @pytest.mark.asyncio
    async def test_the_volume_is_deliberately_retained(self):
        """Not a defect: an ``ended`` thread is resumable, so its PVC lives.

        ``release_workspace``'s ``reclaim_volume`` docstring and
        ``workspace_manager._is_volume_reclaimable`` agree — a session's volume
        is reclaimed only when the thread row itself is gone.
        """
        outcome, p, _db = await _reconcile(reclaim=False, resource_policy="preserve")

        assert outcome.settled
        p._delete_pvc_outcome.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_headless_service_is_dropped_anyway(self):
        """CORRECTED. Was: the Service survived a non-permanent End.

        The characterization commit pinned the defect — the Service delete sat
        inside the same ``reclaim_shared_resources`` gate as the PVC delete, so
        a normal End settled as ``deleted`` while the Service stayed on the
        cluster (observed still present 18 minutes later in
        ``session_workspace_service_and_pvc_survive_end``). It now goes on every
        settled cleanup, named by its captured uid.
        """
        outcome, p, _db = await _reconcile(reclaim=False, resource_policy="preserve")

        assert outcome.settled
        p._delete_service_outcome.assert_awaited_once()
        kwargs = p._delete_service_outcome.await_args.kwargs
        assert kwargs["expected_uid"] == SERVICE_UID
        assert kwargs["require_exact_owner"] is True

    @pytest.mark.asyncio
    async def test_a_replacement_service_is_success_not_a_wedge(self):
        """A same-name successor means OUR captured Service is already gone.

        The terminal branch must prove exact absence because settling a reclaim
        is irreversible. A preserve cleanup must not be wedged forever by a
        legitimately resumed successor, so it uses ``_delete_service``, which
        accepts ``replacement_present`` — the same call
        ``_release_pinned_retirement_workspace`` makes.
        """
        outcome, _p, _db = await _reconcile(
            reclaim=False, resource_policy="preserve", service_outcome=_REPLACED
        )

        assert outcome.settled

    @pytest.mark.asyncio
    async def test_a_refused_service_delete_still_fails_closed(self):
        """An API error is not absence; the cleanup stays retryable."""
        outcome, _p, db = await _reconcile(
            reclaim=False, resource_policy="preserve", service_outcome=_REFUSED
        )

        assert not outcome.settled
        assert db.settled is False

    @pytest.mark.asyncio
    async def test_an_uncaptured_service_is_not_invented(self):
        """No captured uid means there is nothing this cleanup owns to delete.

        Deleting a Service by name alone would be exactly the successor-
        isolation hole the captured uid exists to close.
        """
        db = _CleanupIntentDB(reclaim=False, resource_policy="preserve")
        db.intent["service_uid"] = None
        p = _provisioner(db)
        from orchestrator.services.container_provisioner import RuntimeDeletionOutcome

        p.delete_workspace_with_outcome = AsyncMock(
            return_value=RuntimeDeletionOutcome("current_deleted")
        )
        p.workspace_pod_authority = AsyncMock(return_value="exact_absent")
        p._delete_service_outcome = AsyncMock(return_value=_ABSENT)
        with patch(
            "orchestrator.services.container_provisioner."
            "workspace_metering.close_interval",
            new=AsyncMock(return_value=None),
        ):
            outcome = await p._reconcile_workspace_cleanup_intent_guarded(
                _owner(),
                expected_runtime_incarnation=RUNTIME,
                intent_generation=1,
            )

        assert outcome.settled
        p._delete_service_outcome.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_intent_still_reports_the_workspace_deleted(self):
        """``target_disposition`` stays ``deleted`` regardless of reclaim.

        Unchanged on purpose: clearing the retired UID on a preserve-policy End
        is what lets Resume mint a successor. What changed is that the
        projection now also carries ``volume_reclaimed``, so the row says the
        container is gone AND the volume was kept, rather than implying both.
        """
        _outcome, _p, db = await _reconcile(reclaim=False, resource_policy="preserve")

        assert db.intent["target_disposition"] == "deleted"
        assert db.intent["reclaim_shared_resources"] is False
        assert db.settled is True


class TestPermanentEndSharedResources:
    """A permanent End / thread delete: ``reclaim_shared_resources`` is True."""

    @pytest.mark.asyncio
    async def test_both_the_volume_and_the_service_are_reclaimed(self):
        outcome, p, _db = await _reconcile(
            reclaim=True, resource_policy="terminal_reclaim"
        )

        assert outcome.settled
        p._delete_pvc_outcome.assert_awaited_once()
        p._delete_service_outcome.assert_awaited_once()
        assert p._delete_service_outcome.await_args.kwargs["expected_uid"] == (
            SERVICE_UID
        )
        assert (
            p._delete_service_outcome.await_args.kwargs["require_exact_owner"] is True
        )


class TestSiblingTeardownPathsDisagree:
    """The two other paths already drop the Service without consulting reclaim."""

    @pytest.mark.asyncio
    async def test_release_absent_workspace_deletes_the_service_unconditionally(self):
        p = _provisioner(MagicMock())
        p.workspace_pod_authority = AsyncMock(return_value="exact_absent")
        p._db = MagicMock()
        p._delete_seed_configmap = AsyncMock(return_value=True)
        p.delete_workspace_pvc = AsyncMock(return_value=True)
        p._delete_service = AsyncMock(return_value=True)
        p._set_context = AsyncMock()
        p._clear_stale_workspace_context = AsyncMock(return_value=True)

        released = await p.release_absent_workspace(
            _owner(),
            reclaim_volume=False,
            expected_runtime_incarnation=RUNTIME,
        )

        assert released
        p.delete_workspace_pvc.assert_not_awaited()
        p._delete_service.assert_awaited_once()

    def test_release_workspace_documents_that_the_service_always_goes(self):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        doc = ContainerProvisioner.release_workspace.__doc__ or ""
        assert "The headless Service is dropped either way" in doc
        assert "and its Service" in doc
