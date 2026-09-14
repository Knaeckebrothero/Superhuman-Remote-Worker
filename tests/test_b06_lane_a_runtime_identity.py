"""Characterization of the pinned-attach runtime identity predicates.

R1.B06 lane A. These four predicates had **no** direct coverage before the
extraction; this file pins their exact current behaviour so the move is
provably behaviour-preserving, and so the fail-open/fail-closed asymmetry in
``agent_sha_is_current`` cannot be "tidied up" later by accident.
"""

import pytest

from orchestrator.services.session_runtime_identity import (
    agent_sha_is_current,
    expected_agent_shas,
    thread_accepts_runtime,
    thread_uses_pinned_execution,
)


class TestExpectedAgentShas:
    def test_reads_both_image_vars_and_strips_the_sha_prefix(self, monkeypatch):
        monkeypatch.setenv("AGENT_IMAGE", "ghcr.io/x/agent:sha-aaaa111")
        monkeypatch.setenv("PERSISTENT_AGENT_IMAGE", "ghcr.io/x/persist:sha-bbbb222")
        assert expected_agent_shas() == {"aaaa111", "bbbb222"}

    def test_a_tag_without_the_sha_marker_contributes_nothing(self, monkeypatch):
        monkeypatch.setenv("AGENT_IMAGE", "ghcr.io/x/agent:latest")
        monkeypatch.delenv("PERSISTENT_AGENT_IMAGE", raising=False)
        assert expected_agent_shas() == set()

    def test_the_last_marker_wins_so_a_registry_port_cannot_poison_it(
        self, monkeypatch
    ):
        monkeypatch.setenv("AGENT_IMAGE", "reg:5000/x/agent:sha-cccc333")
        monkeypatch.delenv("PERSISTENT_AGENT_IMAGE", raising=False)
        assert expected_agent_shas() == {"cccc333"}

    def test_the_environment_is_read_per_call_not_captured_at_import(self, monkeypatch):
        monkeypatch.setenv("AGENT_IMAGE", "ghcr.io/x/agent:sha-first")
        monkeypatch.delenv("PERSISTENT_AGENT_IMAGE", raising=False)
        assert expected_agent_shas() == {"first"}
        monkeypatch.setenv("AGENT_IMAGE", "ghcr.io/x/agent:sha-second")
        assert expected_agent_shas() == {"second"}


class TestAgentShaIsCurrent:
    @pytest.fixture(autouse=True)
    def _no_ambient_images(self, monkeypatch):
        monkeypatch.delenv("AGENT_IMAGE", raising=False)
        monkeypatch.delenv("PERSISTENT_AGENT_IMAGE", raising=False)

    @pytest.mark.parametrize("metadata", [None, {}, {"build_sha": "anything"}])
    def test_no_sha_tagged_image_configured_fails_open(self, metadata):
        """Local dev has no SHA-tagged image; the check is skipped entirely."""
        assert agent_sha_is_current(metadata) is True

    @pytest.mark.parametrize("metadata", [None, {}, {"build_sha": ""}])
    def test_unknown_build_sha_fails_closed_once_an_expectation_exists(
        self, metadata, monkeypatch
    ):
        monkeypatch.setenv("AGENT_IMAGE", "ghcr.io/x/agent:sha-aaaa111")
        assert agent_sha_is_current(metadata) is False

    def test_a_matching_sha_is_current(self, monkeypatch):
        monkeypatch.setenv("AGENT_IMAGE", "ghcr.io/x/agent:sha-aaaa111")
        assert agent_sha_is_current({"build_sha": "aaaa111"}) is True

    def test_a_mismatched_sha_is_stale(self, monkeypatch):
        monkeypatch.setenv("AGENT_IMAGE", "ghcr.io/x/agent:sha-aaaa111")
        assert agent_sha_is_current({"build_sha": "zzzz999"}) is False

    def test_either_configured_image_satisfies_the_check(self, monkeypatch):
        monkeypatch.setenv("AGENT_IMAGE", "ghcr.io/x/agent:sha-aaaa111")
        monkeypatch.setenv("PERSISTENT_AGENT_IMAGE", "ghcr.io/x/persist:sha-bbbb222")
        assert agent_sha_is_current({"build_sha": "bbbb222"}) is True


class TestThreadUsesPinnedExecution:
    def test_the_exact_pinned_lane_is_the_only_admitted_value(self):
        from shared.run_queue import LANE_PINNED

        assert thread_uses_pinned_execution({"execution_lane": LANE_PINNED}) is True

    @pytest.mark.parametrize(
        "thread",
        [
            None,
            {},
            {"execution_lane": None},
            {"execution_lane": ""},
            {"execution_lane": "stateless"},
            {"execution_lane": "PINNED"},
            {"execution_lane": " pinned"},
            {"execution_lane": "pinned_v2"},
        ],
        ids=[
            "no-row",
            "no-lane-key",
            "null-lane",
            "empty-lane",
            "other-lane",
            "wrong-case",
            "padded",
            "future-value",
        ],
    )
    def test_everything_else_is_refused_including_future_values(self, thread):
        assert thread_uses_pinned_execution(thread) is False


class TestThreadAcceptsRuntime:
    @pytest.mark.parametrize(
        "status", ["created", "active", "awaiting_user", "suspended"]
    )
    def test_the_four_preparable_statuses_are_admitted(self, status):
        assert (
            thread_accepts_runtime({"status": status, "runtime_retirement_token": None})
            is True
        )

    @pytest.mark.parametrize("status", ["ended", "ending", "", "archived"])
    def test_a_non_preparable_status_is_refused(self, status):
        assert (
            thread_accepts_runtime({"status": status, "runtime_retirement_token": None})
            is False
        )

    def test_a_retirement_token_refuses_an_otherwise_preparable_row(self):
        assert (
            thread_accepts_runtime(
                {"status": "active", "runtime_retirement_token": "t"}
            )
            is False
        )

    def test_it_is_only_the_lifecycle_half_and_ignores_the_lane(self):
        """The lane is a separate question; this predicate must not answer it."""
        row = {
            "status": "active",
            "runtime_retirement_token": None,
            "execution_lane": "stateless",
        }
        assert thread_accepts_runtime(row) is True
        assert thread_uses_pinned_execution(row) is False
