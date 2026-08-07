from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from orchestrator.services.infrastructure_metering.collectors.contracts import (
    InventoryScope,
    KubernetesApiFailure,
    WatchEventByteLimitExceeded,
    WatchEventType,
    WatchProtocolFailure,
)
from orchestrator.services.infrastructure_metering.collectors.kubernetes_client import (
    RawKubernetesClient,
    RawKubernetesPodClient,
)


SCOPE = InventoryScope("dev-cluster", "core/v1/pods", "srw")
PVC_SCOPE = InventoryScope("dev-cluster", "core/v1/persistentvolumeclaims", "srw")
PV_SCOPE = InventoryScope(
    "dev-cluster", "core/v1/persistentvolumes", None, cluster_scoped=True
)
VMI_SCOPE = InventoryScope(
    "vm-cluster", "kubevirt.io/v1/virtualmachineinstances", "srw-vms"
)


class _Response:
    def __init__(self, body: bytes, *, segments: list[bytes] | None = None):
        self.body = body
        self.offset = 0
        self.segments = segments
        self.closed = False
        self.released = False
        self.stream_amounts: list[int | None] = []

    def read(self, amt: int):
        chunk = self.body[self.offset : self.offset + amt]
        self.offset += len(chunk)
        return chunk

    def stream(self, amt=None, decode_content=False):
        self.stream_amounts.append(amt)
        yield from self.segments or [self.body]

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class _CoreApi:
    def __init__(self, responses: list[_Response]):
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.operations: list[str] = []

    def list_namespaced_pod(self, **kwargs):
        self.operations.append("pod")
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def list_namespaced_persistent_volume_claim(self, **kwargs):
        self.operations.append("pvc")
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def list_persistent_volume(self, **kwargs):
        self.operations.append("pv")
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _CustomObjectsApi:
    def __init__(self, responses: list[_Response]):
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def list_namespaced_custom_object(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _BlockingResponse(_Response):
    def __init__(self):
        super().__init__(b"")
        self.read_started = threading.Event()
        self.release_read = threading.Event()

    def stream(self, amt=None, decode_content=False):
        self.stream_amounts.append(amt)
        self.read_started.set()
        self.release_read.wait(timeout=5)
        if False:
            yield b""

    def close(self):
        super().close()
        self.release_read.set()


@pytest.mark.asyncio
async def test_raw_client_lists_unfiltered_pages_and_counts_wire_bytes():
    body = json.dumps(
        {
            "metadata": {"resourceVersion": "rv-1", "continue": "next"},
            "items": [{"kind": "Pod", "metadata": {"uid": "u1"}}],
        }
    ).encode()
    response = _Response(body)
    api = _CoreApi([response])
    page = await RawKubernetesPodClient(api).list_resources(
        scope=SCOPE,
        limit=500,
        continue_token=None,
        resource_version=None,
    )

    assert page.resource_version == "rv-1"
    assert page.continue_token == "next"
    assert page.byte_count == len(body)
    assert len(page.items) == 1
    assert "label_selector" not in api.calls[0]
    assert response.closed and response.released


@pytest.mark.asyncio
async def test_raw_client_requests_an_exact_nonzero_resource_version():
    body = json.dumps({"metadata": {"resourceVersion": "17"}, "items": []}).encode()
    api = _CoreApi([_Response(body)])

    await RawKubernetesPodClient(api).list_resources(
        scope=SCOPE,
        limit=500,
        continue_token=None,
        resource_version="17",
    )

    assert api.calls[0]["resource_version"] == "17"
    assert api.calls[0]["resource_version_match"] == "Exact"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "operation", "namespaced"),
    [
        (SCOPE, "pod", True),
        (PVC_SCOPE, "pvc", True),
        (PV_SCOPE, "pv", False),
    ],
)
async def test_general_raw_client_dispatches_each_supported_list_scope(
    scope: InventoryScope, operation: str, namespaced: bool
) -> None:
    body = json.dumps({"metadata": {"resourceVersion": "17"}, "items": []}).encode()
    api = _CoreApi([_Response(body)])

    await RawKubernetesClient(api).list_resources(
        scope=scope,
        limit=500,
        continue_token=None,
        resource_version=None,
    )

    assert api.operations == [operation]
    assert ("namespace" in api.calls[0]) is namespaced
    if namespaced:
        assert api.calls[0]["namespace"] == "srw"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "operation"),
    [(PVC_SCOPE, "pvc"), (PV_SCOPE, "pv")],
)
async def test_general_raw_client_dispatches_storage_watch_scopes(
    scope: InventoryScope, operation: str
) -> None:
    response = _Response(
        b"",
        segments=[
            json.dumps(
                {
                    "type": "BOOKMARK",
                    "object": {"metadata": {"resourceVersion": "18"}},
                }
            ).encode()
            + b"\n"
        ],
    )
    api = _CoreApi([response])

    events = [
        event
        async for event in RawKubernetesClient(api).watch_resources(
            scope=scope,
            resource_version="17",
            allow_bookmarks=True,
            timeout_seconds=30,
        )
    ]

    assert api.operations == [operation]
    assert [event.event_type for event in events] == [WatchEventType.BOOKMARK]
    assert events[0].resource_version == "18"


@pytest.mark.asyncio
async def test_general_raw_client_dispatches_exact_vmi_list_and_watch_scope():
    list_body = json.dumps(
        {"metadata": {"resourceVersion": "17"}, "items": []}
    ).encode()
    watch_response = _Response(
        b"",
        segments=[
            b'{"type":"BOOKMARK","object":{"metadata":{"resourceVersion":"18"}}}\n'
        ],
    )
    custom = _CustomObjectsApi([_Response(list_body), watch_response])
    client = RawKubernetesClient(_CoreApi([]), custom_objects_api=custom)

    page = await client.list_resources(
        scope=VMI_SCOPE,
        limit=500,
        continue_token=None,
        resource_version=None,
    )
    events = [
        event
        async for event in client.watch_resources(
            scope=VMI_SCOPE,
            resource_version=page.resource_version or "",
            allow_bookmarks=True,
            timeout_seconds=30,
        )
    ]

    assert custom.calls[0]["group"] == "kubevirt.io"
    assert custom.calls[0]["version"] == "v1"
    assert custom.calls[0]["plural"] == "virtualmachineinstances"
    assert custom.calls[0]["namespace"] == "srw-vms"
    assert "label_selector" not in custom.calls[0]
    assert custom.calls[1]["watch"] is True
    assert [event.resource_version for event in events] == ["18"]


@pytest.mark.asyncio
async def test_general_raw_client_rejects_wrong_scope_for_known_resource() -> None:
    wrong = InventoryScope("dev-cluster", "core/v1/persistentvolumes", "srw")
    client = RawKubernetesClient(_CoreApi([]))

    with pytest.raises(ValueError, match="Pod/PVC/VMI scopes"):
        await client.list_resources(
            scope=wrong,
            limit=500,
            continue_token=None,
            resource_version=None,
        )


@pytest.mark.asyncio
async def test_raw_client_rejects_resource_version_with_continuation():
    client = RawKubernetesPodClient(_CoreApi([]))

    with pytest.raises(ValueError, match="continuation"):
        await client.list_resources(
            scope=SCOPE,
            limit=500,
            continue_token="next",
            resource_version="17",
        )


@pytest.mark.asyncio
async def test_raw_client_fails_before_decoding_an_oversized_page():
    response = _Response(b"{" + b"x" * 100 + b"}")
    api = _CoreApi([response])
    with pytest.raises(KubernetesApiFailure) as raised:
        await RawKubernetesPodClient(api, max_page_bytes=20).list_resources(
            scope=SCOPE,
            limit=1,
            continue_token=None,
            resource_version=None,
        )
    assert raised.value.status_code == 413
    assert response.closed and response.released


@pytest.mark.asyncio
async def test_raw_client_streams_watch_events_and_preserves_410_status():
    lines = [
        json.dumps(
            {
                "type": "ADDED",
                "object": {
                    "kind": "Pod",
                    "metadata": {"uid": "u1", "resourceVersion": "rv-2"},
                },
            }
        ).encode()
        + b"\n",
        json.dumps(
            {
                "type": "ERROR",
                "object": {
                    "kind": "Status",
                    "metadata": {"resourceVersion": "rv-3"},
                    "code": 410,
                },
            }
        ).encode()
        + b"\n",
    ]
    response = _Response(b"", segments=[lines[0][:20], lines[0][20:] + lines[1]])
    api = _CoreApi([response])
    events = []
    async for event in RawKubernetesPodClient(api).watch_resources(
        scope=SCOPE,
        resource_version="rv-1",
        allow_bookmarks=True,
        timeout_seconds=30,
    ):
        events.append(event)

    assert [event.event_type for event in events] == [
        WatchEventType.ADDED,
        WatchEventType.ERROR,
    ]
    assert events[0].resource_version == "rv-2"
    assert events[1].status_code == 410
    assert response.stream_amounts == [65_536]
    assert response.closed and response.released


@pytest.mark.asyncio
async def test_raw_client_types_oversized_watch_framing() -> None:
    response = _Response(b"", segments=[b"x" * 21])
    iterator = RawKubernetesPodClient(
        _CoreApi([response]),
        max_watch_event_bytes=20,
    ).watch_resources(
        scope=SCOPE,
        resource_version="rv-1",
        allow_bookmarks=True,
        timeout_seconds=30,
    )

    with pytest.raises(WatchEventByteLimitExceeded) as raised:
        await anext(iterator)

    assert raised.value.status_code == 413
    assert response.closed and response.released


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        b"not-json\n",
        b'{"type":"ADDED","object":[]}\n',
        b'{"type":"UNKNOWN","object":{}}\n',
    ],
)
async def test_raw_client_types_malformed_watch_protocol(frame: bytes) -> None:
    response = _Response(b"", segments=[frame])
    iterator = RawKubernetesPodClient(_CoreApi([response])).watch_resources(
        scope=SCOPE,
        resource_version="rv-1",
        allow_bookmarks=True,
        timeout_seconds=30,
    )

    with pytest.raises(WatchProtocolFailure) as raised:
        await anext(iterator)

    assert raised.value.status_code is None
    assert response.closed and response.released


@pytest.mark.asyncio
async def test_raw_client_closes_watch_when_consumer_stops_early():
    lines = [
        json.dumps(
            {
                "type": "ADDED",
                "object": {
                    "kind": "Pod",
                    "metadata": {"uid": "u1", "resourceVersion": "rv-2"},
                },
            }
        ).encode()
        + b"\n",
        b'{"type":"BOOKMARK","object":{"metadata":{"resourceVersion":"rv-3"}}}\n',
    ]
    response = _Response(b"", segments=lines)
    iterator = RawKubernetesPodClient(_CoreApi([response])).watch_resources(
        scope=SCOPE,
        resource_version="rv-1",
        allow_bookmarks=True,
        timeout_seconds=30,
    )

    await anext(iterator)
    await iterator.aclose()

    assert response.closed and response.released


@pytest.mark.asyncio
async def test_raw_client_cancellation_unblocks_and_joins_blocking_watch_read():
    response = _BlockingResponse()
    iterator = RawKubernetesPodClient(_CoreApi([response])).watch_resources(
        scope=SCOPE,
        resource_version="rv-1",
        allow_bookmarks=True,
        timeout_seconds=30,
    )
    read_task = asyncio.create_task(anext(iterator))
    assert await asyncio.to_thread(response.read_started.wait, 1)

    read_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(read_task, timeout=2)

    assert response.closed and response.released
    await iterator.aclose()
