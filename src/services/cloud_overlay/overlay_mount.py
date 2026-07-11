"""fuse-overlayfs capture overlay over a read-only rclone lower.

Sibling to ``src.services.cloud_mount.RcloneMountManager``: pure Python that
generates a bash script and runs it on the WORKSPACE pod/VM over the workspace
backend's SSH channel (never the agent pod). The overlay stacks a local
scratch upperdir (the staged diff) over the read-only rclone mount so every
agent write — shell included — is captured locally and the cloud is untouched
until an operator applies the diff (design §3.1/§3.2).

Layout is fixed by the snapshot placement rule (design §11.3): upperdir/workdir
INSIDE /home/agent-host (captured by snapshots), merged mountpoint + raw rclone
lower OUTSIDE it (not captured). The agent sees the merged view at
``workspace/cloud`` via a symlink this manager owns.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OVERLAY_OK = "__SRW_OVERLAY_OK__"
_OVERLAY_FAILED = "__SRW_OVERLAY_FAILED__"


class OverlayMountError(RuntimeError):
    """The capture overlay could not be mounted/refreshed/healed."""


class OverlayMountManager:
    def __init__(
        self,
        *,
        thread_id: str,
        overlay_cfg: dict[str, Any],
        workspace_backend: Any,
        workspace_root: Path | str,
    ) -> None:
        self.thread_id = thread_id
        self.cfg = dict(overlay_cfg)
        self.workspace_backend = workspace_backend
        self.workspace_root = str(
            getattr(workspace_backend, "root", None) or workspace_root
        )
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def lower(self) -> str:
        return str(self.cfg["lower"])

    @property
    def merged(self) -> str:
        return str(self.cfg["merged"])

    @property
    def upper(self) -> str:
        return str(self.cfg["upper"])

    @property
    def work(self) -> str:
        return str(self.cfg["work"])

    def mount(self) -> None:
        self._run("overlay_mount.sh", self._mount_script(), timeout=60)
        self._active = True
        logger.info(
            "capture overlay mounted: thread=%s merged=%s", self.thread_id, self.merged
        )

    def unmount(self) -> None:
        try:
            self._run(
                "overlay_unmount.sh", self._unmount_script(), timeout=45, require_ok=False
            )
        finally:
            self._active = False

    # --------------------------------------------------------------- scripts

    def _mount_script(self) -> str:
        lower = shlex.quote(self.lower)
        upper = shlex.quote(self.upper)
        work = shlex.quote(self.work)
        merged = shlex.quote(self.merged)
        workspace = shlex.quote(self.workspace_root)
        # Quote the WHOLE -o value: raw interpolation would word-split on paths
        # with spaces/metacharacters (B2 review finding).
        opts = shlex.quote(
            f"lowerdir={self.lower},upperdir={self.upper},workdir={self.work}"
        )
        return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077
trap 'rc=$?; echo "{_OVERLAY_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR

# The RO rclone lower MUST already be mounted; refuse otherwise (fail-closed).
if ! mountpoint -q {lower}; then
  echo "{_OVERLAY_FAILED} rc=2 (lower {self.lower} not mounted)"
  exit 2
fi

mkdir -p {upper} {work} {merged}

# Re-mount idempotently: tear a stale overlay down first (plain, then lazy).
if mountpoint -q {merged}; then
  fusermount3 -u {merged} 2>/dev/null || fusermount -u {merged} 2>/dev/null || true
fi
if mountpoint -q {merged}; then
  fusermount3 -uz {merged} 2>/dev/null || fusermount -uz {merged} 2>/dev/null || true
fi

fuse-overlayfs -o {opts} {merged}

if ! mountpoint -q {merged}; then
  echo "{_OVERLAY_FAILED} rc=3 (overlay did not mount)"
  exit 3
fi

# Point the agent's workspace/cloud at the MERGED view (not the raw lower).
workspace={workspace}
mkdir -p "${{workspace}}/.srw"
entry="${{workspace}}/cloud"
if [ -L "${{entry}}" ]; then rm "${{entry}}"; fi
if [ -e "${{entry}}" ] && [ ! -L "${{entry}}" ]; then
  mv "${{entry}}" "${{workspace}}/.srw/cloud.pre-overlay.$(date +%s)"
fi
ln -sfn {merged} "${{entry}}"

echo "{_OVERLAY_OK}"
"""

    def _unmount_script(self) -> str:
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set +e
if mountpoint -q {merged}; then
  fusermount3 -u {merged} 2>/dev/null || fusermount -u {merged} 2>/dev/null
fi
if mountpoint -q {merged}; then
  fusermount3 -uz {merged} 2>/dev/null || fusermount -uz {merged} 2>/dev/null
fi
echo "{_OVERLAY_OK}"
"""

    # ----------------------------------------------------------- remote exec

    def _run(self, name: str, script: str, *, timeout: int = 30, require_ok: bool = True) -> str:
        rel = f".cache/srw/overlay/{self.thread_id}/scripts/{name}"
        self.workspace_backend.write_home_file(rel, script)
        script_path = self.workspace_backend.resolve_home_path(rel)
        command = (
            f"chmod 700 {shlex.quote(script_path)} && bash {shlex.quote(script_path)}; "
            f"rc=$?; rm -f {shlex.quote(script_path)}; exit $rc"
        )
        output = self.workspace_backend.exec_command(command, timeout=timeout)
        if require_ok and _OVERLAY_OK not in output:
            raise OverlayMountError(f"{name} did not report OK:\n{output}")
        return output
