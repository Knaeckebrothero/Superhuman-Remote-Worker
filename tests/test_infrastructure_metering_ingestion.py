from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.ingestion import (
    InfrastructureIngestionService,
    dispatch_ingestion_request,
    run_inventory_generation_loop,
)
from orchestrator.services.infrastructure_metering.ingestion_http import (
    IngestionRequestError,
)
from orchestrator.services.infrastructure_metering.ingestion_types import (
    InventoryItemWire,
    InventorySnapshotFinalize,
    InventoryTicketRequest,
    InventoryWatchApply,
    InventoryWatchFinish,
)
from orchestrator.services.infrastructure_metering.collectors.contracts import (
    normalized_payload,
)
from orchestrator.services.infrastructure_metering.collectors.pod_normalization import (
    normalize_pod,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryFenceError,
    InventoryRecoveryRequired,
    InventoryScopeIdentity,
    TransportNonceClaim,
    WatchEventKind,
    WatchMutationAction,
)


def _settings(*, shadow_enabled: bool) -> InfrastructureMeteringSettings:
    return InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=shadow_enabled,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )


def _finalization(*, shadow_enabled: bool) -> InventorySnapshotFinalize:
    return InventorySnapshotFinalize.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "snapshot_id": uuid4(),
            "scope": {
                "source_cluster": "dev-cluster",
                "api_resource": "core/v1/pods",
                "namespace": "srw",
                "cluster_scoped": False,
            },
            "shadow_enabled": shadow_enabled,
            "collection_completed_at": datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
            "complete": False,
            "resource_version": None,
            "item_count": 0,
            "item_digest": None,
            "fatal_errors": [],
        }
    )


def _meterable_item() -> dict:
    pod = normalize_pod(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "worker",
                "namespace": "srw",
                "uid": "pod-uid",
                "resourceVersion": "17",
                "creationTimestamp": "2026-08-05T11:59:00Z",
                "labels": {"srw/job-id": "job-1"},
            },
            "spec": {
                "nodeName": "node-a",
                "containers": [
                    {
                        "name": "agent",
                        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}},
                    }
                ],
            },
            "status": {"phase": "Running", "startTime": "2026-08-05T12:00:00Z"},
        }
    )
    return {
        "scope": {
            "source_cluster": "dev-cluster",
            "api_resource": "core/v1/pods",
            "namespace": "srw",
            "cluster_scoped": False,
        },
        "snapshot_id": uuid4(),
        "kind": "pod",
        "uid": pod.uid,
        "revision_hash": pod.revision_hash,
        "valid_for_metering": True,
        "normalized": normalized_payload(pod),
    }


def test_server_accepts_only_the_allowlisted_normalized_pod_projection():
    valid = InventoryItemWire.model_validate(_meterable_item())
    projected = InfrastructureIngestionService._inventory_item(valid)
    assert projected.source_uid == "pod-uid"
    assert projected.valid_for_metering

    injections = (
        lambda payload: payload["normalized"].update(
            {"image": "registry.invalid/private-image"}
        ),
        lambda payload: payload["normalized"]["request_evidence"]["admitted_requests"][
            "containers"
        ][0].update({"command": ["leak-me"]}),
        lambda payload: payload["normalized"]["labels"].update(
            {"private-token": "leak-me"}
        ),
    )
    for inject in injections:
        payload = _meterable_item()
        inject(payload)
        wire = InventoryItemWire.model_validate(payload)
        with pytest.raises(IngestionRequestError, match="payload is invalid") as exc:
            InfrastructureIngestionService._inventory_item(wire)
        assert exc.value.status_code == 400


def test_server_binds_normalized_pod_identity_validity_and_revision():
    payload = _meterable_item()
    payload["normalized"]["namespace"] = "other"
    wire = InventoryItemWire.model_validate(payload)
    with pytest.raises(IngestionRequestError, match="identity mismatch"):
        InfrastructureIngestionService._inventory_item(wire)


@pytest.mark.asyncio
async def test_snapshot_finalize_rejects_mixed_shadow_rollout_before_mutation():
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _settings(shadow_enabled=True)
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            _finalization(shadow_enabled=False),
            SimpleNamespace(collector_id="kubernetes-pods"),
        )
    )

    with pytest.raises(IngestionRequestError, match="shadow mode") as raised:
        await service.snapshot_finalize(SimpleNamespace())  # type: ignore[arg-type]

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_dispatch_is_unavailable_without_service_and_sanitizes_fences():
    with pytest.raises(IngestionRequestError, match="unavailable") as unavailable:
        await dispatch_ingestion_request(None, "ticket", SimpleNamespace())  # type: ignore[arg-type]
    assert unavailable.value.status_code == 503

    service = SimpleNamespace(
        ticket=AsyncMock(side_effect=InventoryFenceError("secret cursor detail"))
    )
    with pytest.raises(IngestionRequestError, match="generation conflict") as fenced:
        await dispatch_ingestion_request(  # type: ignore[arg-type]
            service, "ticket", SimpleNamespace()
        )
    assert fenced.value.status_code == 409
    assert "secret cursor detail" not in str(fenced.value)

    service.ticket = AsyncMock(
        side_effect=InventoryRecoveryRequired("private abandoned session detail")
    )
    with pytest.raises(IngestionRequestError, match="recovery required") as recovery:
        await dispatch_ingestion_request(  # type: ignore[arg-type]
            service, "ticket", SimpleNamespace()
        )
    assert recovery.value.status_code == 409
    assert "private abandoned session detail" not in str(recovery.value)


@pytest.mark.asyncio
async def test_snapshot_ticket_rechecks_continuity_at_store_admission():
    scope = InventoryScopeIdentity(
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        api_resource="core/v1/pods",
        namespace="srw",
    )
    epoch_id = uuid4()
    model = InventoryTicketRequest.model_validate(
        {
            "scope": {
                "source_cluster": scope.source_cluster,
                "api_resource": scope.api_resource,
                "namespace": scope.namespace,
                "cluster_scoped": False,
            },
            "intent": "snapshot",
            "snapshot_id": uuid4(),
        }
    )
    claim = TransportNonceClaim(
        collector_id=scope.collector_id,
        request_nonce=uuid4(),
        request_kind="snapshot-ticket",
        request_digest="9" * 64,
    )
    grant = SimpleNamespace(
        id=uuid4(),
        token="t" * 32,
        leader_generation=7,
        expires_at=datetime(2026, 8, 5, 13, tzinfo=timezone.utc),
    )
    store = SimpleNamespace(issue_ingest_ticket=AsyncMock(return_value=grant))
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _settings(shadow_enabled=True)
    service.store = store
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id=scope.collector_id,
                body=b"{}",
                transport_claim=claim,
            ),
        )
    )
    service._scope = lambda *_args: scope  # type: ignore[method-assign]
    service._ensure_scope_epoch = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": epoch_id,
            "continuity_health": "healthy",
            "last_resource_version": "rv-list",
        }
    )

    response = await service.ticket(SimpleNamespace())  # type: ignore[arg-type]

    assert response["ticket_id"] == str(grant.id)
    store.issue_ingest_ticket.assert_awaited_once()
    assert (
        store.issue_ingest_ticket.await_args.kwargs["require_healthy_continuity"]
        is True
    )


@pytest.mark.asyncio
async def test_watch_finish_forwards_typed_ambiguous_gap_to_store():
    event_id = uuid4()
    model = InventoryWatchFinish.model_validate_json(
        json.dumps(
            {
                "ticket_id": str(uuid4()),
                "ticket_token": "t" * 32,
                "leader_generation": 7,
                "scope": {
                    "source_cluster": "dev-cluster",
                    "api_resource": "core/v1/pods",
                    "namespace": "srw",
                    "cluster_scoped": False,
                },
                "started_at": "2026-08-05T12:00:00Z",
                "completed_at": "2026-08-05T12:00:01Z",
                "starting_resource_version": "17",
                "committed_resource_version": "17",
                "processed_events": 0,
                "object_events": 0,
                "bookmarks": 0,
                "bytes_read": 10,
                "reconnect_required": True,
                "relist_required": True,
                "history_lost": True,
                "limit_reached": False,
                "gap_reason": "ambiguous-watch-apply",
                "ambiguous_resource_version": "18",
                "history_event_id": str(event_id),
                "fatal_errors": [],
                "item_errors": [],
            }
        )
    )
    claim = TransportNonceClaim(
        collector_id="kubernetes-pods",
        request_nonce="n" * 32,
        request_kind="watch-finish",
        request_digest="a" * 64,
    )
    store = SimpleNamespace(
        record_watch_gap=AsyncMock(
            return_value=SimpleNamespace(coverage_gap_id=uuid4(), replayed=False)
        )
    )
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _settings(shadow_enabled=True)
    service.store = store
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubernetes-pods",
                transport_claim=claim,
            ),
        )
    )
    service._require_generation = AsyncMock()  # type: ignore[method-assign]

    result = await service.watch_finish(SimpleNamespace())  # type: ignore[arg-type]

    assert result["history_lost"] is True
    kwargs = store.record_watch_gap.await_args.kwargs
    assert kwargs["gap_reason"] == "ambiguous-watch-apply"
    assert kwargs["alternate_expected_resource_version"] == "18"
    assert kwargs["transport"].request_kind == "watch-history-lost"


@pytest.mark.asyncio
async def test_watch_apply_maps_uppercase_kubernetes_event_to_durable_kind():
    model = InventoryWatchApply.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "leader_generation": 7,
            "event_id": uuid4(),
            "expected_resource_version": "17",
            "observation": {
                "scope": {
                    "source_cluster": "dev-cluster",
                    "api_resource": "core/v1/pods",
                    "namespace": "srw",
                    "cluster_scoped": False,
                },
                "event_type": "BOOKMARK",
                "resource_version": "18",
                "source_event_bytes": 64,
                "collector_observed_at": datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
                "confirms_presence": False,
                "item": None,
            },
        }
    )
    scope = InventoryScopeIdentity(
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        api_resource="core/v1/pods",
        namespace="srw",
    )
    claim = TransportNonceClaim(
        collector_id="kubernetes-pods",
        request_nonce=uuid4(),
        request_kind="watch-event",
        request_digest="a" * 64,
    )
    store = SimpleNamespace(
        apply_watch_event=AsyncMock(
            return_value=SimpleNamespace(
                event_id=model.event_id,
                resource_version="18",
                mutation_action=WatchMutationAction.BOOKMARK,
                session_consumed=False,
                replayed=False,
            )
        )
    )
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _settings(shadow_enabled=True)
    service.store = store
    service._pod_reconciler = SimpleNamespace(apply_watch=AsyncMock())
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubernetes-pods",
                transport_claim=claim,
            ),
        )
    )
    service._require_generation = AsyncMock()  # type: ignore[method-assign]
    service._scope = lambda *_args: scope  # type: ignore[method-assign]

    result = await service.watch_apply(SimpleNamespace())  # type: ignore[arg-type]

    assert result["resource_version"] == "18"
    event = store.apply_watch_event.await_args.args[5]
    assert event.event_type is WatchEventKind.BOOKMARK


@pytest.mark.asyncio
async def test_generation_loop_drains_bounded_cleanup_and_forwards_retention() -> None:
    stop = asyncio.Event()
    first = SimpleNamespace(might_have_more=True)
    final = SimpleNamespace(might_have_more=False)
    diagnostic_calls = 0

    async def finish_cleanup(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal diagnostic_calls
        diagnostic_calls += 1
        if diagnostic_calls == 1:
            return first
        stop.set()
        return final

    store = SimpleNamespace(
        activate_generation=AsyncMock(return_value=17),
        deactivate_generation=AsyncMock(),
        purge_expired_transport_nonces=AsyncMock(side_effect=(1_000, 0)),
        purge_diagnostics=AsyncMock(side_effect=finish_cleanup),
    )

    await run_inventory_generation_loop(
        stop,
        store,
        cleanup_interval_seconds=30,
        snapshot_item_retention=timedelta(days=9),
        diagnostic_retention=timedelta(days=40),
    )

    store.deactivate_generation.assert_awaited_once_with(17)
    assert store.purge_expired_transport_nonces.await_count == 2
    assert store.purge_diagnostics.await_count == 2
    kwargs = store.purge_diagnostics.await_args.kwargs
    assert kwargs["snapshot_item_retention"] == timedelta(days=9)
    assert kwargs["diagnostic_retention"] == timedelta(days=40)
    assert kwargs["abandoned_staging_retention"] == timedelta(hours=24)
    assert kwargs["limit"] == 1_000
