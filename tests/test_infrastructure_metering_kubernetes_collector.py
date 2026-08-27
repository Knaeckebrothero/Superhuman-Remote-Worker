"""Fake-client tests for the bounded Kubernetes inventory state machine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.infrastructure_metering.collectors import (
    CollectorLimits,
    InventoryScope,
    KubernetesApiFailure,
    KubernetesCollectionEngine,
    KubernetesListPage,
    KubernetesWatchEvent,
    RecoverableItemError,
    StagedInventoryItem,
    WatchEventType,
    WatchQueueOverflow,
    inventory_item_digest,
)
from orchestrator.services.infrastructure_metering.collectors.contracts import (
    WatchEventByteLimitExceeded,
    WatchGapReason,
    WatchProtocolFailure,
)
from orchestrator.services.infrastructure_metering.collectors.pod_normalization import (
    normalize_pod,
)


NOW = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
SNAPSHOT_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
SCOPE = InventoryScope("dev-cluster", "core/v1/pods", "srw")


@dataclass(frozen=True)
class _Normalized:
    kind: str
    uid: str
    namespace: str | None
    revision_hash: str | None
    valid_for_metering: bool
    metering_value: int | None = None

    def to_db_item(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "uid": self.uid,
            "namespace": self.namespace,
            "revision_hash": self.revision_hash,
            "valid_for_metering": self.valid_for_metering,
            "metering_value": self.metering_value,
        }


def _revision(kind: str, uid: str, value: Any) -> str:
    payload = json.dumps([kind, uid, value], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _normalize(raw: dict[str, Any]) -> _Normalized:
    metadata = raw.get("metadata") or {}
    kind = raw.get("kind", "Pod")
    uid = metadata.get("uid")
    namespace = metadata.get("namespace")
    if raw.get("raise_fatal"):
        # The collector must never copy this exception text to its envelope.
        raise ValueError(f"decode failed: {raw.get('secret')}")
    if raw.get("raise_item"):
        raise RecoverableItemError(
            kind=kind,
            uid=uid,
            namespace=namespace,
            error_class="capacity-invalid",
        )
    value = raw.get("metering_value")
    valid = raw.get("valid_for_metering", True)
    return _Normalized(
        kind=kind,
        uid=uid,
        namespace=namespace,
        revision_hash=_revision(kind, uid, value) if valid else None,
        valid_for_metering=valid,
        metering_value=value,
    )


def _pod(
    uid: str,
    value: int = 1,
    *,
    namespace: str = "srw",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"uid": uid, "namespace": namespace},
        "metering_value": value,
        **extra,
    }


class _FakeClient:
    def __init__(self, pages=None, watch_events=None):
        self.pages = pages or {}
        self.watch_events = list(watch_events or [])
        self.list_calls: list[dict[str, Any]] = []
        self.watch_calls: list[dict[str, Any]] = []
        self.watch_closed = False

    async def list_resources(self, **kwargs):
        self.list_calls.append(kwargs)
        result = self.pages.get(kwargs["continue_token"])
        if isinstance(result, BaseException):
            raise result
        return result

    def watch_resources(self, **kwargs):
        self.watch_calls.append(kwargs)

        async def stream():
            try:
                for event in self.watch_events:
                    if isinstance(event, BaseException):
                        raise event
                    yield event
            finally:
                self.watch_closed = True

        return stream()


class _DirectWatchClient(_FakeClient):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def watch_resources(self, **kwargs):
        self.watch_calls.append(kwargs)
        return self.stream


def _engine(client: _FakeClient, limits: CollectorLimits | None = None):
    return KubernetesCollectionEngine(
        client,
        _normalize,
        collector_id="kubernetes-pods",
        limits=limits,
        clock=lambda: NOW,
        snapshot_id_factory=lambda: SNAPSHOT_ID,
    )


async def _capture_list(engine, *, scope=SCOPE, generation=7):
    staged = []

    async def stage(item):
        staged.append(item)

    result = await engine.collect_list(
        scope,
        leader_generation=generation,
        stage_item=stage,
    )
    return result, staged


async def _capture_exact_list(
    engine, *, resource_version: str, scope=SCOPE, generation=7
):
    staged = []

    async def stage(item):
        staged.append(item)

    result = await engine.collect_list(
        scope,
        leader_generation=generation,
        stage_item=stage,
        resource_version=resource_version,
    )
    return result, staged


def _page(items, rv="rv-snapshot", token=None, byte_count=100):
    return KubernetesListPage(
        items=items,
        resource_version=rv,
        continue_token=token,
        byte_count=byte_count,
    )


def _watch(kind, rv, raw=None, *, size=10, status=None):
    return KubernetesWatchEvent(
        event_type=kind,
        resource_version=rv,
        raw_object=raw,
        byte_count=size,
        status_code=status,
    )


def test_scope_requires_an_explicit_cluster_scope_for_null_namespace():
    with pytest.raises(ValueError, match="cluster_scoped"):
        InventoryScope("dev-cluster", "core/v1/persistentvolumes", None)

    cluster_scope = InventoryScope(
        "dev-cluster",
        "core/v1/persistentvolumes",
        None,
        cluster_scoped=True,
    )
    assert cluster_scope.key == (
        "dev-cluster",
        "core/v1/persistentvolumes",
        None,
    )


def test_staged_item_requires_a_durable_normalized_mapping_even_when_invalid():
    with pytest.raises(ValueError, match="mapping"):
        StagedInventoryItem(
            scope=SCOPE,
            snapshot_id=SNAPSHOT_ID,
            kind="pod",
            uid="pod-uid",
            revision_hash=None,
            valid_for_metering=False,
            normalized=None,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_paginated_list_is_unfiltered_exact_scope_and_complete_only_at_end():
    client = _FakeClient(
        pages={
            None: _page([_pod("uid-b", 2)], token="next", byte_count=80),
            "next": _page([_pod("uid-a", 1)], byte_count=70),
        }
    )
    snapshot, staged = await _capture_list(_engine(client))

    assert snapshot.complete
    assert snapshot.absence_authoritative
    assert snapshot.scope is SCOPE
    assert snapshot.resource_version == "rv-snapshot"
    assert snapshot.item_count == 2
    assert snapshot.pages_read == 2
    assert snapshot.bytes_read == 150
    assert snapshot.item_digest == inventory_item_digest(
        [
            ("Pod", "uid-b", _revision("Pod", "uid-b", 2)),
            ("Pod", "uid-a", _revision("Pod", "uid-a", 1)),
        ]
    )
    assert [item.uid for item in staged] == ["uid-b", "uid-a"]
    assert all(item.snapshot_id == SNAPSHOT_ID for item in staged)
    assert [call["continue_token"] for call in client.list_calls] == [None, "next"]
    assert all(call["scope"] is SCOPE for call in client.list_calls)
    assert all(call["resource_version"] is None for call in client.list_calls)
    assert all("label_selector" not in call for call in client.list_calls)
    assert not hasattr(snapshot, "items")
    snapshot_wire = json.loads(json.dumps(snapshot.to_wire()))
    assert snapshot_wire["snapshot_id"] == str(SNAPSHOT_ID)
    assert "received_at" not in snapshot_wire
    assert json.loads(json.dumps(staged[0].to_wire()))["normalized"]["uid"] == "uid-b"


@pytest.mark.asyncio
async def test_exact_relist_uses_resource_version_only_on_the_first_page():
    client = _FakeClient(
        pages={
            None: _page([_pod("uid-a")], rv="17", token="next"),
            "next": _page([_pod("uid-b")], rv="17"),
        }
    )

    snapshot, _staged = await _capture_exact_list(
        _engine(client), resource_version="17"
    )

    assert snapshot.complete
    assert [call["resource_version"] for call in client.list_calls] == ["17", None]
    assert [call["continue_token"] for call in client.list_calls] == [None, "next"]


@pytest.mark.asyncio
async def test_exact_relist_fails_closed_when_server_returns_a_newer_version():
    client = _FakeClient(pages={None: _page([_pod("uid-a")], rv="18")})

    snapshot, staged = await _capture_exact_list(_engine(client), resource_version="17")

    assert not snapshot.complete
    assert not snapshot.absence_authoritative
    assert snapshot.resource_version is None
    assert snapshot.item_count == 0
    assert staged == []
    assert [error.error_class for error in snapshot.fatal_errors] == [
        "resource-version-mismatch"
    ]


@pytest.mark.asyncio
async def test_engine_accepts_the_typed_pod_projection_without_raw_object_fields():
    raw = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "uid": "0a83bcf5-feb8-449d-ae26-a0418f445f3d",
            "name": "worker-a",
            "namespace": "srw",
            "resourceVersion": "17",
            "creationTimestamp": "2026-08-05T08:00:00Z",
        },
        "spec": {
            "nodeName": "node-a",
            "containers": [
                {
                    "name": "app",
                    "resources": {"requests": {"cpu": "250m", "memory": "2Gi"}},
                }
            ],
        },
        "status": {
            "phase": "Running",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "True",
                    "lastTransitionTime": "2026-08-05T08:00:02Z",
                }
            ],
        },
    }
    client = _FakeClient(pages={None: _page([raw])})
    engine = KubernetesCollectionEngine(
        client,
        normalize_pod,
        collector_id="kubernetes-pods",
        clock=lambda: NOW,
        snapshot_id_factory=lambda: SNAPSHOT_ID,
    )

    snapshot, staged = await _capture_list(engine)

    assert snapshot.complete
    assert len(staged) == 1
    wire = json.loads(json.dumps(staged[0].to_wire()))
    assert wire["kind"] == "pod"
    assert wire["normalized"]["capacity"]["cpu_millicores"] == 250
    assert wire["normalized"]["capacity"]["memory_bytes"] == 2 * 1024**3
    encoded = json.dumps(wire, sort_keys=True)
    assert '"metadata"' not in encoded
    assert '"spec"' not in encoded


@pytest.mark.asyncio
async def test_digest_is_stable_across_page_and_item_order_including_empty_list():
    first = _FakeClient(pages={None: _page([_pod("b", 2), _pod("a", 1)], byte_count=2)})
    second = _FakeClient(
        pages={
            None: _page([_pod("a", 1)], token="c", byte_count=1),
            "c": _page([_pod("b", 2)], byte_count=1),
        }
    )
    first_snapshot, _ = await _capture_list(_engine(first))
    second_snapshot, _ = await _capture_list(_engine(second))
    empty_snapshot, empty_items = await _capture_list(
        _engine(_FakeClient(pages={None: _page([], byte_count=0)}))
    )

    assert first_snapshot.item_digest == second_snapshot.item_digest
    assert empty_snapshot.complete
    assert empty_snapshot.item_count == 0
    assert (
        empty_snapshot.item_digest
        == hashlib.sha256(b"srw-inventory-manifest-v1\x00").hexdigest()
    )
    assert empty_items == []


@pytest.mark.asyncio
async def test_failed_continuation_page_is_incomplete_and_cannot_close_absent_uids():
    client = _FakeClient(
        pages={
            None: _page([_pod("seen")], token="next"),
            "next": KubernetesApiFailure(403),
        }
    )
    snapshot, staged = await _capture_list(_engine(client))

    assert not snapshot.complete
    assert not snapshot.absence_authoritative
    assert snapshot.resource_version is None
    assert snapshot.item_digest is None
    assert snapshot.item_count == 1
    assert [item.uid for item in staged] == ["seen"]
    assert snapshot.fatal_errors[0].error_class == "kubernetes-api"
    assert "HTTP 403" in snapshot.fatal_errors[0].message


@pytest.mark.asyncio
async def test_identifiable_invalid_item_stays_present_without_blocking_absence_proof():
    client = _FakeClient(
        pages={None: _page([_pod("bad", valid_for_metering=False), _pod("good")])}
    )
    snapshot, staged = await _capture_list(_engine(client))

    assert snapshot.complete
    assert snapshot.item_count == 2
    assert [(error.kind, error.uid) for error in snapshot.item_errors] == [
        ("Pod", "bad")
    ]
    invalid = staged[0]
    assert not invalid.valid_for_metering
    assert invalid.digest_revision == "invalid"
    assert snapshot.item_digest == inventory_item_digest(
        [
            ("Pod", "bad", "invalid"),
            ("Pod", "good", _revision("Pod", "good", 1)),
        ]
    )


@pytest.mark.asyncio
async def test_recoverable_normalizer_exception_preserves_uid_as_invalid_presence():
    client = _FakeClient(pages={None: _page([_pod("bad", raise_item=True)])})
    snapshot, staged = await _capture_list(_engine(client))

    assert snapshot.complete
    assert snapshot.item_errors[0].error_class == "capacity-invalid"
    assert staged[0].uid == "bad"
    assert staged[0].normalized == {
        "source_kind": "Pod",
        "uid": "bad",
        "namespace": "srw",
        "valid_for_metering": False,
        "revision_hash": None,
        "normalization_error": "capacity-invalid",
    }
    assert staged[0].digest_revision == "invalid"


@pytest.mark.asyncio
async def test_fatal_decode_error_is_redacted_and_never_persists_raw_object():
    secret = "super-secret-bearer-token"
    raw = _pod(
        "bad",
        raise_fatal=True,
        secret=secret,
        spec={"containers": [{"env": [{"value": secret}]}]},
    )
    client = _FakeClient(pages={None: _page([raw])})
    snapshot, staged = await _capture_list(_engine(client))

    assert not snapshot.complete
    assert staged == []
    assert snapshot.fatal_errors[0].error_class == "fatal-normalization"
    assert "ValueError" in snapshot.fatal_errors[0].message
    assert secret not in repr(snapshot)
    assert secret not in repr(snapshot.fatal_errors)
    assert secret not in repr(client.pages[None])


@pytest.mark.asyncio
async def test_normalizer_cannot_smuggle_a_raw_object_into_the_wire_payload():
    raw = _pod("bad", spec={"containers": [{"env": [{"value": "secret"}]}]})

    def passthrough(value):
        metadata = value["metadata"]
        return {
            "kind": value["kind"],
            "uid": metadata["uid"],
            "namespace": metadata["namespace"],
            "valid_for_metering": True,
            "revision_hash": _revision(value["kind"], metadata["uid"], 1),
            "spec": value["spec"],
        }

    client = _FakeClient(pages={None: _page([raw])})
    engine = KubernetesCollectionEngine(
        client,
        passthrough,
        collector_id="kubernetes-pods",
        clock=lambda: NOW,
        snapshot_id_factory=lambda: SNAPSHOT_ID,
    )
    snapshot, staged = await _capture_list(engine)

    assert not snapshot.complete
    assert staged == []
    assert snapshot.fatal_errors[-1].error_class == "fatal-payload"
    assert "secret" not in json.dumps(snapshot.to_wire()).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pages", "expected_error"),
    [
        (
            {None: _page([_pod("a"), _pod("b")], byte_count=1)},
            "page-item-limit",
        ),
        ({None: _page([], byte_count=11)}, "page-byte-limit"),
        (
            {
                None: _page([_pod("a")], token="c", byte_count=5),
                "c": _page([_pod("b")], byte_count=5),
            },
            "snapshot-item-limit",
        ),
        (
            {
                None: _page([], token="c", byte_count=6),
                "c": _page([], byte_count=6),
            },
            "snapshot-byte-limit",
        ),
        (
            {None: _page([], token="c", byte_count=1), "c": _page([])},
            "page-limit",
        ),
    ],
)
async def test_list_limits_fail_closed_before_oversized_content_is_staged(
    pages, expected_error
):
    limits = CollectorLimits(
        list_page_size=1,
        max_page_items=1,
        max_pages=1 if expected_error == "page-limit" else 10,
        max_page_bytes=10,
        max_snapshot_items=1,
        max_snapshot_bytes=10,
        max_watch_events=10,
        max_watch_event_bytes=10,
        max_watch_bytes=10,
    )
    snapshot, staged = await _capture_list(_engine(_FakeClient(pages=pages), limits))

    assert not snapshot.complete
    assert snapshot.resource_version is None
    assert snapshot.item_digest is None
    assert snapshot.fatal_errors[-1].error_class == expected_error
    if expected_error in {"page-item-limit", "page-byte-limit"}:
        assert staged == []


@pytest.mark.asyncio
async def test_resource_version_change_continuation_cycle_and_duplicate_uid_are_fatal():
    rv_client = _FakeClient(
        pages={
            None: _page([], rv="rv-a", token="c"),
            "c": _page([], rv="rv-b"),
        }
    )
    rv_snapshot, _ = await _capture_list(_engine(rv_client))
    assert rv_snapshot.fatal_errors[-1].error_class == "resource-version-mismatch"

    cycle_client = _FakeClient(
        pages={None: _page([], token="c"), "c": _page([], token="c")}
    )
    cycle_snapshot, _ = await _capture_list(_engine(cycle_client))
    assert cycle_snapshot.fatal_errors[-1].error_class == "continuation-cycle"

    duplicate_client = _FakeClient(pages={None: _page([_pod("same"), _pod("same")])})
    duplicate_snapshot, staged = await _capture_list(_engine(duplicate_client))
    assert duplicate_snapshot.fatal_errors[-1].error_class == "duplicate-identity"
    assert len(staged) == 1


@pytest.mark.asyncio
async def test_cross_scope_item_and_staging_failure_keep_snapshot_incomplete():
    cross_scope, staged = await _capture_list(
        _engine(_FakeClient(pages={None: _page([_pod("x", namespace="other")])}))
    )
    assert cross_scope.fatal_errors[-1].error_class == "fatal-identity"
    assert staged == []

    engine = _engine(_FakeClient(pages={None: _page([_pod("x")])}))

    async def fail_stage(_item):
        raise RuntimeError("db password should never be echoed")

    failed = await engine.collect_list(
        SCOPE,
        leader_generation=1,
        stage_item=fail_stage,
    )
    assert not failed.complete
    assert failed.fatal_errors[-1].error_class == "staging-failed"
    assert "password" not in failed.fatal_errors[-1].message.lower()


@pytest.mark.asyncio
async def test_watch_applies_object_and_cursor_together_in_opaque_stream_order():
    secret = "do-not-forward-this-secret"
    client = _FakeClient(
        watch_events=[
            _watch("ADDED", "z", _pod("one", 1, secret=secret)),
            _watch("BOOKMARK", "opaque-bookmark"),
            # Resource versions are opaque: lexical/numeric ordering is invalid.
            _watch("MODIFIED", "a", _pod("one", 2)),
            _watch("DELETED", "end", _pod("one", 2)),
        ]
    )
    observations = []

    async def apply(observation):
        observations.append(observation)

    outcome = await _engine(client).watch(
        SCOPE,
        resource_version="start",
        apply_observation=apply,
        timeout_seconds=30,
    )

    assert [observation.event_type for observation in observations] == [
        WatchEventType.ADDED,
        WatchEventType.BOOKMARK,
        WatchEventType.MODIFIED,
        WatchEventType.DELETED,
    ]
    assert [observation.resource_version for observation in observations] == [
        "z",
        "opaque-bookmark",
        "a",
        "end",
    ]
    assert observations[1].item is None
    assert [observation.source_event_bytes for observation in observations] == [
        10,
        10,
        10,
        10,
    ]
    assert not observations[1].confirms_object
    assert not observations[1].confirms_presence
    assert observations[0].confirms_presence
    assert not observations[-1].confirms_presence
    assert outcome.committed_resource_version == "end"
    assert outcome.processed_events == 4
    assert outcome.object_events == 3
    assert outcome.bookmarks == 1
    assert not outcome.relist_required
    assert outcome.gap_reason is None
    assert outcome.ambiguous_resource_version is None
    assert secret not in repr(observations)
    observation_wire = [event.to_wire() for event in observations]
    assert [event["source_event_bytes"] for event in observation_wire] == [
        10,
        10,
        10,
        10,
    ]
    assert secret not in json.dumps(observation_wire)
    json.dumps(outcome.to_wire())
    assert client.watch_calls == [
        {
            "scope": SCOPE,
            "resource_version": "start",
            "allow_bookmarks": True,
            "timeout_seconds": 30,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expired",
    [
        _watch("ERROR", "expired", status=410),
        KubernetesApiFailure(410),
    ],
)
async def test_watch_410_is_an_explicit_history_gap_and_relist_signal(expired):
    observations = []
    client = _FakeClient(watch_events=[expired])
    outcome = await _engine(client).watch(
        SCOPE,
        resource_version="old",
        apply_observation=observations.append,
    )

    assert outcome.relist_required
    assert outcome.history_lost
    assert outcome.committed_resource_version == "old"
    assert outcome.processed_events == 0
    assert observations == []
    assert outcome.gap_reason == WatchGapReason.RESOURCE_VERSION_EXPIRED
    assert outcome.ambiguous_resource_version is None
    assert outcome.to_wire()["gap_reason"] == "resource-version-expired"
    assert outcome.fatal_errors[-1].error_class == "resource-version-expired"


@pytest.mark.asyncio
async def test_bookmark_advances_cursor_but_never_confirms_an_item():
    observations = []
    client = _FakeClient(watch_events=[_watch("BOOKMARK", "bookmark")])
    outcome = await _engine(client).watch(
        SCOPE,
        resource_version="old",
        apply_observation=observations.append,
    )

    assert outcome.committed_resource_version == "bookmark"
    assert outcome.bookmarks == 1
    assert outcome.object_events == 0
    assert observations[0].item is None
    assert not observations[0].confirms_object


@pytest.mark.asyncio
async def test_invalid_watch_item_advances_with_presence_error_but_fatal_item_does_not():
    invalid_observations = []
    invalid_client = _FakeClient(
        watch_events=[_watch("MODIFIED", "next", _pod("bad", valid_for_metering=False))]
    )
    invalid_outcome = await _engine(invalid_client).watch(
        SCOPE,
        resource_version="old",
        apply_observation=invalid_observations.append,
    )
    assert invalid_outcome.committed_resource_version == "next"
    assert invalid_outcome.item_errors[0].uid == "bad"
    assert invalid_observations[0].item is not None
    assert not invalid_observations[0].item.valid_for_metering
    assert invalid_observations[0].confirms_presence

    fatal_observations = []
    fatal_client = _FakeClient(
        watch_events=[
            _watch(
                "MODIFIED",
                "skipped",
                _pod("bad", raise_fatal=True, secret="never-echo-me"),
            )
        ]
    )
    fatal_outcome = await _engine(fatal_client).watch(
        SCOPE,
        resource_version="old",
        apply_observation=fatal_observations.append,
    )
    assert fatal_outcome.committed_resource_version == "old"
    assert fatal_outcome.relist_required
    assert fatal_outcome.history_lost
    assert fatal_outcome.gap_reason == WatchGapReason.NORMALIZATION_ERROR
    assert fatal_outcome.ambiguous_resource_version is None
    assert fatal_observations == []
    assert "never-echo-me" not in repr(fatal_outcome)


@pytest.mark.asyncio
async def test_watch_apply_failure_reports_only_last_committed_cursor():
    client = _FakeClient(
        watch_events=[
            _watch("ADDED", "one", _pod("one")),
            _watch("MODIFIED", "two", _pod("one", 2)),
        ]
    )
    applied = []

    async def apply(observation):
        if observation.resource_version == "two":
            raise RuntimeError("database credential must stay private")
        applied.append(observation)

    outcome = await _engine(client).watch(
        SCOPE,
        resource_version="old",
        apply_observation=apply,
    )

    assert outcome.committed_resource_version == "one"
    assert outcome.processed_events == 1
    assert [event.resource_version for event in applied] == ["one"]
    assert outcome.relist_required
    assert outcome.history_lost
    assert outcome.gap_reason == WatchGapReason.AMBIGUOUS_APPLY
    assert outcome.ambiguous_resource_version == "two"
    assert outcome.to_wire()["ambiguous_resource_version"] == "two"
    assert outcome.fatal_errors[-1].error_class == "watch-apply-failed"
    assert "credential" not in outcome.fatal_errors[-1].message.lower()


@pytest.mark.asyncio
async def test_watch_queue_overflow_forces_relist_without_silent_drop():
    client = _FakeClient(
        watch_events=[
            _watch("ADDED", "one", _pod("one")),
            WatchQueueOverflow("raw queue payload must not leak"),
        ]
    )
    outcome = await _engine(client).watch(
        SCOPE,
        resource_version="old",
        apply_observation=lambda _event: None,
    )

    assert outcome.committed_resource_version == "one"
    assert outcome.relist_required
    assert outcome.history_lost
    assert outcome.gap_reason == WatchGapReason.QUEUE_OVERFLOW
    assert outcome.ambiguous_resource_version is None
    assert outcome.fatal_errors[-1].error_class == "watch-queue-overflow"
    assert "payload" not in outcome.fatal_errors[-1].message


@pytest.mark.asyncio
async def test_watch_limits_are_bounded_and_visible():
    event_limit = replace(CollectorLimits(), max_watch_events=1)
    client = _FakeClient(
        watch_events=[
            _watch("ADDED", "one", _pod("one"), size=1),
            _watch("ADDED", "two", _pod("two"), size=1),
        ]
    )
    outcome = await _engine(client, event_limit).watch(
        SCOPE,
        resource_version="old",
        apply_observation=lambda _event: None,
    )
    assert outcome.limit_reached
    assert outcome.processed_events == 1
    assert outcome.committed_resource_version == "one"
    assert not outcome.relist_required
    assert outcome.gap_reason is None
    assert client.watch_closed

    byte_limits = replace(
        CollectorLimits(), max_watch_event_bytes=10, max_watch_bytes=10
    )
    too_large = _FakeClient(
        watch_events=[_watch("ADDED", "large", _pod("one"), size=11)]
    )
    large_outcome = await _engine(too_large, byte_limits).watch(
        SCOPE,
        resource_version="old",
        apply_observation=lambda _event: None,
    )
    assert large_outcome.processed_events == 0
    assert large_outcome.relist_required
    assert large_outcome.history_lost
    assert large_outcome.gap_reason == WatchGapReason.EVENT_BYTE_LIMIT
    assert large_outcome.fatal_errors[-1].error_class == "watch-event-byte-limit"

    stream_limited = _FakeClient(
        watch_events=[
            _watch("ADDED", "one", _pod("one"), size=6),
            _watch("ADDED", "two", _pod("two"), size=6),
        ]
    )
    stream_outcome = await _engine(stream_limited, byte_limits).watch(
        SCOPE,
        resource_version="old",
        apply_observation=lambda _event: None,
    )
    assert stream_outcome.processed_events == 1
    assert stream_outcome.committed_resource_version == "one"
    assert stream_outcome.bytes_read == 6
    assert stream_outcome.relist_required
    assert stream_outcome.history_lost
    assert stream_outcome.gap_reason == WatchGapReason.STREAM_BYTE_LIMIT
    assert stream_outcome.fatal_errors[-1].error_class == "watch-byte-limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"type": "ADDED", "object": {}},
        _watch("ADDED", None, _pod("missing-cursor")),
        _watch("ADDED", "0", _pod("zero-cursor")),
    ],
)
async def test_watch_protocol_gaps_have_a_stable_reason(event: Any) -> None:
    outcome = await _engine(_FakeClient(watch_events=[event])).watch(
        SCOPE,
        resource_version="old",
        apply_observation=lambda _event: None,
    )

    assert outcome.committed_resource_version == "old"
    assert outcome.relist_required
    assert outcome.history_lost
    assert outcome.gap_reason == WatchGapReason.PROTOCOL_ERROR
    assert outcome.ambiguous_resource_version is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reason", "error_class"),
    [
        (
            WatchEventByteLimitExceeded(),
            WatchGapReason.EVENT_BYTE_LIMIT,
            "watch-event-byte-limit",
        ),
        (
            WatchProtocolFailure(),
            WatchGapReason.PROTOCOL_ERROR,
            "protocol-error",
        ),
    ],
)
async def test_typed_adapter_watch_failures_open_the_matching_gap(
    failure: BaseException,
    reason: WatchGapReason,
    error_class: str,
) -> None:
    outcome = await _engine(_FakeClient(watch_events=[failure])).watch(
        SCOPE,
        resource_version="old",
        apply_observation=lambda _event: None,
    )

    assert outcome.committed_resource_version == "old"
    assert outcome.relist_required
    assert outcome.history_lost
    assert outcome.gap_reason == reason
    assert outcome.fatal_errors[-1].error_class == error_class


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [KubernetesApiFailure(None), KubernetesApiFailure(413)]
)
async def test_ordinary_watch_disconnects_resume_from_the_committed_cursor(
    failure: KubernetesApiFailure,
) -> None:
    outcome = await _engine(
        _FakeClient(
            watch_events=[
                _watch("ADDED", "one", _pod("one")),
                failure,
            ]
        )
    ).watch(
        SCOPE,
        resource_version="old",
        apply_observation=lambda _event: None,
    )

    assert outcome.committed_resource_version == "one"
    assert outcome.reconnect_required
    assert not outcome.relist_required
    assert not outcome.history_lost
    assert outcome.gap_reason is None
    assert outcome.fatal_errors[-1].error_class == "kubernetes-api"


@pytest.mark.asyncio
async def test_watch_closes_a_distinct_async_iterable_wrapper():
    class EmptyIterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class WrappedStream:
        def __init__(self):
            self.closed = False

        def __aiter__(self):
            return EmptyIterator()

        async def aclose(self):
            self.closed = True

    stream = WrappedStream()
    outcome = await _engine(_DirectWatchClient(stream)).watch(
        SCOPE,
        resource_version="old",
        apply_observation=lambda _event: None,
    )

    assert stream.closed
    assert not outcome.fatal_errors


@pytest.mark.asyncio
async def test_watch_cancellation_deterministically_closes_the_iterator():
    class BlockingStream:
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True

    stream = BlockingStream()
    task = asyncio.create_task(
        _engine(_DirectWatchClient(stream)).watch(
            SCOPE,
            resource_version="old",
            apply_observation=lambda _event: None,
        )
    )
    await asyncio.wait_for(stream.started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed


@pytest.mark.asyncio
async def test_watch_rejects_resource_version_zero_before_opening_stream():
    client = _FakeClient()
    with pytest.raises(ValueError, match="version 0"):
        await _engine(client).watch(
            SCOPE,
            resource_version="0",
            apply_observation=lambda _event: None,
        )
    assert client.watch_calls == []


def test_watch_event_wire_size_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        _watch("BOOKMARK", "next", size=0)
