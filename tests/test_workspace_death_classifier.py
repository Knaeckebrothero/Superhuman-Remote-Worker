"""Unit tests for the workspace-death legibility classifier.

Slice 1 of knowledge-base/knowledge/features/workspace_resource_pressure_resilience.md — turn a
workspace pod's terminated-container facts into a legible cause + an
is-resource-kill escalation signal, so an OOM stops masquerading as an SSH blip.
"""

from orchestrator.services.completion import classify_workspace_death


class TestClassifyWorkspaceDeath:
    def test_oomkilled_is_resource_kill(self):
        reason, is_resource = classify_workspace_death(
            {"container_reason": "OOMKilled", "exit_code": 137}
        )
        assert is_resource is True
        assert "memory" in reason.lower()

    def test_evicted_is_resource_kill(self):
        reason, is_resource = classify_workspace_death(
            {"pod_reason": "Evicted", "container_reason": None}
        )
        assert is_resource is True
        assert "evict" in reason.lower()

    def test_bare_sigkill_137_is_likely_resource(self):
        # "Error" reason (not OOMKilled) but exit 137/SIGKILL on a probe-less
        # workspace is still treated as likely memory pressure.
        reason, is_resource = classify_workspace_death(
            {"container_reason": "Error", "exit_code": 137}
        )
        assert is_resource is True
        assert "137" in reason

    def test_non_resource_error_is_not_resource_kill(self):
        reason, is_resource = classify_workspace_death(
            {"container_reason": "Error", "exit_code": 1}
        )
        assert is_resource is False
        assert "Error" in reason

    def test_missing_termination_never_fabricates_oom(self):
        # Pod gone (NXDOMAIN) before we could read it → no evidence → not a
        # resource kill, so recovery won't wrongly grow a job that didn't OOM.
        reason, is_resource = classify_workspace_death(None)
        assert is_resource is False
        assert "gone" in reason.lower()

    def test_empty_dict_is_not_resource_kill(self):
        reason, is_resource = classify_workspace_death({})
        assert is_resource is False
