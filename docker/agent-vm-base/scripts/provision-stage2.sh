#!/usr/bin/env bash
# =============================================================================
# Agent VM Base Image — Stage 2 Provisioning
# =============================================================================
#
# Light, per-commit bits applied on top of the stage1 qcow2:
#   - User setup (agent-host for SSH + workspace)
#   - SSH server config tuned for RemoteBackend
#   - Management daemon (NATS bridge to orchestrator)
#   - Sudo approval gate (plugin .so + Go daemon, optional)
#   - tmux + git config
#
# Files (daemon binaries, sudo-gate artifacts, configs) are uploaded to /tmp/
# by Packer file provisioners before this script runs. Most stage2 changes
# are: new sudo-gate binary version, updated daemon Python, config tweaks.
# =============================================================================

set -euxo pipefail

echo "=== Stage 2: light provisioning ==="

# -----------------------------------------------------------------------------
# Profiling helper — same shape as stage1 for grep-friendly post-run analysis.
# -----------------------------------------------------------------------------
__SECTION_START=$SECONDS
__PREV_SECTION=""
_section() {
    if [ -n "${__PREV_SECTION}" ]; then
        echo ">>> [PROFILE] '${__PREV_SECTION}' took $((SECONDS - __SECTION_START))s"
    fi
    __PREV_SECTION="$1"
    __SECTION_START=$SECONDS
    echo "--- ${1} ---"
}
_section_end() {
    if [ -n "${__PREV_SECTION}" ]; then
        echo ">>> [PROFILE] '${__PREV_SECTION}' took $((SECONDS - __SECTION_START))s"
    fi
    echo ">>> [PROFILE] stage2 total: ${SECONDS}s"
}

# -----------------------------------------------------------------------------
# 1. Users and directories
# -----------------------------------------------------------------------------

_section "Setting up users"

# agent-host: the SSH user that RemoteBackend connects as. Skip if stage1
# already created it (defensive — currently stage1 doesn't, but lets us
# safely re-run stage2 against an already-stage2'd image during local dev).
if ! id agent-host >/dev/null 2>&1; then
    sudo useradd -m -s /bin/bash agent-host
fi
echo "agent-host ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/agent-host
sudo chmod 0440 /etc/sudoers.d/agent-host
# Allow agent-host to read systemd journal (for debugging daemon issues)
sudo usermod -aG systemd-journal agent-host

# Workspace lives in a dedicated subdirectory of home — keeps dotfiles
# separate and provides a clean target for git clone.
sudo mkdir -p /home/agent-host/workspace
sudo chown agent-host:agent-host /home/agent-host/workspace

# SSH authorized_keys outside home dir — keeps ~ clean for workspace use.
# Keys are injected at runtime by cloud-init.
sudo mkdir -p /etc/ssh/authorized_keys
sudo chmod 755 /etc/ssh/authorized_keys

# Agent runtime directory
sudo mkdir -p /run/agent
sudo chmod 755 /run/agent

# Ensure /run/agent survives reboots via tmpfiles.d
echo "d /run/agent 0755 root root -" | sudo tee /etc/tmpfiles.d/agent.conf

# -----------------------------------------------------------------------------
# 2. SSH server configuration
# -----------------------------------------------------------------------------

_section "Configuring SSH"
sudo tee /etc/ssh/sshd_config.d/agent.conf > /dev/null <<'SSHEOF'
# Agent VM SSH config — optimized for RemoteBackend
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile /etc/ssh/authorized_keys/%u
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
# Keep connections alive (agent may have idle periods between tool calls)
ClientAliveInterval 60
ClientAliveCountMax 720
MaxStartups 10:30:100
SSHEOF

sudo systemctl enable ssh

# -----------------------------------------------------------------------------
# 3. Management daemon
# -----------------------------------------------------------------------------

_section "Installing management daemon"
sudo mkdir -p /opt/srw
sudo cp /tmp/management-daemon.py /opt/srw/management-daemon.py
sudo chmod 755 /opt/srw/management-daemon.py

sudo cp /tmp/management-daemon.service /etc/systemd/system/management-daemon.service
sudo systemctl daemon-reload
# Don't enable here — cloud-init runcmd starts it with the correct env vars.
# The daemon's _wait_for_cloud_init() method also ensures SSH keys are in
# place before registering, as a safety net.

# Create default env file (overwritten by cloud-init at VM creation)
sudo tee /etc/default/management-daemon > /dev/null <<'EOF'
# Overwritten by cloud-init at VM creation time
NATS_URL=
JOB_ID=
EOF

# -----------------------------------------------------------------------------
# 4. Sudo approval gate
# -----------------------------------------------------------------------------
#
# The sudo approval gate intercepts every sudo invocation and forwards it
# to the orchestrator for human approval. Components:
#   - sudo_gate.so    — C plugin loaded by sudo (compiled from vm/sudo-plugin/)
#   - sudo-gated      — Go daemon bridging plugin to orchestrator via NATS (vm/sudo-daemon/)
#
# Compiled binaries are expected at /tmp/ (placed by Packer file provisioner
# from CI artifacts, or compiled during an earlier build step).
# If the binaries aren't present, this section is skipped — the gate is optional.

_section "Setting up sudo approval gate"

if [ -s /tmp/sudo_gate.so ] && [ -s /tmp/sudo-gated ]; then
    echo "Installing plugin .so..."
    sudo install -o root -g root -m 0644 /tmp/sudo_gate.so /usr/libexec/sudo/

    echo "Installing daemon binary..."
    sudo install -o root -g root -m 0755 /tmp/sudo-gated /usr/local/bin/

    echo "Creating daemon user..."
    if ! getent group sudo-gated >/dev/null 2>&1; then
        sudo groupadd -r sudo-gated
    fi
    if ! id sudo-gated >/dev/null 2>&1; then
        sudo useradd -r -g sudo-gated -s /usr/sbin/nologin -d /nonexistent sudo-gated
    fi

    echo "Installing systemd units..."
    sudo install -o root -g root -m 0644 /tmp/sudo-gated.service /etc/systemd/system/
    sudo install -o root -g root -m 0644 /tmp/sudo-gated.socket /etc/systemd/system/

    echo "Installing daemon config..."
    sudo mkdir -p /etc/sudo-gate
    sudo install -o root -g root -m 0644 /tmp/sudo-gated-config.yaml /etc/sudo-gate/config.yaml

    echo "Setting up tmpfiles.d..."
    sudo mkdir -p /etc/tmpfiles.d
    sudo sh -c 'echo "d /run/sudo-gated 0775 root sudo-gated -" > /etc/tmpfiles.d/sudo-gated.conf'

    echo "Enabling socket activation..."
    sudo systemctl daemon-reload
    sudo systemctl enable sudo-gated.socket

    echo "Applying immutable flags..."
    sudo chattr +i /usr/libexec/sudo/sudo_gate.so 2>/dev/null || echo "  chattr skipped (unsupported fs)"

    # Register plugin in sudo.conf LAST — once registered, the plugin runs on
    # every sudo invocation. Since the daemon isn't running during provisioning,
    # fail_mode=deny would break all subsequent sudo commands in this script
    # and in later Packer provisioners (tmux, git config, cleanup).
    # We use fail_mode=open here; cloud-init switches to fail_mode=deny at boot.
    echo "Registering plugin in sudo.conf..."
    # Strip the immutable flag if a prior stage2 already set it (idempotent re-run)
    sudo chattr -i /etc/sudo.conf 2>/dev/null || true
    if ! grep -q "sudo_gate_approval" /etc/sudo.conf; then
        sudo sh -c 'echo "Plugin sudo_gate_approval sudo_gate.so socket_path=/run/sudo-gated/sudo-gated.sock timeout=305 fail_mode=open" >> /etc/sudo.conf'
    fi
    sudo chattr +i /etc/sudo.conf 2>/dev/null || echo "  chattr skipped (unsupported fs)"

    echo "Sudo approval gate installed"
else
    echo "Sudo gate binaries not found at /tmp/ — skipping (gate is optional)"
fi

# Default env file for sudo-gated (always created, overwritten by cloud-init).
# Placed outside the if-block because heredocs inside conditional blocks
# fail under Packer's SSH script provisioner.
sudo tee /etc/default/sudo-gated > /dev/null <<'SGEOF'
# Overwritten by cloud-init at VM creation time
NATS_URL=
JOB_ID=
VM_ID=
SGEOF

# -----------------------------------------------------------------------------
# 5. tmux configuration
# -----------------------------------------------------------------------------

_section "Configuring tmux"
sudo tee /home/agent-host/.tmux.conf > /dev/null <<'TMUXEOF'
# Increase scrollback buffer
set-option -g history-limit 50000
# Mouse support
set -g mouse on
# 256 colors
set -g default-terminal "screen-256color"
TMUXEOF
sudo chown agent-host:agent-host /home/agent-host/.tmux.conf

# -----------------------------------------------------------------------------
# 6. Git configuration
# -----------------------------------------------------------------------------

_section "Configuring git"
sudo -u agent-host git config --global init.defaultBranch main
sudo -u agent-host git config --global user.name "Agent Worker"
sudo -u agent-host git config --global user.email "agent@srw.local"
sudo -u agent-host git config --global core.editor vim
sudo -u agent-host git config --global core.pager cat

_section_end
echo "=== Stage 2 complete ==="
