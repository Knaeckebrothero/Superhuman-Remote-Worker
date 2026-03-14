#!/usr/bin/env bash
# =============================================================================
# Agent VM Base Image — Cleanup Script
# =============================================================================
#
# Minimize image size and remove build artifacts.
# Run as the last Packer provisioner.
# =============================================================================

set -euo pipefail

echo "=== Cleaning up image ==="

# Remove the packer build user (no longer needed)
sudo userdel -r packer 2>/dev/null || true
sudo rm -f /etc/sudoers.d/90-cloud-init-users

# Disable password auth (was enabled for Packer SSH)
sudo sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config 2>/dev/null || true

# Clean apt cache
sudo apt-get autoremove -y
sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*

# Clean pip cache
sudo rm -rf /root/.cache/pip /home/*/.cache/pip

# Clean npm cache
sudo npm cache clean --force 2>/dev/null || true

# Remove cloud-init artifacts (will re-run on first real boot)
sudo cloud-init clean --logs --seed

# Clear tmp
sudo rm -rf /tmp/* /var/tmp/*

# Clear machine-id (regenerated on first boot)
sudo truncate -s 0 /etc/machine-id
sudo rm -f /var/lib/dbus/machine-id

# Clear shell history
sudo rm -f /root/.bash_history /home/*/.bash_history
history -c 2>/dev/null || true

# Zero free space for better compression (optional, takes time)
# sudo dd if=/dev/zero of=/zeros bs=1M 2>/dev/null || true
# sudo rm -f /zeros

echo "=== Cleanup complete ==="
