"""Focused tests for the stateless cloud-sync generation store."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from shared.cloud_sync_generations import (
    CloudSyncRequirement,
    CloudSyncScope,
    EMPTY_BASELINE_SHA256,
    acknowledge_cloud_sync_generation,
    arm_cloud_sync_generations,
    load_cloud_sync_requirements,
)


THREAD_ID = UUID("11111111-2222-4333-8444-555555555555")
LEASE_TOKEN = 47
SCOPE_A = "a" * 64
SCOPE_B = "b" * 64


def _row(
    mount_id: str,
    *,
    generation: int = LEASE_TOKEN,
    acknowledged: int = 0,
    lease_token: int = LEASE_TOKEN,
    workspace_generation: str = "workspace-3",
    scope_sha256: str = SCOPE_A,
) -> dict[str, object]:
    return {
        "mount_id": mount_id,
        "required_generation": generation,
        "acknowledged_generation": acknowledged,
        "required_lease_token": lease_token,
        "workspace_generation": workspace_generation,
        "sync_scope_sha256": scope_sha256,
        "baseline_manifest": {},
        "baseline_sha256": EMPTY_BASELINE_SHA256,
    }


@pytest.mark.asyncio
async def test_load_returns_all_persisted_mounts_without_a_mount_filter():
    conn = AsyncMock()
    conn.fetch.return_value = [
        _row("legacy-session", acknowledged=LEASE_TOKEN),
        _row(
            "removed-project-mount",
            generation=41,
            acknowledged=40,
            lease_token=41,
            workspace_generation="workspace-2",
            scope_sha256=SCOPE_B,
        ),
    ]

    requirements = await load_cloud_sync_requirements(
        conn,
        thread_id=str(THREAD_ID),
        lease_token=LEASE_TOKEN,
        workspace_generation="workspace-3",
    )

    assert set(requirements) == {"legacy-session", "removed-project-mount"}
    assert requirements["removed-project-mount"] == CloudSyncRequirement(
        mount_id="removed-project-mount",
        required_generation=41,
        acknowledged_generation=40,
        required_lease_token=41,
        workspace_generation="workspace-2",
        sync_scope_sha256=SCOPE_B,
    )
    sql, *params = conn.fetch.await_args.args
    assert params == [THREAD_ID, LEASE_TOKEN, "workspace-3"]
    assert "generation' = $3::text" in sql
    assert "ANY(" not in sql
    assert "JOIN owner ON owner.unit_id = generation.thread_id" in sql


@pytest.mark.parametrize(
    "scope",
    [
        CloudSyncScope("", "workspace-3", SCOPE_A),
        CloudSyncScope("legacy-session", "", SCOPE_A),
        CloudSyncScope("legacy-session", "workspace-3", "a" * 63),
        CloudSyncScope("legacy-session", "workspace-3", "A" * 64),
        CloudSyncScope("legacy-session", "workspace-3", ("a" * 63) + "g"),
    ],
    ids=[
        "empty-mount",
        "empty-workspace-generation",
        "short-digest",
        "uppercase-digest",
        "non-hex-digest",
    ],
)
@pytest.mark.asyncio
async def test_arm_rejects_malformed_scope_identity_before_query(scope):
    conn = AsyncMock()

    with pytest.raises(ValueError):
        await arm_cloud_sync_generations(
            conn,
            thread_id=THREAD_ID,
            lease_token=LEASE_TOKEN,
            scopes=[scope],
        )

    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_deduplicates_identical_scopes_and_sorts_query_arrays():
    conn = AsyncMock()
    conn.fetch.return_value = [
        _row("legacy-session"),
        _row(
            "project-mount",
            workspace_generation="workspace-3",
            scope_sha256=SCOPE_B,
        ),
    ]
    legacy = CloudSyncScope("legacy-session", "workspace-3", SCOPE_A)
    project = CloudSyncScope("project-mount", "workspace-3", SCOPE_B)

    result = await arm_cloud_sync_generations(
        conn,
        thread_id=THREAD_ID,
        lease_token=LEASE_TOKEN,
        scopes=[project, legacy, project],
    )

    assert set(result) == {"legacy-session", "project-mount"}
    (
        _sql,
        thread_id,
        token,
        mount_ids,
        workspaces,
        digests,
        manifests,
        baseline_digests,
        binding_generation,
    ) = conn.fetch.await_args.args
    assert thread_id == THREAD_ID
    assert token == LEASE_TOKEN
    assert mount_ids == ["legacy-session", "project-mount"]
    assert workspaces == ["workspace-3", "workspace-3"]
    assert digests == [SCOPE_A, SCOPE_B]
    assert manifests == ["{}", "{}"]
    assert baseline_digests == [EMPTY_BASELINE_SHA256, EMPTY_BASELINE_SHA256]
    assert binding_generation == "workspace-3"


@pytest.mark.asyncio
async def test_arm_rejects_different_workspace_generations_across_mounts():
    conn = AsyncMock()

    with pytest.raises(
        ValueError, match="all cloud sync scopes must share one workspace generation"
    ):
        await arm_cloud_sync_generations(
            conn,
            thread_id=THREAD_ID,
            lease_token=LEASE_TOKEN,
            scopes=[
                CloudSyncScope("legacy-session", "workspace-3", SCOPE_A),
                CloudSyncScope("project-mount", "workspace-4", SCOPE_B),
            ],
        )

    conn.fetch.assert_not_awaited()


@pytest.mark.parametrize(
    "conflicting_scope",
    [
        CloudSyncScope("legacy-session", "workspace-4", SCOPE_A),
        CloudSyncScope("legacy-session", "workspace-3", SCOPE_B),
    ],
    ids=["workspace-generation", "scope-digest"],
)
@pytest.mark.asyncio
async def test_arm_rejects_conflicting_duplicate_mount_scope(conflicting_scope):
    conn = AsyncMock()
    original = CloudSyncScope("legacy-session", "workspace-3", SCOPE_A)

    with pytest.raises(
        ValueError,
        match="conflicting cloud sync scope for mount legacy-session",
    ):
        await arm_cloud_sync_generations(
            conn,
            thread_id=THREAD_ID,
            lease_token=LEASE_TOKEN,
            scopes=[original, conflicting_scope],
        )

    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_uses_queue_token_as_generation_and_lease_token():
    conn = AsyncMock()
    conn.fetch.return_value = [_row("legacy-session")]

    requirements = await arm_cloud_sync_generations(
        conn,
        thread_id=THREAD_ID,
        lease_token=LEASE_TOKEN,
        scopes=[CloudSyncScope("legacy-session", "workspace-3", SCOPE_A)],
    )

    assert requirements["legacy-session"].required_generation == LEASE_TOKEN
    assert requirements["legacy-session"].required_lease_token == LEASE_TOKEN
    sql, thread_id, token, *_scope_arrays = conn.fetch.await_args.args
    assert thread_id == THREAD_ID
    assert token == LEASE_TOKEN
    compact_sql = " ".join(sql.split())
    assert (
        "SELECT owner.unit_id, requested.mount_id, $2::bigint, 0, "
        "$2::bigint" in compact_sql
    )
    assert (
        "acknowledged_generation = "
        "thread_cloud_sync_generations.required_generation" in compact_sql
    )


@pytest.mark.parametrize(
    ("rows", "expected_mounts"),
    [
        ([], set()),
        ([_row("legacy-session")], {"legacy-session"}),
    ],
    ids=["lease-or-all-scopes-rejected", "one-of-two-scopes-rejected"],
)
@pytest.mark.asyncio
async def test_arm_exposes_sql_rejection_as_an_empty_or_partial_result(
    rows,
    expected_mounts,
):
    conn = AsyncMock()
    conn.fetch.return_value = rows

    requirements = await arm_cloud_sync_generations(
        conn,
        thread_id=THREAD_ID,
        lease_token=LEASE_TOKEN,
        scopes=[
            CloudSyncScope("legacy-session", "workspace-3", SCOPE_A),
            CloudSyncScope("project-mount", "workspace-3", SCOPE_B),
        ],
    )

    # The caller compares this returned set with its configured set. Missing
    # rows are the signal that ownership was stale or a predecessor generation
    # was still pending; the helper must never fabricate successful reserves.
    assert set(requirements) == expected_mounts


@pytest.mark.asyncio
async def test_acknowledge_passes_exact_generation_workspace_and_scope_fences():
    conn = AsyncMock()
    conn.fetchval.return_value = LEASE_TOKEN

    acknowledged = await acknowledge_cloud_sync_generation(
        conn,
        thread_id=str(THREAD_ID),
        lease_token=53,
        mount_id="legacy-session",
        generation=LEASE_TOKEN,
        workspace_generation="workspace-3",
        sync_scope_sha256=SCOPE_A,
        baseline_sha256=EMPTY_BASELINE_SHA256,
    )

    assert acknowledged is True
    sql, *params = conn.fetchval.await_args.args
    assert params == [
        THREAD_ID,
        53,
        "legacy-session",
        LEASE_TOKEN,
        "workspace-3",
        SCOPE_A,
        EMPTY_BASELINE_SHA256,
    ]
    compact_sql = " ".join(sql.split())
    assert "generation.required_generation = $4::bigint" in compact_sql
    assert "generation.workspace_generation = $5::text" in compact_sql
    assert "generation.sync_scope_sha256 = $6::text" in compact_sql
    assert "generation.baseline_sha256 = $7::text" in compact_sql


@pytest.mark.asyncio
async def test_acknowledge_returns_false_when_any_fence_rejects_update():
    conn = AsyncMock()
    conn.fetchval.return_value = None

    assert not await acknowledge_cloud_sync_generation(
        conn,
        thread_id=THREAD_ID,
        lease_token=LEASE_TOKEN,
        mount_id="legacy-session",
        generation=LEASE_TOKEN,
        workspace_generation="workspace-3",
        sync_scope_sha256=SCOPE_A,
        baseline_sha256=EMPTY_BASELINE_SHA256,
    )
