"""Unit contract for the bounded legacy Kubernetes adoption bridge."""

from __future__ import annotations

import pytest

from orchestrator.database.postgres import (
    _LEGACY_STATELESS_RUNTIME_CREATION_KEY,
    _STATELESS_RUNTIME_CREATION_KEY,
    _persisted_creation_marker,
    _stateless_runtime_creation_marker,
)
from services.session_workspace_adoption import (
    legacy_k8s_thread_runtime_adoption_candidate,
)

GENERATION = "11111111-2222-4333-8444-555555555555"
POD_UID = "22222222-3333-4444-8555-666666666666"


def _marker(**changes):
    value = {
        "generation": GENERATION,
        "mode": "create",
        "attempted": True,
        "replaces_uid": None,
    }
    value.update(changes)
    return value


class TestHistoricalCreationMarker:
    def test_absent_marker_is_absence(self):
        assert _stateless_runtime_creation_marker({}) is None

    def test_current_key_parses_as_current_authority(self):
        parsed = _stateless_runtime_creation_marker(
            {_STATELESS_RUNTIME_CREATION_KEY: _marker()}
        )
        assert parsed["legacy"] is False
        assert parsed["generation"] == GENERATION

    def test_historical_key_is_refused_by_default(self):
        # Invisible would be the dangerous outcome: a reader that cannot see
        # this marker concludes no create was attempted and makes a second Pod.
        with pytest.raises(RuntimeError, match="predates runtime authority"):
            _stateless_runtime_creation_marker(
                {_LEGACY_STATELESS_RUNTIME_CREATION_KEY: _marker()}
            )

    def test_historical_key_is_readable_only_by_the_adoption_bridge(self):
        parsed = _stateless_runtime_creation_marker(
            {_LEGACY_STATELESS_RUNTIME_CREATION_KEY: _marker()}, allow_legacy=True
        )
        assert parsed["legacy"] is True
        assert parsed["attempted"] is True

    @pytest.mark.parametrize("allow_legacy", (False, True))
    def test_both_keys_at_once_is_contradictory(self, allow_legacy):
        with pytest.raises(RuntimeError, match="contradictory"):
            _stateless_runtime_creation_marker(
                {
                    _STATELESS_RUNTIME_CREATION_KEY: _marker(),
                    _LEGACY_STATELESS_RUNTIME_CREATION_KEY: _marker(),
                },
                allow_legacy=allow_legacy,
            )

    def test_historical_key_is_validated_as_strictly_as_the_current_one(self):
        with pytest.raises(RuntimeError, match="mode is malformed"):
            _stateless_runtime_creation_marker(
                {_LEGACY_STATELESS_RUNTIME_CREATION_KEY: _marker(mode="reattach")},
                allow_legacy=True,
            )

    def test_the_reader_annotation_is_never_persisted(self):
        parsed = _stateless_runtime_creation_marker(
            {_STATELESS_RUNTIME_CREATION_KEY: _marker()}
        )
        assert "legacy" not in _persisted_creation_marker(parsed)
        assert _persisted_creation_marker(parsed) == _marker()


def _thread(**changes):
    value = {
        "id": "33333333-4444-4555-8666-777777777777",
        "status": "active",
        "execution_lane": "stateless",
        "runtime_generation": GENERATION,
        "metadata": {
            "workspace_container": {
                "status": "ready",
                "provisioner": "k8s",
                "host": "ws-thread.internal",
                "pod_ip": "10.42.3.21",
                "port": 30022,
            }
        },
    }
    value.update(changes)
    return value


def _with_workspace(**changes):
    thread = _thread()
    thread["metadata"]["workspace_container"].update(changes)
    return thread


class TestSessionAdoptionCandidate:
    def test_the_exact_historical_shape_is_a_candidate(self):
        assert legacy_k8s_thread_runtime_adoption_candidate(_thread())

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"execution_lane": "pinned"}, id="pinned-lane"),
            pytest.param({"status": "ended"}, id="terminal-owner"),
            pytest.param({"runtime_generation": None}, id="no-generation"),
        ],
    )
    def test_a_foreign_owner_shape_is_not_a_candidate(self, changes):
        assert not legacy_k8s_thread_runtime_adoption_candidate(_thread(**changes))

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"status": "deleted"}, id="not-ready"),
            pytest.param({"provisioner": "docker"}, id="foreign-provisioner"),
            pytest.param({"host": None, "pod_ip": None}, id="no-endpoint"),
            pytest.param({"_runtime_incarnation": POD_UID}, id="already-authoritative"),
            pytest.param(
                {"_runtime_incarnation": "not-a-uuid"}, id="malformed-incarnation"
            ),
            pytest.param(
                {"_creation_reservation_id": "r"}, id="post-tranche-reservation"
            ),
            pytest.param({"_creation_claim_token": "7"}, id="post-tranche-claim"),
        ],
    )
    def test_a_non_historical_projection_is_not_a_candidate(self, changes):
        assert not legacy_k8s_thread_runtime_adoption_candidate(
            _with_workspace(**changes)
        )

    def test_both_creation_marker_names_at_once_is_not_a_candidate(self):
        assert not legacy_k8s_thread_runtime_adoption_candidate(
            _with_workspace(
                **{
                    _STATELESS_RUNTIME_CREATION_KEY: _marker(),
                    _LEGACY_STATELESS_RUNTIME_CREATION_KEY: _marker(),
                }
            )
        )

    @pytest.mark.parametrize(
        "marker",
        [
            "_stateless_workspace_retirement_pending",
            "_stateless_claim_retirement",
            "_stateless_claim_loss_hold",
            "_stateless_claim_losses",
        ],
    )
    def test_a_session_on_its_way_out_is_not_a_candidate(self, marker):
        thread = _thread()
        thread["metadata"][marker] = True
        assert not legacy_k8s_thread_runtime_adoption_candidate(thread)

    def test_a_protected_cloud_session_is_not_a_candidate(self):
        thread = _thread()
        thread["metadata"]["protected_cloud"] = True
        assert not legacy_k8s_thread_runtime_adoption_candidate(thread)
