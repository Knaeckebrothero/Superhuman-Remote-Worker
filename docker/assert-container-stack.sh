#!/usr/bin/env bash
# =============================================================================
# Conformance gate for the VM workspace container stack (rootless podman).
# =============================================================================
#
# Asserted by docker/agent-vm-base/ (stage2) and installed permanently as
# /usr/local/bin/assert-container-stack so "can this VM run containers?" is
# answerable in five seconds on a live box instead of five weeks — the same
# reasoning as assert-browser-stack.sh, which exists because the VM shipped
# for ~5 weeks with a capability no agent could reach.
#
# Deliberately NOT shared with docker/Dockerfile.workspace, unlike the browser
# gate. The container (sandbox) tier CANNOT run containers: it has no runtime
# and sudo there is intercepted by the sudo gate. That asymmetry is the whole
# point of the VM tier, so this gate is VM-only by design. If the sandbox tier
# ever gains a runtime, move this file to the shared pattern.
#
# Most failures here are silent-degradation traps rather than hard breakage —
# a missing aardvark-dns starts a compose stack whose services cannot resolve
# each other, which reads as an application bug. Assert the parts, not just
# `podman --version`.
#
# Usage:
#   assert-container-stack.sh          static contract (build-time safe)
#   assert-container-stack.sh --run    also pull and run a real container
#                                      (needs a user session + network; use on
#                                      a live VM, not during the image build)
# =============================================================================
set -uo pipefail

# Same reasoning as the browser gate: run from a directory every user can
# traverse. Ubuntu 24.04 made home dirs 0750, and stage2 invokes this as
# `sudo -u agent-host` from /home/packer.
cd /

run_container=0
[ "${1:-}" = "--run" ] && run_container=1

fail=0

_ok()   { echo "  OK       $1"; }
_bad()  { echo "  MISSING  $1"; [ "${2:-fatal}" = "warn" ] || fail=1; }

_need_bin() {
    local bin="$1" why="$2"
    if command -v "$bin" >/dev/null 2>&1; then
        _ok "$bin — $why"
    else
        _bad "$bin — $why"
    fi
}

echo "=== container stack (rootless podman) ==="

# --- engine + CLI surface ----------------------------------------------------
if out=$(podman --version 2>&1); then _ok "$out"; else _bad "podman ($out)"; fi
_need_bin docker         "Docker CLI shim (podman-docker); workers and compose files say 'docker'"
_need_bin podman-compose "compose stacks (a postgres beside the app is the common case)"

# --- rootless prerequisites --------------------------------------------------
# Each of these is a Recommends that this image's APT config does NOT install
# automatically, so absence is plausible and the symptoms are indirect.
_need_bin newuidmap      "rootless uid mapping (uidmap); without it podman refuses to start"
_need_bin newgidmap      "rootless gid mapping (uidmap)"
_need_bin fuse-overlayfs "rootless storage driver; without it podman falls back to the very slow vfs"

# netavark/aardvark ship as libexec helpers, not on PATH.
for helper in /usr/lib/podman/netavark /usr/libexec/podman/netavark; do
    [ -x "$helper" ] && { _ok "netavark — container networking"; netavark_found=1; break; }
done
[ "${netavark_found:-0}" = "1" ] || _bad "netavark — container networking"

for helper in /usr/lib/podman/aardvark-dns /usr/libexec/podman/aardvark-dns; do
    [ -x "$helper" ] && { _ok "aardvark-dns — container NAME RESOLUTION"; aardvark_found=1; break; }
done
# The sharpest trap in the whole stack: without this a compose stack starts
# and its services simply cannot find each other by name.
[ "${aardvark_found:-0}" = "1" ] || _bad "aardvark-dns — container NAME RESOLUTION (compose services cannot resolve each other without it)"

# rootless networking: either implementation satisfies podman.
if command -v pasta >/dev/null 2>&1 || command -v slirp4netns >/dev/null 2>&1; then
    _ok "rootless networking (pasta and/or slirp4netns)"
else
    _bad "rootless networking (need pasta or slirp4netns)"
fi

# --- configuration that makes it usable non-interactively --------------------
whoami_user=$(id -un)
if grep -q "^${whoami_user}:" /etc/subuid 2>/dev/null && \
   grep -q "^${whoami_user}:" /etc/subgid 2>/dev/null; then
    _ok "/etc/subuid + /etc/subgid ranges for ${whoami_user}"
else
    _bad "/etc/subuid + /etc/subgid ranges for ${whoami_user} (rootless cannot map without them)"
fi

# Ubuntu ships this commented out; `podman run postgres:16` then fails on a
# short-name prompt that no agent shell can answer.
if grep -qs '^unqualified-search-registries' /etc/containers/registries.conf; then
    _ok "unqualified-search-registries (short names like 'postgres:16' resolve)"
else
    _bad "unqualified-search-registries in /etc/containers/registries.conf"
fi

# podman-docker's banner goes to stderr on every docker invocation, and agents
# read stderr as evidence.
if [ -f /etc/containers/nodocker ]; then
    _ok "/etc/containers/nodocker (docker shim stays quiet on stderr)"
else
    _bad "/etc/containers/nodocker — docker shim will print an emulation banner to stderr" warn
fi

# --- live checks -------------------------------------------------------------
# `podman info` needs a user runtime dir, which does not exist during an image
# build (no login session). Report it, never fail the build on it.
if out=$(podman info --format '{{.Host.OCIRuntime.Name}} / {{.Store.GraphDriverName}}' 2>&1); then
    _ok "podman info — runtime+storage: ${out}"
else
    echo "  DEFERRED podman info unavailable here (expected during image build: no user session)"
    echo "           ${out}"
fi

if [ "$run_container" = "1" ]; then
    echo "--- live container run ---"
    if out=$(podman run --rm docker.io/library/hello-world 2>&1); then
        _ok "podman run hello-world"
    else
        _bad "podman run hello-world"
        echo "$out" | tail -5
    fi
fi

if [ "$fail" -ne 0 ]; then
    echo "=== container stack: FAIL ==="
    exit 1
fi
echo "=== container stack: OK ==="
