"""Exact-recipient tests for session Service + Ingress publication."""

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.session_router import (
    SessionRouteAuthorityError,
    SessionRouterService,
)
from src.shared.pinned_session_identity import PinnedSessionBinding

THREAD_ID = "00000000-0000-4000-8000-0000000000a1"
AGENT_ID = "00000000-0000-4000-8000-0000000000a2"
ATTACH_TOKEN = "00000000-0000-4000-8000-0000000000a3"
RUNTIME_GENERATION = "11111111-2222-4333-8444-555555555555"
POD_NAME = "srw-agent-abc"
POD_UID = "pod-uid-1"
POD_IP = "10.0.0.5"


def _not_found():
    from kubernetes.client.exceptions import ApiException

    return ApiException(status=404)


def _binding(**changes) -> PinnedSessionBinding:
    values = {
        "thread_id": THREAD_ID,
        "runtime_generation": RUNTIME_GENERATION,
        "agent_id": AGENT_ID,
        "runtime_attach_token": ATTACH_TOKEN,
        "agent_hostname": POD_NAME,
        "pod_namespace": "srw",
        "pod_uid": POD_UID,
        "pod_ip": POD_IP,
        "pod_port": 8001,
        "agent_status": "session",
    }
    values.update(changes)
    return PinnedSessionBinding(**values)


def _pod(*, uid=POD_UID, ip=POD_IP, route_labels=False, resource_version="7"):
    labels = {
        "srw/component": "agent",
        "srw/managed-by": "agent-provisioner",
        "srw/purpose": "session" if route_labels else "job",
    }
    if route_labels:
        labels.update(
            {
                "srw.io/thread-id": THREAD_ID,
                "srw/thread-id": THREAD_ID[:12],
                "srw.io/runtime-generation": RUNTIME_GENERATION,
            }
        )
    return {
        "metadata": {
            "name": POD_NAME,
            "namespace": "srw",
            "uid": uid,
            "resourceVersion": resource_version,
            "labels": labels,
        },
        "status": {
            "phase": "Running",
            "podIP": ip,
            "containerStatuses": [{"name": "agent", "ready": True}],
        },
    }


@pytest.fixture
def db():
    value = MagicMock()
    value.get_pinned_session_binding = AsyncMock(return_value=_binding())
    return value


@pytest.fixture
def k8s_core_api():
    api = MagicMock()
    pod = _pod()
    services = {}

    def read_pod(**_kwargs):
        return deepcopy(pod)

    def patch_pod(*, body, **_kwargs):
        for operation in body:
            if operation["op"] != "add":
                continue
            path = operation["path"]
            if path == "/metadata/labels/srw.io~1thread-id":
                pod["metadata"]["labels"]["srw.io/thread-id"] = operation["value"]
            elif path == "/metadata/labels/srw~1thread-id":
                pod["metadata"]["labels"]["srw/thread-id"] = operation["value"]
            elif path == "/metadata/labels/srw~1purpose":
                pod["metadata"]["labels"]["srw/purpose"] = operation["value"]
            elif path == "/metadata/labels/srw.io~1runtime-generation":
                pod["metadata"]["labels"]["srw.io/runtime-generation"] = operation[
                    "value"
                ]
        return deepcopy(pod)

    def read_service(*, name, **_kwargs):
        if name not in services:
            raise _not_found()
        return deepcopy(services[name])

    def create_service(*, body, **_kwargs):
        resource = deepcopy(body)
        resource["metadata"]["uid"] = "service-uid"
        services[resource["metadata"]["name"]] = resource
        return deepcopy(resource)

    def patch_service(*, name, body, **_kwargs):
        resource = services[name]
        for operation in body:
            if operation["path"] == "/metadata/labels/srw.io~1runtime-generation":
                resource["metadata"]["labels"]["srw.io/runtime-generation"] = operation[
                    "value"
                ]
            elif operation["path"] == "/metadata/ownerReferences":
                resource["metadata"]["ownerReferences"] = operation["value"]
            elif operation["path"] == "/spec/selector":
                resource["spec"]["selector"] = operation["value"]
        return deepcopy(resource)

    api._pod = pod
    api._services = services
    api.read_namespaced_pod.side_effect = read_pod
    api.patch_namespaced_pod.side_effect = patch_pod
    api.read_namespaced_service.side_effect = read_service
    api.create_namespaced_service.side_effect = create_service
    api.patch_namespaced_service.side_effect = patch_service
    return api


@pytest.fixture
def k8s_networking_api():
    api = MagicMock()
    ingresses = {}

    def read_ingress(*, name, **_kwargs):
        if name not in ingresses:
            raise _not_found()
        return deepcopy(ingresses[name])

    def create_ingress(*, body, **_kwargs):
        resource = deepcopy(body)
        resource["metadata"]["uid"] = "ingress-uid"
        ingresses[resource["metadata"]["name"]] = resource
        return deepcopy(resource)

    def patch_ingress(*, name, body, **_kwargs):
        resource = ingresses[name]
        for operation in body:
            if operation["path"] == "/metadata/labels/srw.io~1runtime-generation":
                resource["metadata"]["labels"]["srw.io/runtime-generation"] = operation[
                    "value"
                ]
            elif operation["path"] == "/metadata/ownerReferences":
                resource["metadata"]["ownerReferences"] = operation["value"]
        return deepcopy(resource)

    api._ingresses = ingresses
    api.read_namespaced_ingress.side_effect = read_ingress
    api.create_namespaced_ingress.side_effect = create_ingress
    api.patch_namespaced_ingress.side_effect = patch_ingress
    return api


@pytest.fixture
def svc(db, k8s_core_api, k8s_networking_api):
    return SessionRouterService(
        namespace="srw",
        ingress_host="api.example.com",
        ingress_class="traefik",
        annotations={"foo": "bar"},
        db=db,
        core_api=k8s_core_api,
        networking_api=k8s_networking_api,
    )


@pytest.mark.asyncio
async def test_route_creation_is_pod_uid_resource_version_and_generation_fenced(
    svc, db, k8s_core_api, k8s_networking_api
):
    assert (
        await svc.ensure_route(THREAD_ID, POD_NAME, POD_UID, RUNTIME_GENERATION)
        == f"/p/{THREAD_ID}"
    )

    patch = k8s_core_api.patch_namespaced_pod.call_args.kwargs
    assert patch["body"][:2] == [
        {"op": "test", "path": "/metadata/uid", "value": POD_UID},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": "7",
        },
    ]
    assert {
        "op": "add",
        "path": "/metadata/labels/srw.io~1runtime-generation",
        "value": RUNTIME_GENERATION,
    } in patch["body"]
    service = k8s_core_api._services[f"session-{THREAD_ID}"]
    assert service["spec"]["selector"] == {
        "srw.io/thread-id": THREAD_ID,
        "srw.io/runtime-generation": RUNTIME_GENERATION,
    }
    ingress = k8s_networking_api._ingresses[f"session-{THREAD_ID}"]
    assert ingress["metadata"]["ownerReferences"][0]["uid"] == POD_UID
    assert db.get_pinned_session_binding.await_count >= 3
    assert "_request_timeout" in k8s_core_api.read_namespaced_pod.call_args.kwargs


@pytest.mark.asyncio
async def test_legacy_namespace_route_targets_exact_binding_without_shadow(
    svc, db, k8s_core_api, k8s_networking_api, monkeypatch
):
    monkeypatch.setenv("PINNED_LEGACY_AGENT_NAMESPACES", "agents-old")
    db.get_pinned_session_binding.return_value = _binding(pod_namespace="agents-old")
    k8s_core_api._pod["metadata"]["namespace"] = "agents-old"

    assert (
        await svc.ensure_route(THREAD_ID, POD_NAME, POD_UID, RUNTIME_GENERATION)
        == f"/p/{THREAD_ID}"
    )

    assert k8s_core_api.patch_namespaced_pod.call_args.kwargs["namespace"] == (
        "agents-old"
    )
    assert k8s_core_api.create_namespaced_service.call_args.kwargs["namespace"] == (
        "agents-old"
    )
    assert (
        k8s_networking_api.create_namespaced_ingress.call_args.kwargs["namespace"]
        == "agents-old"
    )
    assert (
        k8s_core_api._services[f"session-{THREAD_ID}"]["metadata"]["namespace"]
        == "agents-old"
    )
    assert (
        k8s_networking_api._ingresses[f"session-{THREAD_ID}"]["metadata"]["namespace"]
        == "agents-old"
    )


@pytest.mark.asyncio
async def test_legacy_namespace_refuses_current_namespace_route_shadow(
    svc, db, k8s_core_api, k8s_networking_api, monkeypatch
):
    monkeypatch.setenv("PINNED_LEGACY_AGENT_NAMESPACES", "agents-old")
    db.get_pinned_session_binding.return_value = _binding(pod_namespace="agents-old")
    k8s_core_api._pod["metadata"]["namespace"] = "agents-old"
    name = f"session-{THREAD_ID}"
    shadow = svc._service_body(
        THREAD_ID,
        name,
        POD_NAME,
        POD_UID,
        RUNTIME_GENERATION,
        namespace="srw",
    )
    shadow["metadata"]["uid"] = "shadow-service-uid"
    k8s_core_api._services[name] = shadow

    with pytest.raises(
        SessionRouteAuthorityError,
        match="shadow exists outside authoritative namespace",
    ):
        await svc.ensure_route(THREAD_ID, POD_NAME, POD_UID, RUNTIME_GENERATION)

    k8s_core_api.patch_namespaced_pod.assert_not_called()
    k8s_core_api.create_namespaced_service.assert_not_called()
    k8s_networking_api.create_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_namespace_refuses_other_allowlisted_route_shadow(
    svc, db, k8s_core_api, k8s_networking_api, monkeypatch
):
    monkeypatch.setenv(
        "PINNED_LEGACY_AGENT_NAMESPACES",
        "agents-old,agents-older",
    )
    db.get_pinned_session_binding.return_value = _binding(pod_namespace="agents-old")
    k8s_core_api._pod["metadata"]["namespace"] = "agents-old"
    name = f"session-{THREAD_ID}"
    shadow = svc._service_body(
        THREAD_ID,
        name,
        POD_NAME,
        POD_UID,
        RUNTIME_GENERATION,
        namespace="agents-older",
    )
    shadow["metadata"]["uid"] = "shadow-service-uid"

    def read_service(*, name, namespace, **_kwargs):
        if namespace == "agents-older":
            return deepcopy(shadow)
        raise _not_found()

    k8s_core_api.read_namespaced_service.side_effect = read_service

    with pytest.raises(
        SessionRouteAuthorityError,
        match="shadow exists outside authoritative namespace",
    ):
        await svc.ensure_route(THREAD_ID, POD_NAME, POD_UID, RUNTIME_GENERATION)

    k8s_core_api.patch_namespaced_pod.assert_not_called()
    k8s_core_api.create_namespaced_service.assert_not_called()
    k8s_networking_api.create_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_teardown_rejects_missing_namespace_authority(
    svc, k8s_core_api, k8s_networking_api
):
    with pytest.raises(SessionRouteAuthorityError, match="namespace authority"):
        await svc.teardown_route(
            THREAD_ID,
            expected_namespace="",
            expected_runtime_generation=RUNTIME_GENERATION,
            expected_owner_uid=POD_UID,
        )

    k8s_core_api.delete_namespaced_service.assert_not_called()
    k8s_networking_api.delete_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_recycled_pod_uid_fails_before_any_mutation(
    svc, k8s_core_api, k8s_networking_api
):
    k8s_core_api._pod["metadata"]["uid"] = "pod-uid-successor"

    with pytest.raises(SessionRouteAuthorityError):
        await svc.ensure_route(THREAD_ID, POD_NAME, POD_UID, RUNTIME_GENERATION)

    k8s_core_api.patch_namespaced_pod.assert_not_called()
    k8s_core_api.create_namespaced_service.assert_not_called()
    k8s_networking_api.create_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_binding_change_after_pod_read_fails_before_patch(
    svc, db, k8s_core_api, k8s_networking_api
):
    db.get_pinned_session_binding.side_effect = [_binding(), None]

    with pytest.raises(SessionRouteAuthorityError):
        await svc.ensure_route(THREAD_ID, POD_NAME, POD_UID, RUNTIME_GENERATION)

    k8s_core_api.patch_namespaced_pod.assert_not_called()
    k8s_core_api.create_namespaced_service.assert_not_called()
    k8s_networking_api.create_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_existing_service_fails_before_pod_mutation(
    svc, k8s_core_api, k8s_networking_api
):
    name = f"session-{THREAD_ID}"
    foreign = svc._service_body(THREAD_ID, name, POD_NAME, POD_UID, RUNTIME_GENERATION)
    foreign["metadata"]["uid"] = "service-uid"
    foreign["spec"]["selector"]["srw.io/thread-id"] = "foreign-thread"
    k8s_core_api._services[name] = foreign

    with pytest.raises(SessionRouteAuthorityError):
        await svc.ensure_route(THREAD_ID, POD_NAME, POD_UID, RUNTIME_GENERATION)

    k8s_core_api.patch_namespaced_pod.assert_not_called()
    k8s_core_api.create_namespaced_service.assert_not_called()
    k8s_networking_api.create_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_exact_route_is_generation_adopted(
    svc, k8s_core_api, k8s_networking_api
):
    name = f"session-{THREAD_ID}"
    service = svc._service_body(THREAD_ID, name, POD_NAME, POD_UID, None)
    service["metadata"]["uid"] = "service-uid"
    service["spec"]["selector"].pop("srw.io/runtime-generation")
    ingress = svc._ingress_body(THREAD_ID, name, POD_NAME, POD_UID, None)
    ingress["metadata"]["uid"] = "ingress-uid"
    k8s_core_api._services[name] = service
    k8s_networking_api._ingresses[name] = ingress

    assert (
        await svc.ensure_route(THREAD_ID, POD_NAME, POD_UID, RUNTIME_GENERATION)
        == f"/p/{THREAD_ID}"
    )

    assert (
        k8s_core_api._services[name]["metadata"]["labels"]["srw.io/runtime-generation"]
        == RUNTIME_GENERATION
    )
    assert (
        k8s_networking_api._ingresses[name]["metadata"]["labels"][
            "srw.io/runtime-generation"
        ]
        == RUNTIME_GENERATION
    )


@pytest.mark.asyncio
async def test_exact_teardown_preserves_successor_generation(
    svc, k8s_core_api, k8s_networking_api
):
    name = f"session-{THREAD_ID}"
    successor_generation = "99999999-2222-4333-8444-555555555555"
    service = svc._service_body(
        THREAD_ID, name, POD_NAME, "successor-uid", successor_generation
    )
    service["metadata"]["uid"] = "service-successor"
    ingress = svc._ingress_body(
        THREAD_ID, name, POD_NAME, "successor-uid", successor_generation
    )
    ingress["metadata"]["uid"] = "ingress-successor"
    k8s_core_api._services[name] = service
    k8s_networking_api._ingresses[name] = ingress

    assert (
        await svc.teardown_route(
            THREAD_ID,
            expected_namespace="srw",
            expected_runtime_generation=RUNTIME_GENERATION,
            expected_owner_uid=POD_UID,
        )
        is True
    )
    k8s_core_api.delete_namespaced_service.assert_not_called()
    k8s_networking_api.delete_namespaced_ingress.assert_not_called()
