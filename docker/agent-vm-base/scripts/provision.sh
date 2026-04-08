#!/usr/bin/env bash
# =============================================================================
# Agent VM Base Image — Provisioning Script
# =============================================================================
#
# Installs everything an agent needs to work inside the VM:
#   - System packages (dev tools, build essentials, networking)
#   - Tailscale (mesh VPN client for Headscale connectivity)
#   - Python 3 + pip (Ubuntu 24.04 ships 3.12 natively)
#   - Node.js 22 + npm + Angular CLI + TypeScript
#   - Management daemon (NATS bridge to orchestrator)
#   - SSH server configured for RemoteBackend
#   - User setup (agent-host for SSH + workspace)
#
# Run by Packer as a shell provisioner.
# =============================================================================

set -euxo pipefail

echo "=== Agent VM Base Image Provisioning ==="

# -----------------------------------------------------------------------------
# 1. System packages
# -----------------------------------------------------------------------------

echo "--- Installing system packages ---"
sudo apt-get update -y
sudo apt-get install -y \
    openssh-server \
    tmux \
    git \
    curl \
    wget \
    jq \
    vim \
    nano \
    less \
    tree \
    htop \
    procps \
    net-tools \
    iproute2 \
    dnsutils \
    iputils-ping \
    zip \
    unzip \
    sudo \
    ca-certificates \
    gnupg \
    lsb-release \
    build-essential \
    cmake \
    pkg-config \
    libssl-dev \
    libffi-dev \
    libpq-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    ripgrep \
    fd-find \
    poppler-utils \
    pandoc \
    python3 \
    python3-venv \
    python3-dev \
    python3-pip \
    chromium-browser \
    fonts-liberation \
    fonts-noto-core

# -----------------------------------------------------------------------------
# 1b. Datasource CLI clients (psql, mongosh, cypher-shell)
#     Needed for read-write datasource mode — agent runs queries via shell.
#     Mirrors docker/Dockerfile.workspace section 1b.
# -----------------------------------------------------------------------------

echo "--- Installing datasource CLI clients ---"

# PostgreSQL client (psql) — available in default Ubuntu repos
sudo apt-get install -y postgresql-client

# MongoDB Shell (mongosh) — from MongoDB's official APT repo
curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc \
    | sudo gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg
echo "deb [signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" \
    | sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list
sudo apt-get update -y
sudo apt-get install -y mongodb-mongosh

# Neo4j Cypher Shell — requires JRE, from Neo4j's official APT repo
curl -fsSL https://debian.neo4j.com/neotechnology.gpg.key \
    | sudo gpg --dearmor -o /usr/share/keyrings/neo4j-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/neo4j-archive-keyring.gpg] https://debian.neo4j.com stable latest" \
    | sudo tee /etc/apt/sources.list.d/neo4j.list
sudo apt-get update -y
sudo apt-get install -y openjdk-17-jre-headless cypher-shell

# -----------------------------------------------------------------------------
# 2. Tailscale (mesh VPN — joins Headscale tailnet at VM boot via cloud-init)
# -----------------------------------------------------------------------------

echo "--- Installing Tailscale ---"
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable tailscaled

# -----------------------------------------------------------------------------
# 3. Python setup (Ubuntu 24.04 ships Python 3.12)
# -----------------------------------------------------------------------------

echo "--- Configuring Python ---"

# Ensure python/python3 point to the system python
sudo update-alternatives --install /usr/bin/python python /usr/bin/python3 1 || true

# Management daemon dependencies (system-wide so they survive packer user deletion)
sudo python3 -m pip install --break-system-packages nats-py psutil

# -----------------------------------------------------------------------------
# 4. Node.js 22 + npm + global packages
# -----------------------------------------------------------------------------

echo "--- Installing Node.js 22 ---"
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

echo "--- Installing global npm packages ---"
sudo npm install -g \
    typescript \
    ts-node \
    @angular/cli \
    eslint \
    prettier \
    yarn

# -----------------------------------------------------------------------------
# 5. Users and directories
# -----------------------------------------------------------------------------

echo "--- Setting up users ---"

# agent-host: the SSH user that RemoteBackend connects as
sudo useradd -m -s /bin/bash agent-host
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
# 6. SSH server configuration
# -----------------------------------------------------------------------------

echo "--- Configuring SSH ---"
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
# 7. Management daemon
# -----------------------------------------------------------------------------

echo "--- Installing management daemon ---"
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
# 8. Sudo approval gate
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

echo "--- Setting up sudo approval gate ---"

if [ -s /tmp/sudo_gate.so ] && [ -s /tmp/sudo-gated ]; then
    echo "Installing plugin .so..."
    sudo install -o root -g root -m 0644 /tmp/sudo_gate.so /usr/libexec/sudo/

    echo "Installing daemon binary..."
    sudo install -o root -g root -m 0755 /tmp/sudo-gated /usr/local/bin/

    echo "Creating daemon user..."
    sudo groupadd -r sudo-gated
    sudo useradd -r -g sudo-gated -s /usr/sbin/nologin -d /nonexistent sudo-gated

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
    sudo sh -c 'echo "Plugin sudo_gate_approval sudo_gate.so socket_path=/run/sudo-gated/sudo-gated.sock timeout=305 fail_mode=open" >> /etc/sudo.conf'
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
# 9. tmux configuration
# -----------------------------------------------------------------------------

echo "--- Configuring tmux ---"
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
# 10. Git configuration
# -----------------------------------------------------------------------------

echo "--- Configuring git ---"
sudo -u agent-host git config --global init.defaultBranch main
sudo -u agent-host git config --global user.name "Agent Worker"
sudo -u agent-host git config --global user.email "agent@srw.local"
sudo -u agent-host git config --global core.editor vim

echo "=== Provisioning complete ==="
