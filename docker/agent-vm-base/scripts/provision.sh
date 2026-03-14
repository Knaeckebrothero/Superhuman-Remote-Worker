#!/usr/bin/env bash
# =============================================================================
# Agent VM Base Image — Provisioning Script
# =============================================================================
#
# Installs everything an agent needs to work inside the VM:
#   - System packages (dev tools, build essentials, networking)
#   - Python 3 + pip (Ubuntu 24.04 ships 3.12 natively)
#   - Node.js 22 + npm + Angular CLI + TypeScript
#   - Management daemon (NATS bridge to orchestrator)
#   - SSH server configured for RemoteBackend
#   - User setup (agent-host for SSH, agent-worker workspace)
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
    python3-pip

# -----------------------------------------------------------------------------
# 2. Python setup (Ubuntu 24.04 ships Python 3.12)
# -----------------------------------------------------------------------------

echo "--- Configuring Python ---"

# Ensure python/python3 point to the system python
sudo update-alternatives --install /usr/bin/python python /usr/bin/python3 1 || true

# Upgrade pip
python3 -m pip install --break-system-packages --upgrade pip setuptools wheel

# Management daemon dependencies (system-wide, needed at boot before any venv)
python3 -m pip install --break-system-packages nats-py psutil

# -----------------------------------------------------------------------------
# 3. Node.js 22 + npm + global packages
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
# 4. Users and directories
# -----------------------------------------------------------------------------

echo "--- Setting up users ---"

# agent-host: the SSH user that RemoteBackend connects as
sudo useradd -m -s /bin/bash agent-host
echo "agent-host ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/agent-host
sudo chmod 0440 /etc/sudoers.d/agent-host

# Workspace directory (where the agent operates)
sudo mkdir -p /home/agent-worker/workspace
sudo chown agent-host:agent-host /home/agent-worker
sudo chown agent-host:agent-host /home/agent-worker/workspace

# SSH key directory (keys injected at runtime by orchestrator or cloud-init)
sudo mkdir -p /home/agent-host/.ssh
sudo chmod 700 /home/agent-host/.ssh
sudo chown agent-host:agent-host /home/agent-host/.ssh

# Agent runtime directory
sudo mkdir -p /run/agent
sudo chmod 755 /run/agent

# Ensure /run/agent survives reboots via tmpfiles.d
echo "d /run/agent 0755 root root -" | sudo tee /etc/tmpfiles.d/agent.conf

# -----------------------------------------------------------------------------
# 5. SSH server configuration
# -----------------------------------------------------------------------------

echo "--- Configuring SSH ---"
sudo tee /etc/ssh/sshd_config.d/agent.conf > /dev/null <<'SSHEOF'
# Agent VM SSH config — optimized for RemoteBackend
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
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
# 6. Management daemon
# -----------------------------------------------------------------------------

echo "--- Installing management daemon ---"
sudo mkdir -p /opt/srw
sudo cp /tmp/management-daemon.py /opt/srw/management-daemon.py
sudo chmod 755 /opt/srw/management-daemon.py

sudo cp /tmp/management-daemon.service /etc/systemd/system/management-daemon.service
sudo systemctl daemon-reload
sudo systemctl enable management-daemon

# Create default env file (overwritten by cloud-init at VM creation)
sudo tee /etc/default/management-daemon > /dev/null <<'EOF'
# Overwritten by cloud-init at VM creation time
NATS_URL=
JOB_ID=
EOF

# -----------------------------------------------------------------------------
# 7. tmux configuration
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
# 8. Git configuration
# -----------------------------------------------------------------------------

echo "--- Configuring git ---"
sudo -u agent-host git config --global init.defaultBranch main
sudo -u agent-host git config --global user.name "Agent Worker"
sudo -u agent-host git config --global user.email "agent@srw.local"
sudo -u agent-host git config --global core.editor vim

echo "=== Provisioning complete ==="
