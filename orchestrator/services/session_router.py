"""Per-session K8s Service + Ingress lifecycle.

For each bound agent pod, the orchestrator creates one Service (selects the
pod by its `srw.io/thread-id` label) and one Ingress (path-based,
`/p/{thread_id}`, points at the Service). Both resources carry
``ownerReferences`` to the agent pod so K8s GC cleans them up if explicit
teardown is skipped (orchestrator crash, etc.).

See `knowledge-base/knowledge/features/direct_session_websockets.md` §Component details.
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
        tls_secret_name: Optional[str] = None,
        # Injected for testability; lazy-resolved in production.
        core_api: Any = None,
        networking_api: Any = None,
    ) -> None:
        self._namespace = namespace
        self._ingress_host = ingress_host
        self._ingress_class = ingress_class
        self._annotations = annotations or {}
        # Local dev needs the per-session Ingress to be on the same TLS
        # entrypoint as the cockpit (mkcert/cert-manager). When set, the
        # Ingress gets a `tls:` block + websecure entrypoint annotation.
        self._tls_secret_name = tls_secret_name
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
        runtime_generation: str | None = None,
    ) -> str:
        """Create Service + Ingress if missing. Patch the pod's thread-id
        label so the Service selector matches. Returns the path prefix."""
        self._lazy_init_apis()
        if not str(pod_uid or "").strip() or not str(runtime_generation or "").strip():
            raise RuntimeError("session route requires Pod UID and runtime generation")
        name = f"session-{thread_id}"

        # Stamp the pod's srw.io/thread-id label. The Service selector matches
        # this label; idle-pool agents that get attached via main._send_session_attach
        # are bound in DB but never get their K8s pod metadata updated, so the
        # Service ends up with zero endpoints. Patching here is idempotent —
        # same value is a no-op, and a no-longer-bound thread can claim the
        # pod next by overwriting the label.
        #
        # Also stamp the short-form srw/thread-id (lifecycle reconciler
        # selects on it) and flip srw/purpose to "session": a warm pool pod
        # is provisioned as purpose=job, and serving a session under a job
        # label breaks purpose-based selectors and dashboards
        # (knowledge-base/knowledge/issues/session_silent_failure_audit.md #16).
        try:
            pod = await self._call(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
            if str(getattr(getattr(pod, "metadata", None), "uid", "") or "") != str(
                pod_uid
            ):
                raise RuntimeError("session route Pod identity changed")
            await self._call(
                self._core_api.patch_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                body=[
                    {"op": "test", "path": "/metadata/uid", "value": pod_uid},
                    {
                        "op": "add",
                        "path": "/metadata/labels/srw.io~1thread-id",
                        "value": thread_id,
                    },
                    {
                        "op": "add",
                        "path": "/metadata/labels/srw~1thread-id",
                        "value": thread_id[:12],
                    },
                    {
                        "op": "add",
                        "path": "/metadata/labels/srw~1purpose",
                        "value": "session",
                    },
                    {
                        "op": "add",
                        "path": "/metadata/labels/srw.io~1runtime-generation",
                        "value": runtime_generation,
                    },
                ],
                _content_type="application/json-patch+json",
            )
        except Exception as e:
            if _is_k8s_status(e, 404):
                raise RuntimeError(
                    "session route Pod disappeared before exact binding"
                ) from e
            raise

        # Service
        service = await self._read_or_none(self._core_api.read_namespaced_service, name)
        if service is None:
            try:
                await self._call(
                    self._core_api.create_namespaced_service,
                    namespace=self._namespace,
                    body=self._service_body(
                        thread_id,
                        name,
                        pod_name,
                        pod_uid,
                        runtime_generation,
                    ),
                )
            except Exception as e:
                # 409 = a racing writer beat us; the resource exists. Anything
                # else propagates. Duck-type on `.status` rather than the
                # ApiException class — see _is_k8s_status docstring.
                if not _is_k8s_status(e, 409):
                    raise
            service = await self._read_or_none(
                self._core_api.read_namespaced_service, name
            )
            if service is None or not self._resource_matches_authority(
                service,
                pod_uid=pod_uid,
                runtime_generation=runtime_generation,
                require_generation=runtime_generation is not None,
            ):
                raise RuntimeError(
                    "session Service create lost runtime-generation authority"
                )

        elif not self._resource_matches_authority(
            service,
            pod_uid=pod_uid,
            runtime_generation=runtime_generation,
        ):
            raise RuntimeError("session Service belongs to another runtime generation")
        elif runtime_generation:
            await self._call(
                self._core_api.patch_namespaced_service,
                name=name,
                namespace=self._namespace,
                body=self._route_authority_patch(
                    service,
                    pod_name,
                    pod_uid,
                    runtime_generation,
                    service=True,
                ),
                _content_type="application/json-patch+json",
            )

        # Ingress
        ingress = await self._read_or_none(
            self._networking_api.read_namespaced_ingress, name
        )
        if ingress is None:
            try:
                await self._call(
                    self._networking_api.create_namespaced_ingress,
                    namespace=self._namespace,
                    body=self._ingress_body(
                        thread_id,
                        name,
                        pod_name,
                        pod_uid,
                        runtime_generation,
                    ),
                )
            except Exception as e:
                if not _is_k8s_status(e, 409):
                    raise
            ingress = await self._read_or_none(
                self._networking_api.read_namespaced_ingress, name
            )
            if ingress is None or not self._resource_matches_authority(
                ingress,
                pod_uid=pod_uid,
                runtime_generation=runtime_generation,
                require_generation=runtime_generation is not None,
            ):
                raise RuntimeError(
                    "session Ingress create lost runtime-generation authority"
                )

        elif not self._resource_matches_authority(
            ingress,
            pod_uid=pod_uid,
            runtime_generation=runtime_generation,
        ):
            raise RuntimeError("session Ingress belongs to another runtime generation")
        elif runtime_generation:
            await self._call(
                self._networking_api.patch_namespaced_ingress,
                name=name,
                namespace=self._namespace,
                body=self._route_authority_patch(
                    ingress,
                    pod_name,
                    pod_uid,
                    runtime_generation,
                    service=False,
                ),
                _content_type="application/json-patch+json",
            )

        return f"/p/{thread_id}"

    async def teardown_route(
        self,
        thread_id: str,
        *,
        expected_runtime_generation: str | None = None,
        expected_owner_uid: str | None = None,
    ) -> bool:
        """Delete only the captured route generation. 404 is exact success."""
        self._lazy_init_apis()
        name = f"session-{thread_id}"

        complete = True
        for read_fn, delete_fn in (
            (
                self._networking_api.read_namespaced_ingress,
                self._networking_api.delete_namespaced_ingress,
            ),
            (
                self._core_api.read_namespaced_service,
                self._core_api.delete_namespaced_service,
            ),
        ):
            try:
                resource = await self._call(
                    read_fn, name=name, namespace=self._namespace
                )
            except Exception as e:
                if _is_k8s_status(e, 404):
                    continue
                complete = False
                continue
            if (
                expected_runtime_generation is not None
                or expected_owner_uid is not None
            ) and not self._resource_matches_authority(
                resource,
                pod_uid=expected_owner_uid,
                runtime_generation=expected_runtime_generation,
                require_generation=expected_runtime_generation is not None,
            ):
                # A replacement owns this deterministic name. Preserve it.
                continue
            resource_uid = str(
                getattr(getattr(resource, "metadata", None), "uid", "") or ""
            )
            if not resource_uid:
                complete = False
                continue
            try:
                await self._call(
                    delete_fn,
                    name=name,
                    namespace=self._namespace,
                    body={"preconditions": {"uid": resource_uid}},
                )
            except Exception as e:
                if _is_k8s_status(e, 404) or _is_k8s_status(e, 409):
                    continue
                logger.warning(
                    "teardown_route: %s on %s returned %s",
                    delete_fn.__name__,
                    name,
                    getattr(e, "status", "?"),
                )
                complete = False
        return complete

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

    async def _read_or_none(self, read_fn: Any, name: str) -> Any | None:
        try:
            return await self._call(read_fn, name=name, namespace=self._namespace)
        except Exception as exc:
            if _is_k8s_status(exc, 404):
                return None
            raise

    @staticmethod
    def _resource_matches_authority(
        resource: Any,
        *,
        pod_uid: str | None,
        runtime_generation: str | None,
        require_generation: bool = False,
    ) -> bool:
        metadata = getattr(resource, "metadata", None)
        labels = dict(getattr(metadata, "labels", None) or {})
        owners = list(getattr(metadata, "owner_references", None) or [])
        owner_uids = {
            str(getattr(owner, "uid", "") or "")
            if not isinstance(owner, dict)
            else str(owner.get("uid") or "")
            for owner in owners
        }
        if pod_uid and str(pod_uid) not in owner_uids:
            return False
        observed_generation = labels.get("srw.io/runtime-generation")
        if runtime_generation:
            if observed_generation not in {None, "", runtime_generation}:
                return False
            if require_generation and observed_generation != runtime_generation:
                return False
        return True

    def _route_authority_patch(
        self,
        resource: Any,
        pod_name: str,
        pod_uid: str,
        runtime_generation: str,
        *,
        service: bool,
    ) -> list[dict[str, Any]]:
        """UID-fenced route adoption/refresh patch.

        Deterministic resource names are reusable across Resume.  A normal
        merge patch can therefore read G1 and overwrite replacement G2 after
        a delete/create race.  JSON Patch's immutable UID test makes that
        interleaving fail without mutating the replacement.
        """

        metadata = getattr(resource, "metadata", None)
        resource_uid = str(getattr(metadata, "uid", "") or "")
        if not resource_uid:
            raise RuntimeError("session route resource has no immutable UID")
        patch: list[dict[str, Any]] = [
            {"op": "test", "path": "/metadata/uid", "value": resource_uid},
            {
                "op": "add",
                "path": "/metadata/labels/srw.io~1runtime-generation",
                "value": runtime_generation,
            },
            {
                "op": "add",
                "path": "/metadata/ownerReferences",
                "value": [self._owner_ref(pod_name, pod_uid)],
            },
        ]
        if service:
            patch.append(
                {
                    "op": "add",
                    "path": "/spec/selector",
                    "value": {
                        "srw.io/thread-id": str(
                            (getattr(metadata, "labels", None) or {}).get(
                                "srw.io/thread-id", ""
                            )
                        ),
                        "srw.io/runtime-generation": runtime_generation,
                    },
                }
            )
        return patch

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
        self,
        thread_id: str,
        name: str,
        pod_name: str,
        pod_uid: str,
        runtime_generation: str | None = None,
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
                    **(
                        {"srw.io/runtime-generation": runtime_generation}
                        if runtime_generation
                        else {}
                    ),
                },
                "ownerReferences": [self._owner_ref(pod_name, pod_uid)],
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {
                    "srw.io/thread-id": thread_id,
                    "srw.io/runtime-generation": runtime_generation,
                },
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
        self,
        thread_id: str,
        name: str,
        pod_name: str,
        pod_uid: str,
        runtime_generation: str | None = None,
    ) -> dict[str, Any]:
        annotations = dict(self._annotations)
        spec: dict[str, Any] = {
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
        }
        if self._tls_secret_name:
            spec["tls"] = [
                {
                    "hosts": [self._ingress_host],
                    "secretName": self._tls_secret_name,
                }
            ]
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {
                    "srw.io/thread-id": thread_id,
                    "srw.io/managed-by": "orchestrator",
                    **(
                        {"srw.io/runtime-generation": runtime_generation}
                        if runtime_generation
                        else {}
                    ),
                },
                "annotations": annotations,
                "ownerReferences": [self._owner_ref(pod_name, pod_uid)],
            },
            "spec": spec,
        }
