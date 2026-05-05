#!/usr/bin/env bash
# =============================================================================
# Agent VM Base Image — Stage 1 Cleanup
# =============================================================================
#
# Trims caches and shuts the VM down so Packer can capture the qcow2.
# Lighter than scripts/cleanup.sh: must NOT touch the packer user, machine-id,
# cloud-init state, or sshd PasswordAuthentication — stage 2 still needs to
# SSH in as packer when it boots this image.
# =============================================================================

set -euo pipefail

echo "=== Stage 1 cleanup ==="

sudo bash <<'CLEANUP'
set -euo pipefail

# Reclaim disk space — apt cache, pip cache, playwright tarballs, npm cache.
# Browser binaries live under /opt/playwright and stay.
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*
rm -rf /root/.cache/pip /home/*/.cache/pip
rm -rf /root/.cache/ms-playwright /home/*/.cache/ms-playwright
npm cache clean --force 2>/dev/null || true
rm -rf /tmp/* /var/tmp/*
rm -f /root/.bash_history /home/*/.bash_history

# Verify critical stage1 artifacts before sealing
ls -la /opt/playwright/ /usr/local/bin/agent-chromium >/dev/null 2>&1 \
    || { echo "ERROR: stage1 artifacts missing"; exit 1; }

echo "=== Stage 1 cleanup complete ==="
sync
shutdown -P now
CLEANUP
