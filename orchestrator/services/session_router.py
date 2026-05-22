"""Per-session K8s Service + Ingress lifecycle.

For each bound agent pod, the orchestrator creates one Service (selects the
pod by its `srw.io/thread-id` label) and one Ingress (path-based,
`/p/{thread_id}`, points at the Service). Both resources carry
``ownerReferences`` to the agent pod so K8s GC cleans them up if explicit
teardown is skipped (orchestrator crash, etc.).

See `docs/features/direct_session_websockets.md` §Component details.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.exceptions import ApiException

    KUBERNETES_AVAILABLE = True
except ImportError:
    k8s_client = None  # type: ignore[assignment]
    k8s_config = None  # type: ignore[assignment]
    ApiException = Exception  # type: ignore[misc, assignment]  # fallback so isinstance/raise still work
    KUBERNETES_AVAILABLE = False

logger = logging.getLogger(__name__)


class SessionRouterService:
    """Idempotent K8s Service + Ingress lifecycle for sessions."""

    def __init__(
        self,
        namespace: str,
        ingress_host: str,
        ingress_class: str = "traefik",
        annotations: Optional[dict[str, str]] = None,
        # Injected for testability; lazy-resolved in production.
        core_api: Any = None,
        networking_api: Any = None,
    ) -> None:
        self._namespace = namespace
        self._ingress_host = ingress_host
        self._ingress_class = ingress_class
        self._annotations = annotations or {}
        self._core_api = core_api
        self._networking_api = networking_api

    # --------------------------------------------------------------------- #
    # Lazy K8s client setup
    # --------------------------------------------------------------------- #

    def _lazy_init_apis(self) -> None:
        if self._core_api is not None and self._networking_api is not None:
            return
        if not KUBERNETES_AVAILABLE:
            raise RuntimeError(
                "session_router requires the 'kubernetes' package; "
                "set it in orchestrator/requirements.txt or inject test APIs."
            )
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        if self._core_api is None:
            self._core_api = k8s_client.CoreV1Api()
        if self._networking_api is None:
            self._networking_api = k8s_client.NetworkingV1Api()

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #

    async def ensure_route(
        self,
        thread_id: str,
        pod_name: str,
        pod_uid: str,
    ) -> str:
        """Create Service + Ingress if missing. Returns the path prefix."""
        self._lazy_init_apis()
        name = f"session-{thread_id}"

        # Service
        if not await self._exists(self._core_api.read_namespaced_service, name):
            try:
                await self._call(
                    self._core_api.create_namespaced_service,
                    namespace=self._namespace,
                    body=self._service_body(thread_id, name, pod_name, pod_uid),
                )
            except ApiException as e:
                if e.status != 409:
                    raise
                # 409 = a racing writer beat us. The resource exists, we're done.

        # Ingress
        if not await self._exists(self._networking_api.read_namespaced_ingress, name):
            try:
                await self._call(
                    self._networking_api.create_namespaced_ingress,
                    namespace=self._namespace,
                    body=self._ingress_body(thread_id, name, pod_name, pod_uid),
                )
            except ApiException as e:
                if e.status != 409:
                    raise
                # 409 = a racing writer beat us. The resource exists, we're done.

        return f"/p/{thread_id}"

    async def teardown_route(self, thread_id: str) -> None:
        """Delete Service + Ingress. 404 is OK."""
        self._lazy_init_apis()
        name = f"session-{thread_id}"

        for delete_fn in (
            self._networking_api.delete_namespaced_ingress,
            self._core_api.delete_namespaced_service,
        ):
            try:
                await self._call(delete_fn, name=name, namespace=self._namespace)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(
                        "teardown_route: %s on %s returned %s",
                        delete_fn.__name__,
                        name,
                        e.status,
                    )

    # --------------------------------------------------------------------- #
    # Helpers
    # --------------------------------------------------------------------- #

    async def _exists(self, read_fn: Any, name: str) -> bool:
        try:
            await self._call(read_fn, name=name, namespace=self._namespace)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    @staticmethod
    async def _call(fn: Any, **kwargs: Any) -> Any:
        # kubernetes client is sync; run in executor to keep the loop free.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(**kwargs))

    def _owner_ref(self, pod_name: str, pod_uid: str) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "name": pod_name,
            "uid": pod_uid,
            "controller": False,
            "blockOwnerDeletion": False,
        }

    def _service_body(
        self, thread_id: str, name: str, pod_name: str, pod_uid: str
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {
                    "srw.io/thread-id": thread_id,
                    "srw.io/managed-by": "orchestrator",
                },
                "ownerReferences": [self._owner_ref(pod_name, pod_uid)],
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {"srw.io/thread-id": thread_id},
                "ports": [{"port": 8001, "targetPort": 8001}],
            },
        }

    def _ingress_body(
        self, thread_id: str, name: str, pod_name: str, pod_uid: str
    ) -> dict[str, Any]:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {
                    "srw.io/thread-id": thread_id,
                    "srw.io/managed-by": "orchestrator",
                },
                "annotations": dict(self._annotations),
                "ownerReferences": [self._owner_ref(pod_name, pod_uid)],
            },
            "spec": {
                "ingressClassName": self._ingress_class,
                "rules": [
                    {
                        "host": self._ingress_host,
                        "http": {
                            "paths": [
                                {
                                    "path": f"/p/{thread_id}",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": name,
                                            "port": {"number": 8001},
                                        }
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        }
