"""Persistent Agent Provisioner — Pod lifecycle for interactive sessions.

Follows the VMProvisioner dual-backend pattern. For local development,
persistent agents are started manually via:
    python agent.py --mode persistent --thread-id <uuid>

In production, this provisioner creates K8s Pods on demand.

Status: Skeletal — K8s provisioning is not yet implemented.
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PersistentProvisioner:
    """Provisions persistent agent pods on demand.

    Follows the VMProvisioner pattern: dual-backend (direct K8s or manual),
    graceful degradation when K8s is not available.
    """

    def __init__(self) -> None:
        self._db: Optional[Any] = None
        self._k8s_available: bool = False
        self._namespace: str = os.environ.get(
            "AGENT_NAMESPACE", "superhuman-remote-worker"
        )
        self._agent_image: str = os.environ.get(
            "PERSISTENT_AGENT_IMAGE",
            "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent:latest",
        )

    @property
    def is_available(self) -> bool:
        """Whether K8s provisioning is available."""
        return self._k8s_available

    @property
    def mode(self) -> Optional[str]:
        """Current provisioning mode."""
        if self._k8s_available:
            return "k8s"
        return None

    def connect(self, db: Any) -> None:
        """Initialize provisioner with database connection.

        Args:
            db: PostgresDB instance for thread/agent tracking
        """
        self._db = db
        self._init_k8s()

    def _init_k8s(self) -> None:
        """Try to initialize K8s client."""
        try:
            from kubernetes import client, config as k8s_config  # noqa: F401 (client used after TODOs implemented)

            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                try:
                    k8s_config.load_kube_config()
                except k8s_config.ConfigException:
                    logger.info(
                        "K8s not available — persistent agents must be started manually"
                    )
                    return

            self._k8s_available = True
            logger.info(
                f"PersistentProvisioner initialized (K8s, namespace={self._namespace})"
            )
        except ImportError:
            logger.info(
                "kubernetes package not installed — persistent agents must be started manually"
            )

    async def create_agent_pod(
        self,
        thread_id: str,
        config_name: str = "defaults",
        cpu_request: str = "500m",
        memory_request: str = "1Gi",
        memory_limit: str = "4Gi",
    ) -> bool:
        """Create a K8s pod running a persistent agent.

        Args:
            thread_id: Thread UUID
            config_name: Agent config to use
            cpu_request: CPU request
            memory_request: Memory request
            memory_limit: Memory limit

        Returns:
            True if pod was created, False otherwise
        """
        if not self._k8s_available:
            logger.info(
                f"K8s not available — start agent manually: "
                f"python agent.py --mode persistent --thread-id {thread_id} "
                f"--config {config_name}"
            )
            return False

        # TODO: Implement K8s pod creation
        # Pod spec should mirror deployment/21-agent.yaml but with:
        # - args: ["--mode", "persistent", "--thread-id", thread_id]
        # - restartPolicy: Never
        # - labels: app=srw-persistent, thread-id={thread_id}
        logger.warning("K8s pod creation not yet implemented")
        return False

    async def delete_agent_pod(self, thread_id: str) -> bool:
        """Delete the pod for a persistent session.

        Args:
            thread_id: Thread UUID

        Returns:
            True if pod was deleted, False otherwise
        """
        if not self._k8s_available:
            return False

        # TODO: Implement K8s pod deletion
        logger.warning("K8s pod deletion not yet implemented")
        return False

    async def get_pod_status(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Query pod status for a thread.

        Args:
            thread_id: Thread UUID

        Returns:
            Status dict or None if not found
        """
        if not self._k8s_available:
            return None

        # TODO: Implement K8s pod status query
        return None


# Module-level singleton
persistent_provisioner = PersistentProvisioner()
