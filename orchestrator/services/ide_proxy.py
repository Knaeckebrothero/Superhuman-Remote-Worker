"""IDE Proxy Service — Pod IP resolution and caching for code-server reverse proxy.

The orchestrator proxies HTTP and WebSocket traffic from the browser to
code-server running inside workspace pods.  This service resolves a job ID
to the pod IP where code-server is listening, with a short TTL cache to
avoid a DB round-trip on every sub-request (a single code-server page load
triggers ~50 requests in <2 seconds).
"""

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class IdeProxyService:
    """Resolves job_id → pod_ip for the IDE reverse proxy endpoints."""

    def __init__(self) -> None:
        self._db: Any = None
        self._pod_ip_cache: dict[str, tuple[str, float]] = {}
        self._cache_ttl: float = 30.0  # seconds

    def connect(self, db: Any) -> None:
        self._db = db
        logger.info("IDE proxy service initialized")

    async def resolve_pod_ip(self, entity_id: str) -> Optional[str]:
        """Resolve a job or thread ID to the pod IP running code-server.

        Uses a per-entity TTL cache to avoid per-request DB queries.
        Checks jobs first, then falls back to threads (persistent agents).

        For each entity, checks (in order):
          1. ide_session.pod_ip  (restored IDE sessions)
          2. workspace_container.pod_ip  (live workspace containers)
          3. vm.ssh_host / vm.pod_ip  (live VMs)

        Returns:
            Pod IP string, or None if not resolvable.
        """
        # Check cache
        cached = self._pod_ip_cache.get(entity_id)
        if cached:
            pod_ip, expires_at = cached
            if time.monotonic() < expires_at:
                return pod_ip
            del self._pod_ip_cache[entity_id]

        # DB lookup
        if not self._db:
            return None

        # Try jobs first, then threads
        ctx = await self._load_context(entity_id)
        if ctx is None:
            return None

        pod_ip = self._extract_pod_ip(ctx)

        if pod_ip:
            self._pod_ip_cache[entity_id] = (pod_ip, time.monotonic() + self._cache_ttl)

        return pod_ip

    async def _load_context(self, entity_id: str) -> Optional[dict]:
        """Load context dict from jobs table, falling back to threads."""
        job = await self._db.get_job(entity_id)
        if job:
            ctx = job.get("context") or {}
        else:
            thread = await self._db.get_thread(entity_id)
            if not thread:
                return None
            ctx = thread.get("metadata") or {}

        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, TypeError):
                return None
        return ctx

    @staticmethod
    def _extract_pod_ip(ctx: dict) -> Optional[str]:
        """Extract pod IP from a context/metadata dict."""
        # 1. Restored IDE session
        ide_ctx = ctx.get("ide_session", {})
        if ide_ctx.get("status") in ("active", "idle") and ide_ctx.get("pod_ip"):
            return ide_ctx["pod_ip"]

        # 2. Live workspace container (K8s pod or Docker Compose)
        ws_ctx = ctx.get("workspace_container", {})
        if ws_ctx.get("status") == "ready":
            if ws_ctx.get("pod_ip"):
                return ws_ctx["pod_ip"]
            # Docker Compose: code-server exposed on host via mapped port
            if ws_ctx.get("ide_host"):
                ide_port = ws_ctx.get("ide_port", 8080)
                return f"{ws_ctx['ide_host']}:{ide_port}"

        # 3. Live VM — prefer pod_ip (cluster-internal, reachable from orchestrator)
        #    over ssh_host (Tailscale IP, only reachable from mesh nodes)
        vm_ctx = ctx.get("vm", {})
        if vm_ctx.get("status") == "ready":
            return vm_ctx.get("pod_ip") or vm_ctx.get("ssh_host")

        return None

    def evict(self, entity_id: str) -> None:
        """Remove a job or thread from the cache (call on upstream connection failure)."""
        self._pod_ip_cache.pop(entity_id, None)


# Module-level singleton
ide_proxy_service = IdeProxyService()
