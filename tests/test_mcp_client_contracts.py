"""Representative request-body contracts for MCP job/project/connectors."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.mcp.client import AsyncCockpitClient


@pytest.mark.asyncio
async def test_job_creation_forwards_preferred_expert_kickoff_context_and_priority() -> (
    None
):
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "job-1", "status": "created"})

    client = AsyncCockpitClient(
        "http://orchestrator.test", transport=httpx.MockTransport(handler)
    )
    try:
        await client.create_project_job(
            project_id="project-1",
            description="contract job",
            expert_id="expert-1",
            kickoff_message="Begin with the supplied evidence.",
            context={"case": "alpha"},
            priority=8,
            required_deliverables=["output/report.md"],
        )
    finally:
        await client.close()

    assert captured["expert_id"] == "expert-1"
    assert captured["kickoff_message"] == "Begin with the supplied evidence."
    assert captured["context"] == {"case": "alpha"}
    assert captured["priority"] == 8
    assert captured["required_deliverables"] == ["output/report.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ds_type", "fields"),
    [
        (
            "email",
            {
                "credentials": {
                    "imap": {"username": "mailbox", "password": "test-only"}
                },
                "config": {
                    "access": "draft",
                    "folders": ["INBOX"],
                    "drafts_folder": "Drafts",
                },
            },
        ),
        (
            "mcp",
            {
                "connection_url": "https://connector.invalid/mcp",
                "credentials": {"transport": "http", "token": "test-only"},
            },
        ),
        (
            "kubeconfig",
            {
                "credentials": {
                    "files": [
                        {
                            "content": "test-only-kubeconfig",
                            "target_path": "~/.kube/config",
                            "mode": "0600",
                        }
                    ]
                }
            },
        ),
        (
            "kb",
            {
                "connection_url": "https://git.invalid/knowledge.git",
                "config": {"root_path": "notes"},
                "read_only": True,
            },
        ),
    ],
)
async def test_representative_connector_contracts_are_forwarded(
    ds_type: str, fields: dict[str, Any]
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "connector-1", "type": ds_type})

    client = AsyncCockpitClient(
        "http://orchestrator.test", transport=httpx.MockTransport(handler)
    )
    try:
        await client.create_datasource(
            name=f"test-{ds_type}",
            ds_type=ds_type,
            is_global=False,
            **fields,
        )
    finally:
        await client.close()

    assert captured["type"] == ds_type
    assert captured["is_global"] is False
    for key, value in fields.items():
        assert captured[key] == value


@pytest.mark.asyncio
async def test_connector_update_can_publish_and_replace_type_config() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "updated"})

    client = AsyncCockpitClient(
        "http://orchestrator.test", transport=httpx.MockTransport(handler)
    )
    try:
        await client.update_datasource(
            "connector-1",
            config={},
            is_global=True,
            read_only=True,
        )
    finally:
        await client.close()

    assert captured == {"config": {}, "is_global": True, "read_only": True}
