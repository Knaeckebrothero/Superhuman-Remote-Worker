# Orchestrator services
# Note: Database services have been moved to orchestrator.database
# Only workspace_service remains here (file-based, not DB)

import os


def resolve_ssh_key_path() -> str:
    """Return the SSH private key path for workspace SSH connections.

    Resolution order:
      1. SSH_KEY_PATH env var (set by Docker provisioner in dev, or K8s deployment)
      2. /run/secrets/vm-ssh-key (K8s default mount path)

    Returns empty string if no key file is found.
    """
    path = os.environ.get("SSH_KEY_PATH", "").strip()
    if path:
        return path
    default = "/run/secrets/vm-ssh-key"
    if os.path.isfile(default):
        return default
    return ""
