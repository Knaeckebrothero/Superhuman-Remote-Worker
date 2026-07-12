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
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OVERLAY_OK = "__SRW_OVERLAY_OK__"
_OVERLAY_FAILED = "__SRW_OVERLAY_FAILED__"
_OVERLAY_DEAD = "__SRW_OVERLAY_DEAD__"


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

    def refresh(self, refresh_lower: Callable[[], None]) -> None:
        """Refresh the frozen lower without losing the upperdir (design §11.2).

        CALLER MUST QUIESCE FIRST: held FDs across an unmount get silent stale
        reads. Sequence: PLAIN-unmount the overlay (``fusermount3 -u`` EBUSYs
        while an FD is held, so an un-quiesced agent surfaces as an
        OverlayMountError instead of silent staleness — never ``-uz`` here) →
        refresh the rclone lower (callback) → remount the overlay. The upperdir
        is untouched throughout; the workspace/cloud symlink already points at
        the merged path from the initial mount, so no symlink work is needed.
        """
        self._run("overlay_pre_refresh_unmount.sh", self._plain_unmount_script(), timeout=60)
        refresh_lower()
        self._run("overlay_remount.sh", self._mount_body_only_script(), timeout=120)

    def health_check(self) -> bool:
        """True when the merged view is readable; False on ENOTCONN (dead lower).

        A readdir over the merged view is the only reliable liveness signal —
        /proc/mounts and ``mountpoint -q`` both report "mounted" over a dead
        rclone endpoint (design §11.2)."""
        out = self._run("overlay_probe.sh", self._probe_script(), timeout=30, require_ok=False)
        if _OVERLAY_DEAD in out:
            return False
        return _OVERLAY_OK in out

    def upperdir_usage_bytes(self) -> int:
        """Best-effort ``du -sb`` probe of the upperdir (design §7/§9.9).

        Runs with ``require_ok=False``: a probe failure (e.g. du missing, path
        gone) must never raise and block the caller — it degrades to 0 so
        ``over_quota``/``quota_guard_message`` fail open rather than wedging
        the shell preflight on an unrelated probe error.
        """
        out = self._run("overlay_usage.sh", self._usage_script(), timeout=30, require_ok=False)
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("__SRW_"):
                continue
            head = line.split()[0]
            if head.isdigit():
                return int(head)
        return 0

    def over_quota(self) -> bool:
        quota = int(self.cfg.get("quota_bytes") or 0)
        return bool(quota) and self.upperdir_usage_bytes() >= quota

    def quota_guard_message(self) -> str | None:
        """Mirrors ``RcloneMountManager.cache_limit_message``: block message or None."""
        quota = int(self.cfg.get("quota_bytes") or 0)
        if not quota:
            return None
        used = self.upperdir_usage_bytes()
        if used < quota:
            return None
        return (
            "Cloud staging guard: this write was blocked because the protected-"
            "mode staging area (upperdir) has reached its cap.\n"
            f"Used {used} bytes of {quota} allowed.\n\n"
            "Staged changes are held locally until you review/apply them. Apply "
            "or reject the pending cloud diff to free space, or ask the operator "
            "to raise the cap."
        )

    def heal(self, remount_lower: Callable[[], None]) -> None:
        """Recover a dead rclone lower under a live overlay (design §11.2).

        Lazy-unmount the overlay FIRST (safe here: the dead lower makes every
        held-FD read fail loudly with ENOTCONN — no silent-staleness window),
        remount the lower via the callback, then remount the overlay."""
        self._run("overlay_heal_unmount.sh", self._heal_unmount_script(), timeout=60, require_ok=False)
        remount_lower()
        self._run("overlay_remount.sh", self._mount_body_only_script(), timeout=120)

    def reset_upper(self, refresh_lower: Callable[[], None]) -> None:
        """Discard the staged upperdir after an apply/reject and remount with a
        FRESH workdir (a workdir must never be reused across overlay instances
        — design §11.2, fresh-workdir-per-epoch).

        Unmount (plain first, lazy fallback — the overlay is being torn down
        either way, so unlike ``refresh``'s FD-hold quiesce guard there is no
        reason to insist on a clean plain unmount here) → wipe AND recreate
        both upperdir and workdir → refresh the lower (callback) → remount.
        Never touches the merged mountpoint or the lower directory itself."""
        self._run(
            "overlay_reset_unmount.sh",
            self._reset_unmount_script(),
            timeout=60,
            require_ok=False,
        )
        self._run("overlay_wipe_upper.sh", self._wipe_upper_script(), timeout=60)
        refresh_lower()
        self._run("overlay_remount.sh", self._mount_body_only_script(), timeout=120)

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

    def _plain_unmount_script(self) -> str:
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set -euo pipefail
trap 'rc=$?; echo "{_OVERLAY_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR
# PLAIN unmount — EBUSYs while an FD is held (the quiesce guard, design §11.2).
fusermount3 -u {merged} || fusermount -u {merged}
echo "{_OVERLAY_OK}"
"""

    def _mount_body_only_script(self) -> str:
        """Remount the overlay over the (refreshed) lower; no symlink work —
        the symlink already points at the merged path from the initial mount."""
        upper = shlex.quote(self.upper)
        work = shlex.quote(self.work)
        merged = shlex.quote(self.merged)
        lower = shlex.quote(self.lower)
        opts = shlex.quote(
            f"lowerdir={self.lower},upperdir={self.upper},workdir={self.work}"
        )
        return f"""#!/usr/bin/env bash
set -euo pipefail
trap 'rc=$?; echo "{_OVERLAY_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR
mkdir -p {upper} {work} {merged}
if ! mountpoint -q {lower}; then echo "{_OVERLAY_FAILED} rc=2 (lower not mounted)"; exit 2; fi
fuse-overlayfs -o {opts} {merged}
mountpoint -q {merged}
echo "{_OVERLAY_OK}"
"""

    def _probe_script(self) -> str:
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set +e
# A readdir returning ENOTCONN means the rclone lower died under us.
ls {merged} >/tmp/.srw-overlay-probe 2>/tmp/.srw-overlay-probe.err
rc=$?
if grep -qi 'not connected\\|ENOTCONN\\|Transport endpoint' /tmp/.srw-overlay-probe.err 2>/dev/null; then
  echo "{_OVERLAY_DEAD} ENOTCONN"
  exit 0
fi
if [ "$rc" -ne 0 ]; then echo "{_OVERLAY_DEAD} rc=$rc"; exit 0; fi
echo "{_OVERLAY_OK}"
"""

    def _heal_unmount_script(self) -> str:
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set +e
# LAZY unmount is correct on heal (dead lower => held reads ENOTCONN loudly).
fusermount3 -uz {merged} 2>/dev/null || fusermount -uz {merged} 2>/dev/null
echo "{_OVERLAY_OK}"
"""

    def _reset_unmount_script(self) -> str:
        """PLAIN unmount first; LAZY as fallback. No FD-hold quiesce guard is
        needed here (unlike ``refresh``'s plain-only unmount) — the overlay is
        being discarded and rebuilt either way, so a lazy fallback is safe."""
        merged = shlex.quote(self.merged)
        return f"""#!/usr/bin/env bash
set +e
fusermount3 -u {merged} 2>/dev/null || fusermount3 -uz {merged} 2>/dev/null || fusermount -u {merged} 2>/dev/null || fusermount -uz {merged} 2>/dev/null
echo "{_OVERLAY_OK}"
"""

    def _wipe_upper_script(self) -> str:
        """Wipe BOTH upperdir and workdir and recreate them fresh — a workdir
        must never be reused across overlay instances (design §11.2,
        fresh-workdir-per-epoch). Deliberately never references the merged
        mountpoint or the lower — only ``upper``/``work``."""
        upper = shlex.quote(self.upper)
        work = shlex.quote(self.work)
        return f"""#!/usr/bin/env bash
set -euo pipefail
trap 'rc=$?; echo "{_OVERLAY_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR
rm -rf {upper} {work}
mkdir -p {upper} {work}
echo "{_OVERLAY_OK}"
"""

    def _usage_script(self) -> str:
        upper = shlex.quote(self.upper)
        return f"""#!/usr/bin/env bash
set +e
du -sb {upper} 2>/dev/null
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
