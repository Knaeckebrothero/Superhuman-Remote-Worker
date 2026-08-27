"""Tests for the session router service — Service + Ingress per session."""

from unittest.mock import MagicMock

import pytest

from orchestrator.services.session_router import SessionRouterService

RUNTIME_GENERATION = "11111111-2222-4333-8444-555555555555"


def _pod(uid: str = "pod-uid-1") -> MagicMock:
    pod = MagicMock()
    pod.metadata.uid = uid
    return pod


def _route_resource(
    *,
    uid: str,
    pod_uid: str = "pod-uid-1",
    runtime_generation: str = RUNTIME_GENERATION,
) -> MagicMock:
    resource = MagicMock()
    resource.metadata.uid = uid
    resource.metadata.labels = {
        "srw.io/thread-id": "t1",
        "srw.io/runtime-generation": runtime_generation,
    }
    owner = MagicMock()
    owner.uid = pod_uid
    resource.metadata.owner_references = [owner]
    return resource


@pytest.fixture
def k8s_core_api():
    """Mock CoreV1Api with read returning 404 (resource missing) by default."""
    api = MagicMock()
    from kubernetes.client.exceptions import ApiException

    api.read_namespaced_pod.return_value = _pod()
    api.read_namespaced_service.side_effect = ApiException(status=404)
    return api


@pytest.fixture
def k8s_networking_api():
    """Mock NetworkingV1Api with read returning 404 by default."""
    api = MagicMock()
    from kubernetes.client.exceptions import ApiException

    api.read_namespaced_ingress.side_effect = ApiException(status=404)
    return api


@pytest.fixture
def svc(k8s_core_api, k8s_networking_api):
    return SessionRouterService(
        namespace="srw",
        ingress_host="api.example.com",
        ingress_class="traefik",
        annotations={"foo": "bar"},
        core_api=k8s_core_api,
        networking_api=k8s_networking_api,
    )


@pytest.mark.asyncio
async def test_ensure_route_creates_service_and_ingress(
    svc, k8s_core_api, k8s_networking_api
):
    """ensure_route creates both Service and Ingress with correct labels and owner refs."""
    from kubernetes.client.exceptions import ApiException

    k8s_core_api.read_namespaced_service.side_effect = [
        ApiException(status=404),
        _route_resource(uid="service-uid"),
    ]
    k8s_networking_api.read_namespaced_ingress.side_effect = [
        ApiException(status=404),
        _route_resource(uid="ingress-uid"),
    ]
    prefix = await svc.ensure_route(
        thread_id="t1",
        pod_name="srw-agent-abc",
        pod_uid="pod-uid-1",
        runtime_generation=RUNTIME_GENERATION,
    )

    assert prefix == "/p/t1"
    # Pod label patched so the Service selector matches.
    k8s_core_api.patch_namespaced_pod.assert_called_once()
    patch_kwargs = k8s_core_api.patch_namespaced_pod.call_args.kwargs
    assert patch_kwargs["name"] == "srw-agent-abc"
    assert {
        "op": "add",
        "path": "/metadata/labels/srw.io~1thread-id",
        "value": "t1",
    } in patch_kwargs["body"]
    k8s_core_api.create_namespaced_service.assert_called_once()
    k8s_networking_api.create_namespaced_ingress.assert_called_once()

    svc_body = k8s_core_api.create_namespaced_service.call_args.kwargs["body"]
    assert svc_body["metadata"]["name"] == "session-t1"
    assert svc_body["metadata"]["labels"]["srw.io/thread-id"] == "t1"
    assert (
        svc_body["metadata"]["labels"]["srw.io/runtime-generation"]
        == RUNTIME_GENERATION
    )
    assert svc_body["metadata"]["ownerReferences"][0]["name"] == "srw-agent-abc"
    assert svc_body["metadata"]["ownerReferences"][0]["uid"] == "pod-uid-1"
    assert svc_body["spec"]["selector"] == {
        "srw.io/thread-id": "t1",
        "srw.io/runtime-generation": RUNTIME_GENERATION,
    }

    ing_body = k8s_networking_api.create_namespaced_ingress.call_args.kwargs["body"]
    assert ing_body["metadata"]["name"] == "session-t1"
    assert ing_body["spec"]["ingressClassName"] == "traefik"
    rule = ing_body["spec"]["rules"][0]
    assert rule["host"] == "api.example.com"
    path = rule["http"]["paths"][0]
    assert path["path"] == "/p/t1"
    assert path["backend"]["service"]["name"] == "session-t1"


@pytest.mark.asyncio
async def test_ensure_route_idempotent_when_resources_exist(
    svc, k8s_core_api, k8s_networking_api
):
    """If both resources already exist, ensure_route is a no-op."""
    # Override the 404: now reads succeed
    k8s_core_api.read_namespaced_service.side_effect = None
    k8s_core_api.read_namespaced_service.return_value = _route_resource(
        uid="service-uid"
    )
    k8s_networking_api.read_namespaced_ingress.side_effect = None
    k8s_networking_api.read_namespaced_ingress.return_value = _route_resource(
        uid="ingress-uid"
    )

    await svc.ensure_route(
        thread_id="t1",
        pod_name="srw-agent-abc",
        pod_uid="pod-uid-1",
        runtime_generation=RUNTIME_GENERATION,
    )

    k8s_core_api.create_namespaced_service.assert_not_called()
    k8s_networking_api.create_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_teardown_route_deletes_both_resources(
    svc, k8s_core_api, k8s_networking_api
):
    """teardown_route deletes Service and Ingress; absent resources are OK."""
    k8s_core_api.read_namespaced_service.side_effect = None
    k8s_core_api.read_namespaced_service.return_value = _route_resource(
        uid="service-uid"
    )
    k8s_networking_api.read_namespaced_ingress.side_effect = None
    k8s_networking_api.read_namespaced_ingress.return_value = _route_resource(
        uid="ingress-uid"
    )

    assert await svc.teardown_route(thread_id="t1") is True
    k8s_core_api.delete_namespaced_service.assert_called_once()
    k8s_networking_api.delete_namespaced_ingress.assert_called_once()


@pytest.mark.asyncio
async def test_teardown_route_swallows_404(svc, k8s_core_api, k8s_networking_api):
    """Deleting a missing resource is not an error."""
    assert await svc.teardown_route(thread_id="t1") is True
    k8s_core_api.delete_namespaced_service.assert_not_called()
    k8s_networking_api.delete_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_route_tolerates_409_on_race(
    svc, k8s_core_api, k8s_networking_api
):
    """If a concurrent writer beats us to creating the resource, the create
    call returns 409. ensure_route must treat that as success, not raise."""
    from kubernetes.client.exceptions import ApiException

    # Reads still 404 (we think it doesn't exist).
    # Creates return 409 (a racing writer created it just now).
    k8s_core_api.create_namespaced_service.side_effect = ApiException(status=409)
    k8s_networking_api.create_namespaced_ingress.side_effect = ApiException(status=409)
    k8s_core_api.read_namespaced_service.side_effect = [
        ApiException(status=404),
        _route_resource(uid="service-uid"),
    ]
    k8s_networking_api.read_namespaced_ingress.side_effect = [
        ApiException(status=404),
        _route_resource(uid="ingress-uid"),
    ]

    # Must not raise.
    prefix = await svc.ensure_route(
        thread_id="t1",
        pod_name="srw-agent-abc",
        pod_uid="pod-uid-1",
        runtime_generation=RUNTIME_GENERATION,
    )
    assert prefix == "/p/t1"
