"""Re-seed agent-authored files after a workspace pod is recreated under a
live job.

Root cause (knowledge-base/knowledge/issues/reviewing_parent_pod_reaped_under_critic.md Issue 4,
memory srw_p4_unseeded_workspace_root): a sandbox pod is provisioned WITH its
git clone (instructions.md, tools/), then the agent writes task_brief.md and
bound skills on top via RemoteBackend. If the pod is torn down and
re-provisioned mid-run, the new pod has the clone again but NOT the
agent-written files — the agent reconnects (SSH transport died) and reads a
workspace missing task_brief.md / skills/todo-guide/SKILL.md.

Fix: (1) RemoteBackend fires an on_reconnect hook after a genuine reconnect;
(2) the hook idempotently re-writes the agent-authored seed files that are
absent, without clobbering anything the agent has since produced.
"""

from shared.runtime.core.backends.remote import RemoteBackend  # noqa: E402
from agent.core.backends.seed import reseed_missing_files  # noqa: E402
from tests._fs_backend import FilesystemTestBackend  # noqa: E402


def test_ensure_connected_fires_hook_on_reconnect_only():
    """The reconnect hook fires exactly once, on an actual reconnect — not when
    the connection is already live (no pod swap → nothing to re-seed)."""
    backend = RemoteBackend(host="host", workspace_path="/ws", job_id="job1234")

    state = {"connected": False}
    backend.is_connected = lambda: state["connected"]

    def fake_connect():
        state["connected"] = True

    backend.connect = fake_connect

    fired = []
    backend.set_reconnect_hook(lambda: fired.append(1))

    backend._ensure_connected()  # dead -> reconnect -> hook fires
    backend._ensure_connected()  # already live -> no reconnect -> no hook

    assert fired == [1]


def test_reconnect_hook_does_not_recurse_when_connection_flaps():
    """The hook re-writes files THROUGH the backend, which re-enters
    _ensure_connected. If the connection is still reported dead (flapping), the
    re-entrancy guard must stop the hook firing again — otherwise it recurses
    without bound."""
    backend = RemoteBackend(host="host", workspace_path="/ws", job_id="job1234")
    backend.is_connected = lambda: False  # never reports live -> forces the guard
    backend.connect = lambda: None

    fired = []

    def hook():
        fired.append(1)
        backend._ensure_connected()  # simulates the hook's file writes

    backend.set_reconnect_hook(hook)

    backend._ensure_connected()

    assert fired == [1]


def test_reseed_missing_files_writes_only_absent_without_clobbering(tmp_path):
    """Idempotent re-seed: write the agent-authored files that are gone (the
    pod was re-provisioned without them), but never overwrite one that survived
    — the agent may have edited it since."""
    backend = FilesystemTestBackend(tmp_path)
    backend.write_file("task_brief.md", "ORIGINAL")  # survived the re-provision

    written = reseed_missing_files(
        backend,
        {
            "task_brief.md": "REGENERATED",
            "skills/todo-guide/SKILL.md": "guide body",
        },
    )

    assert written == ["skills/todo-guide/SKILL.md"]
    assert backend.read_file("task_brief.md") == "ORIGINAL"  # NOT clobbered
    # nested parent dir created + file written
    assert backend.read_file("skills/todo-guide/SKILL.md") == "guide body"
