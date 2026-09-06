# Orchestrator services
# Note: Database services have been moved to orchestrator.database
# Only workspace_service remains here (file-based, not DB)

import logging
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path


logger = logging.getLogger(__name__)

_ssh_key_stage_lock = threading.Lock()
_ssh_key_stage_dir: Path | None = None
_ssh_key_stage_state: tuple[str, int, int, int, str] | None = None


def _stage_runtime_ssh_key(source: str, source_stat: os.stat_result) -> str:
    """Copy a projected key to a runtime-owned ``0600`` file atomically.

    Kubernetes Secret volumes are root-owned. The chart deliberately projects
    this key as ``0444`` so the production image's non-root process can read it,
    but the root-running Tilt image then trips OpenSSH's owner permission check.
    A process-owned private copy is valid in both environments and also avoids
    relying on OpenSSH's special case for a key owned by another uid.
    """
    global _ssh_key_stage_dir, _ssh_key_stage_state

    signature = (
        source,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )
    with _ssh_key_stage_lock:
        if (
            _ssh_key_stage_state is not None
            and _ssh_key_stage_state[:4] == signature
            and os.path.isfile(_ssh_key_stage_state[4])
        ):
            return _ssh_key_stage_state[4]

        if _ssh_key_stage_dir is None:
            _ssh_key_stage_dir = Path(
                tempfile.mkdtemp(prefix=f"srw-ssh-{os.geteuid()}-")
            )
        else:
            _ssh_key_stage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(_ssh_key_stage_dir, 0o700)

        destination = _ssh_key_stage_dir / "workspace-identity"
        fd, temporary = tempfile.mkstemp(
            prefix=".workspace-identity-", dir=_ssh_key_stage_dir
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as target, open(source, "rb") as source_file:
                fd = -1
                shutil.copyfileobj(source_file, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

        resolved = str(destination)
        _ssh_key_stage_state = (*signature, resolved)
        logger.info("Staged workspace SSH identity at a runtime-owned private path")
        return resolved


def resolve_ssh_key_path() -> str:
    """Return the SSH private key path for workspace SSH connections.

    Resolution order:
      1. SSH_KEY_PATH env var (set by Docker provisioner in dev, or K8s deployment)
      2. /run/secrets/vm-ssh-key (K8s default mount path)

    A readable key that is not owned by this process with private permissions is
    staged to an atomic runtime-owned ``0600`` copy before its path is returned.
    This keeps both root-running Tilt and non-root production OpenSSH clients
    happy while the original projected Secret remains read-only.

    Returns an explicitly configured path unchanged if it does not exist (so
    callers retain their useful file-not-found diagnostics), or an empty string
    when neither an override nor the Kubernetes default is present.
    """
    path = os.environ.get("SSH_KEY_PATH", "").strip()
    if not path:
        default = "/run/secrets/vm-ssh-key"
        if not os.path.isfile(default):
            return ""
        path = default

    try:
        source_stat = os.stat(path)
    except OSError:
        return path

    private_runtime_file = source_stat.st_uid == os.geteuid() and not (
        stat.S_IMODE(source_stat.st_mode) & 0o077
    )
    if private_runtime_file:
        return path

    try:
        return _stage_runtime_ssh_key(path, source_stat)
    except OSError:
        logger.warning(
            "Could not stage workspace SSH identity; using projected path",
            exc_info=True,
        )
        return path
