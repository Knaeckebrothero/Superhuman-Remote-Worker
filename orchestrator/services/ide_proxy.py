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

    async def resolve_pod_ip(self, job_id: str) -> Optional[str]:
        """Resolve job_id to the pod IP running code-server.

        Uses a per-job TTL cache to avoid per-request DB queries.
        Checks (in order):
          1. ide_session.pod_ip  (restored IDE sessions)
          2. workspace_container.pod_ip  (live workspace containers)
          3. vm.ssh_host / vm.pod_ip  (live VMs)

        Returns:
            Pod IP string, or None if not resolvable.
        """
        # Check cache
        cached = self._pod_ip_cache.get(job_id)
        if cached:
            pod_ip, expires_at = cached
            if time.monotonic() < expires_at:
                return pod_ip
            del self._pod_ip_cache[job_id]

        # DB lookup
        if not self._db:
            return None

        job = await self._db.get_job(job_id)
        if not job:
            return None

        ctx = job.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, TypeError):
                return None

        pod_ip = None

        # 1. Restored IDE session
        ide_ctx = ctx.get("ide_session", {})
        if ide_ctx.get("status") in ("active", "idle") and ide_ctx.get("pod_ip"):
            pod_ip = ide_ctx["pod_ip"]

        # 2. Live workspace container
        if not pod_ip:
            ws_ctx = ctx.get("workspace_container", {})
            if ws_ctx.get("status") == "ready" and ws_ctx.get("pod_ip"):
                pod_ip = ws_ctx["pod_ip"]

        # 3. Live VM
        if not pod_ip:
            vm_ctx = ctx.get("vm", {})
            if vm_ctx.get("status") == "ready":
                pod_ip = vm_ctx.get("ssh_host") or vm_ctx.get("pod_ip")

        if pod_ip:
            self._pod_ip_cache[job_id] = (pod_ip, time.monotonic() + self._cache_ttl)

        return pod_ip

    def evict(self, job_id: str) -> None:
        """Remove a job from the cache (call on upstream connection failure)."""
        self._pod_ip_cache.pop(job_id, None)


# Module-level singleton
ide_proxy_service = IdeProxyService()
