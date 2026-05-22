"""Verify the agent's register payload includes pod_uid from env.

The Kubernetes downward API in the agent pod manifest exports
``metadata.uid`` as the ``POD_UID`` env var. ``OrchestratorClient.register``
must lift this from os.environ and include it in the JSON body so the
orchestrator can persist it on the agents row (used by the session router
to set K8s ownerReferences on per-session Service/Ingress resources).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.orchestrator_client import OrchestratorClient


@pytest.mark.asyncio
async def test_register_payload_includes_pod_uid_from_env(monkeypatch):
    monkeypatch.setenv("POD_UID", "k8s-pod-uid-deadbeef")

    client = OrchestratorClient(
        orchestrator_url="http://test-orch:8085",
        pod_ip="10.0.0.5",
        pod_port=8001,
        hostname="srw-agent-test",
        config_name="defaults",
        pid=1234,
    )

    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"agent_id": "a-1", "heartbeat_interval_seconds": 60},
        )
    )
    client._client = fake_http

    ok = await client.register(agent_mode="worker")

    assert ok is True
    # httpx is invoked as client.post(url, json=payload); read kwargs.
    payload = fake_http.post.call_args.kwargs["json"]
    assert payload["pod_uid"] == "k8s-pod-uid-deadbeef"


@pytest.mark.asyncio
async def test_register_payload_pod_uid_empty_when_env_unset(monkeypatch):
    """Outside K8s (local dev), POD_UID is unset; the field is an empty
    string rather than missing, so the orchestrator's pydantic model can
    treat it uniformly. The DB layer treats empty string as NULL on insert.
    """
    monkeypatch.delenv("POD_UID", raising=False)

    client = OrchestratorClient(
        orchestrator_url="http://test-orch:8085",
        pod_ip="10.0.0.5",
        pod_port=8001,
        hostname="srw-agent-test",
        config_name="defaults",
        pid=1234,
    )

    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"agent_id": "a-1", "heartbeat_interval_seconds": 60},
        )
    )
    client._client = fake_http

    ok = await client.register(agent_mode="worker")

    assert ok is True
    payload = fake_http.post.call_args.kwargs["json"]
    assert payload["pod_uid"] == ""
