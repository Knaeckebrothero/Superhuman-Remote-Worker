import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.workspace_lifecycle import WorkspaceOwner  # noqa: E402


def test_owner_job_naming_and_labels():
    o = WorkspaceOwner.job("abcdef0123456789")
    assert o.kind == "job"
    assert o.pod_name == "workspace-abcdef012345"      # 12-char id truncation
    assert o.label_key == "srw/job-id"
    assert o.component_label == "workspace"
    assert o.network_tier_kind == "job"


def test_owner_session_naming_and_labels():
    o = WorkspaceOwner.session("abcdef0123456789")
    assert o.pod_name == "ws-thread-abcdef012345"
    assert o.label_key == "srw/thread-id"
    assert o.component_label == "thread-workspace"
    assert o.network_tier_kind == "thread"


def test_owner_is_frozen_hashable():
    o = WorkspaceOwner.session("t1")
    with pytest.raises(Exception):
        o.id = "t2"  # frozen
    assert {o: 1}[WorkspaceOwner.session("t1")] == 1  # hashable by (kind, id)
