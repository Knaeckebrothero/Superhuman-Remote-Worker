"""Raw-JSON adapter for the synchronous Kubernetes Python client.

Generated client models are intentionally bypassed.  Metering must see newer
resource fields that may not exist in the pinned client, and a raw response
lets us enforce byte limits before decoding it into an object graph.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
import json
from typing import Any

from .contracts import (
    InventoryScope,
    KubernetesApiFailure,
    KubernetesListPage,
    KubernetesWatchEvent,
    WatchEventByteLimitExceeded,
    WatchEventType,
    WatchProtocolFailure,
)


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _response_only(value: Any) -> Any:
    if isinstance(value, tuple):
        return value[0]
    return value


def _close_response(response: Any) -> None:
    try:
        response.close()
    finally:
        release = getattr(response, "release_conn", None)
        if callable(release):
            release()


def _read_bounded(response: Any, maximum: int) -> bytes:
    data = bytearray()
    try:
        while True:
            chunk = response.read(amt=min(65_536, maximum + 1 - len(data)))
            if not chunk:
                break
            encoded = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
            data.extend(encoded)
            if len(data) > maximum:
                raise KubernetesApiFailure(413)
    finally:
        _close_response(response)
    return bytes(data)


def _raw_watch_events(
    response: Any,
    *,
    maximum_event_bytes: int,
) -> Iterator[KubernetesWatchEvent]:
    """Yield raw watch events and always return the HTTP connection."""
    buffer = bytearray()
    try:
        # urllib3 honours ``amt`` while reading from the socket.  Never ask it
        # for an unbounded chunk: an API server (or intermediary) is not a
        # trusted framing boundary and may omit newlines indefinitely.
        read_size = min(65_536, maximum_event_bytes + 1)
        for segment in response.stream(amt=read_size, decode_content=False):
            try:
                encoded = (
                    segment.encode("utf-8")
                    if isinstance(segment, str)
                    else bytes(segment)
                )
            except (TypeError, ValueError) as exc:
                raise WatchProtocolFailure from exc
            buffer.extend(encoded)
            newline = buffer.find(b"\n")
            while newline >= 0:
                if newline > maximum_event_bytes:
                    raise WatchEventByteLimitExceeded
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if line:
                    yield _decode_watch_event(line, maximum_event_bytes)
                newline = buffer.find(b"\n")
            if len(buffer) > maximum_event_bytes:
                raise WatchEventByteLimitExceeded
        if buffer:
            yield _decode_watch_event(bytes(buffer), maximum_event_bytes)
    finally:
        _close_response(response)


def _decode_watch_event(line: bytes, maximum_event_bytes: int) -> KubernetesWatchEvent:
    if len(line) > maximum_event_bytes:
        raise WatchEventByteLimitExceeded
    try:
        event = json.loads(line)
        if not isinstance(event, Mapping):
            raise ValueError
        event_type = WatchEventType(str(event["type"]))
        raw_object = event.get("object")
        if not isinstance(raw_object, Mapping):
            raise ValueError
        metadata = raw_object.get("metadata") or {}
        resource_version = (
            metadata.get("resourceVersion") if isinstance(metadata, Mapping) else None
        )
        status_code = (
            raw_object.get("code") if event_type == WatchEventType.ERROR else None
        )
        if status_code is not None:
            status_code = int(status_code)
        return KubernetesWatchEvent(
            event_type=event_type,
            resource_version=(str(resource_version) if resource_version else None),
            raw_object=dict(raw_object),
            byte_count=len(line) + 1,
            status_code=status_code,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WatchProtocolFailure from exc


def _next(iterator: Iterator[KubernetesWatchEvent]) -> tuple[bool, Any]:
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


_RESOURCE_METHODS = {
    ("core/v1/pods", False): "list_namespaced_pod",
    (
        "core/v1/persistentvolumeclaims",
        False,
    ): "list_namespaced_persistent_volume_claim",
    ("core/v1/persistentvolumes", True): "list_persistent_volume",
}
_CUSTOM_RESOURCE_SCOPES = {
    ("kubevirt.io/v1/virtualmachineinstances", False): (
        "kubevirt.io",
        "v1",
        "virtualmachineinstances",
    ),
}


class RawKubernetesClient:
    """Exact-scope Pod/PVC/PV LIST/WATCH adapter with no selector."""

    def __init__(
        self,
        core_api: Any,
        *,
        custom_objects_api: Any | None = None,
        max_page_bytes: int = 8 * 1024 * 1024,
        max_watch_event_bytes: int = 2 * 1024 * 1024,
        request_timeout_seconds: int = 90,
    ) -> None:
        self._core_api = core_api
        self._custom_objects_api = custom_objects_api
        self._max_page_bytes = max_page_bytes
        self._max_watch_event_bytes = max_watch_event_bytes
        self._request_timeout_seconds = request_timeout_seconds

    def _operation(self, scope: InventoryScope) -> tuple[Any, dict[str, Any]]:
        method_name = _RESOURCE_METHODS.get((scope.api_resource, scope.cluster_scoped))
        if method_name is not None:
            method = getattr(self._core_api, method_name, None)
            if not callable(method):
                raise ValueError("Kubernetes CoreV1 API lacks the requested operation")
            kwargs = {} if scope.cluster_scoped else {"namespace": scope.namespace}
            return method, kwargs

        custom_scope = _CUSTOM_RESOURCE_SCOPES.get(
            (scope.api_resource, scope.cluster_scoped)
        )
        method = getattr(
            self._custom_objects_api,
            "list_namespaced_custom_object",
            None,
        )
        if custom_scope is not None and callable(method):
            group, version, plural = custom_scope
            return method, {
                "group": group,
                "version": version,
                "plural": plural,
                "namespace": scope.namespace,
            }
        raise ValueError(
            "RawKubernetesClient accepts namespaced Pod/PVC/VMI scopes or "
            "cluster-scoped PV scopes only"
        )

    async def list_resources(
        self,
        *,
        scope: InventoryScope,
        limit: int,
        continue_token: str | None,
        resource_version: str | None,
    ) -> KubernetesListPage:
        operation, scope_kwargs = self._operation(scope)
        if resource_version == "0" or (
            resource_version is not None and not resource_version
        ):
            raise ValueError("LIST resource version must be non-zero opaque text")
        if continue_token is not None and resource_version is not None:
            raise ValueError(
                "LIST continuation cannot be combined with a resource version"
            )

        def request() -> bytes:
            try:
                response = operation(
                    **scope_kwargs,
                    limit=limit,
                    _continue=continue_token,
                    resource_version=resource_version,
                    resource_version_match=(
                        "Exact" if resource_version is not None else None
                    ),
                    _preload_content=False,
                    _request_timeout=(10, self._request_timeout_seconds),
                )
                return _read_bounded(
                    _response_only(response),
                    self._max_page_bytes,
                )
            except KubernetesApiFailure:
                raise
            except Exception as exc:
                raise KubernetesApiFailure(_status_code(exc)) from exc

        body = await asyncio.to_thread(request)
        try:
            decoded = json.loads(body)
            if not isinstance(decoded, Mapping):
                raise ValueError
            items = decoded.get("items")
            metadata = decoded.get("metadata")
            if not isinstance(items, list) or not isinstance(metadata, Mapping):
                raise ValueError
            resource_version_value = metadata.get("resourceVersion")
            continue_value = metadata.get("continue") or None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KubernetesApiFailure(None) from exc
        return KubernetesListPage(
            items=items,
            resource_version=(
                str(resource_version_value) if resource_version_value else None
            ),
            continue_token=str(continue_value) if continue_value else None,
            byte_count=len(body),
        )

    def watch_resources(
        self,
        *,
        scope: InventoryScope,
        resource_version: str,
        allow_bookmarks: bool,
        timeout_seconds: int | None,
    ) -> AsyncIterator[KubernetesWatchEvent]:
        operation, scope_kwargs = self._operation(scope)

        async def stream() -> AsyncIterator[KubernetesWatchEvent]:
            iterator: Iterator[KubernetesWatchEvent] | None = None
            response_object: Any = None
            next_task: asyncio.Task[tuple[bool, Any]] | None = None
            try:
                open_task = asyncio.create_task(
                    asyncio.to_thread(
                        operation,
                        **scope_kwargs,
                        watch=True,
                        allow_watch_bookmarks=allow_bookmarks,
                        resource_version=resource_version,
                        timeout_seconds=timeout_seconds,
                        _preload_content=False,
                        _request_timeout=(
                            10,
                            (timeout_seconds or self._request_timeout_seconds) + 15,
                        ),
                    )
                )
                try:
                    response = await asyncio.shield(open_task)
                except asyncio.CancelledError:
                    # A thread-backed Kubernetes request cannot itself be
                    # cancelled.  Wait for its bounded connect/read timeout and
                    # close the eventual response instead of orphaning a pool
                    # connection after the async consumer has gone away.
                    try:
                        response = await asyncio.shield(open_task)
                    except Exception:
                        pass
                    else:
                        await asyncio.to_thread(
                            _close_response, _response_only(response)
                        )
                    raise
                response_object = _response_only(response)
                iterator = _raw_watch_events(
                    response_object,
                    maximum_event_bytes=self._max_watch_event_bytes,
                )
                while True:
                    next_task = asyncio.create_task(asyncio.to_thread(_next, iterator))
                    try:
                        present, event = await asyncio.shield(next_task)
                    except asyncio.CancelledError:
                        # Closing the socket is the only way to unblock a
                        # synchronous urllib3 iterator already executing in a
                        # worker thread.  Do that before touching the generator,
                        # then join the worker so ``iterator.close()`` cannot
                        # race with ``next()`` and mask cancellation with
                        # ``ValueError: generator already executing``.
                        await asyncio.to_thread(_close_response, response_object)
                        try:
                            await asyncio.shield(next_task)
                        except BaseException:
                            pass
                        raise
                    finally:
                        next_task = None
                    if not present:
                        break
                    yield event
            except KubernetesApiFailure:
                raise
            except Exception as exc:
                raise KubernetesApiFailure(_status_code(exc)) from exc
            finally:
                if next_task is not None and not next_task.done():
                    if response_object is not None:
                        await asyncio.to_thread(_close_response, response_object)
                    try:
                        await asyncio.shield(next_task)
                    except BaseException:
                        pass
                if iterator is not None:
                    await asyncio.to_thread(iterator.close)

        return stream()


class RawKubernetesPodClient(RawKubernetesClient):
    """Backward-compatible client name retained for existing Pod runtimes."""


__all__ = ["RawKubernetesClient", "RawKubernetesPodClient"]
