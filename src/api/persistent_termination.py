"""Kubernetes preStop helper for dedicated persistent-agent pods.

Kubernetes sets ``deletionTimestamp`` before it starts a container's preStop
hook, but that state is not synchronously visible inside the process.  The pod
manifest therefore runs this helper at the first in-container termination
boundary.  A shell built-in creates the sentinel before Python starts; this
module repeats that idempotent write and asks the live runtime to wait for its
current turn boundary.

The helper deliberately carries no credential.  Its HTTP endpoint accepts
loopback callers only and the sentinel is on the pod-private ``/tmp`` volume.
Abrupt node loss can skip preStop entirely; transcript/tool-pairing recovery is
the defence for that genuinely ungraceful case.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

TERMINATION_SENTINEL_PATH = "/tmp/srw-persistent-terminating"


def install_termination_sentinel() -> None:
    """Publish the process-local admission fence without logging payloads."""

    path = Path(TERMINATION_SENTINEL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def request_parked_boundary() -> bool:
    """Ask the runtime to hold preStop until its current turn is settled."""

    port = int(os.environ.get("AGENT_PORT", "8001"))
    drain_seconds = max(
        1.0,
        float(os.environ.get("PERSISTENT_TERMINATION_DRAIN_SECONDS", "165")),
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/lifecycle/termination-fence",
        data=json.dumps({"source": "kubernetes_prestop"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=drain_seconds + 5.0) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        # The sentinel is the admission fence.  The HTTP request only extends
        # the grace window so an already-running turn can settle.
        return False


def main() -> int:
    install_termination_sentinel()
    request_parked_boundary()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the pod hook
    raise SystemExit(main())
