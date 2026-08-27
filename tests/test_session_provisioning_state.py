from datetime import datetime, timedelta, timezone

from orchestrator.services.session_provisioning_state import (
    agent_pod_provisioning_in_progress,
)


def test_agent_pod_marker_is_active_only_within_ttl():
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    thread = {
        "agent_id": None,
        "metadata": {
            "agent_pod": {
                "status": "created",
                "pod_name": "srw-agent-s-existing",
                "created_at": now.isoformat(),
            }
        },
    }

    assert agent_pod_provisioning_in_progress(thread, now=now, ttl_s=300)

    later = now + timedelta(seconds=301)
    assert not agent_pod_provisioning_in_progress(thread, now=later, ttl_s=300)


def test_agent_pod_marker_ignores_terminal_or_legacy_metadata():
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    failed = {
        "agent_id": None,
        "metadata": {
            "agent_pod": {
                "status": "failed",
                "pod_name": "srw-agent-s-old",
                "created_at": now.isoformat(),
            }
        },
    }
    legacy = {
        "agent_id": None,
        "metadata": {
            "agent_pod": {
                "status": "created",
                "pod_name": "srw-agent-s-old",
            }
        },
    }

    assert not agent_pod_provisioning_in_progress(failed, now=now, ttl_s=300)
    assert not agent_pod_provisioning_in_progress(legacy, now=now, ttl_s=300)
