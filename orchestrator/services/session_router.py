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


def _is_k8s_status(exc: BaseException, status: int) -> bool:
    """Duck-type a K8s API exception by `.status`.

    Catching by ``ApiException`` class fails under test isolation problems —
    if another test file replaces ``kubernetes.client.exceptions`` in
    ``sys.modules`` after this module captured ``ApiException``, the running
    code's class binding diverges from what tests raise. Comparing by the
    K8s-API-error-shape attribute sidesteps the class identity hazard.
    """
    return getattr(exc, "status", None) == status


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
        """Create Service + Ingress if missing. Patch the pod's thread-id
        label so the Service selector matches. Returns the path prefix."""
        self._lazy_init_apis()
        name = f"session-{thread_id}"

        # Stamp the pod's srw.io/thread-id label. The Service selector matches
        # this label; idle-pool agents that get attached via main._send_session_attach
        # are bound in DB but never get their K8s pod metadata updated, so the
        # Service ends up with zero endpoints. Patching here is idempotent —
        # same value is a no-op, and a no-longer-bound thread can claim the
        # pod next by overwriting the label.
        try:
            await self._call(
                self._core_api.patch_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                body={"metadata": {"labels": {"srw.io/thread-id": thread_id}}},
            )
        except Exception as e:
            if _is_k8s_status(e, 404):
                logger.warning(
                    "ensure_route: pod %s not found for thread %s — skipping label patch",
                    pod_name,
                    thread_id,
                )
            else:
                raise

        # Service
        if not await self._exists(self._core_api.read_namespaced_service, name):
            try:
                await self._call(
                    self._core_api.create_namespaced_service,
                    namespace=self._namespace,
                    body=self._service_body(thread_id, name, pod_name, pod_uid),
                )
            except Exception as e:
                # 409 = a racing writer beat us; the resource exists. Anything
                # else propagates. Duck-type on `.status` rather than the
                # ApiException class — see _is_k8s_status docstring.
                if not _is_k8s_status(e, 409):
                    raise

        # Ingress
        if not await self._exists(self._networking_api.read_namespaced_ingress, name):
            try:
                await self._call(
                    self._networking_api.create_namespaced_ingress,
                    namespace=self._namespace,
                    body=self._ingress_body(thread_id, name, pod_name, pod_uid),
                )
            except Exception as e:
                if not _is_k8s_status(e, 409):
                    raise

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
            except Exception as e:
                if _is_k8s_status(e, 404):
                    continue
                logger.warning(
                    "teardown_route: %s on %s returned %s",
                    delete_fn.__name__,
                    name,
                    getattr(e, "status", "?"),
                )

    # --------------------------------------------------------------------- #
    # Helpers
    # --------------------------------------------------------------------- #

    async def _exists(self, read_fn: Any, name: str) -> bool:
        try:
            await self._call(read_fn, name=name, namespace=self._namespace)
            return True
        except Exception as e:
            if _is_k8s_status(e, 404):
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
                # The orchestrator is the authoritative readiness gate: it won't
                # mint a token / return 200 from /connection until probe_ready()
                # confirms the agent answers /ready on its pod IP. Gating the
                # Service *additionally* on the pod's k8s readinessProbe
                # (initialDelaySeconds=30) only opens a ~30s window where
                # /connection says "ready" but the Service has no endpoints, so
                # the cockpit's WS fails and reconnect-loops. Publishing
                # not-ready addresses closes that gap — traffic only arrives
                # after /connection 200 anyway.
                "publishNotReadyAddresses": True,
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
