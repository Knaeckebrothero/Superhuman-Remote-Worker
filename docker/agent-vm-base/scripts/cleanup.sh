#!/usr/bin/env bash
# =============================================================================
# Agent VM Base Image — Cleanup Script
# =============================================================================
#
# Minimize image size and remove build artifacts.
# Run as the last Packer provisioner.
#
# Runs everything in a single sudo bash block so that sudo is only
# checked once — cloud-init clean removes the sudoers entry that
# grants the packer user NOPASSWD access.
# =============================================================================

set -euo pipefail

echo "=== Cleaning up image ==="

sudo bash <<'CLEANUP'
set -euo pipefail

# Disable password auth (was enabled for Packer SSH)
sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config 2>/dev/null || true

# Clean apt cache
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*

# Clean pip cache
rm -rf /root/.cache/pip /home/*/.cache/pip

# Clean Playwright tarball cache (browser binary lives in /opt/playwright;
# the download tarballs in ~/.cache/ms-playwright are no longer needed)
rm -rf /root/.cache/ms-playwright /home/*/.cache/ms-playwright

# Clean npm cache
npm cache clean --force 2>/dev/null || true

# Remove cloud-init artifacts (will re-run on first real boot)
cloud-init clean --logs --seed

# Clear tmp
rm -rf /tmp/* /var/tmp/*

# Clear machine-id (regenerated on first boot)
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

# Clear shell history
rm -f /root/.bash_history /home/*/.bash_history

# Remove packer build user and sudoers, then shut down
rm -f /etc/sudoers.d/90-cloud-init-users
userdel -r packer 2>/dev/null || true

# Verify critical files exist before shutdown
ls -la /opt/srw/management-daemon.py /etc/systemd/system/management-daemon.service

echo "=== Cleanup complete ==="
sync
shutdown -P now
CLEANUP
