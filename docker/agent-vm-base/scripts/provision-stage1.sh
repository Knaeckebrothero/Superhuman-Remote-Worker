#!/usr/bin/env bash
# =============================================================================
# Agent VM Base Image — Stage 1 Provisioning
# =============================================================================
#
# Heavy, stable bits that go into the published `agent-vm-base-stage1` qcow2:
#   - System packages (dev tools, build essentials, networking)
#   - Datasource CLI clients (psql, mongosh, cypher-shell + JRE)
#   - Tailscale (mesh VPN client for Headscale connectivity)
#   - Python 3 + pip + management-daemon deps (nats-py, psutil)
#   - Playwright Chromium (the slowest single step)
#   - Node.js 22 + minimal global npm packages
#
# Rerun only when stage1 inputs change: this script, .playwright-version,
# cloud-init/, or stage1.pkr.hcl. Stage 2 (provision-stage2.sh) layers
# user/daemon/sudo-gate config on top per commit.
# =============================================================================

set -euxo pipefail

echo "=== Stage 1: heavy provisioning ==="

# -----------------------------------------------------------------------------
# Profiling helper — prints per-section elapsed time so CI logs show which
# step dominates wall-clock. Grep "[PROFILE]" in the build log.
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
    echo ">>> [PROFILE] stage1 total: ${SECONDS}s"
}

# -----------------------------------------------------------------------------
# 1. APT configuration: Azure mirror + tuning + global no-recommends
# -----------------------------------------------------------------------------

_section "Configuring APT"

# archive.ubuntu.com behind Canonical's load balancer is flaky; Azure mirror
# is significantly faster from GHA Azure-hosted runners.
sudo sed -i 's|http://archive\.ubuntu\.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g; s|http://security\.ubuntu\.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g' \
    /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || \
    sudo sed -i 's|http://archive\.ubuntu\.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g; s|http://security\.ubuntu\.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g' \
    /etc/apt/sources.list

sudo tee /etc/apt/apt.conf.d/99-build-tuning > /dev/null <<'EOF'
Acquire::Retries "5";
Acquire::Retries::Delay::Maximum "30";
Acquire::http::Timeout "30";
Acquire::https::Timeout "30";
Acquire::http::Pipeline-Depth "10";
Acquire::Queue-Mode "access";
APT::Install-Recommends "false";
EOF

# -----------------------------------------------------------------------------
# 2. System packages
# -----------------------------------------------------------------------------

_section "Installing system packages"

sudo apt-get update -y

# eatmydata: LD_PRELOADs libeatmydata.so so dpkg's fsync() calls become no-ops.
# Trades durability for speed (~30-50% faster installs); fine for image builds
# since the disk is discarded on failure. Install it first, then wrap every
# subsequent apt-get with `sudo eatmydata`.
sudo apt-get install -y eatmydata

sudo eatmydata apt-get install -y \
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
# 3. Datasource CLI clients (psql, mongosh, cypher-shell)
#     Agent uses these via run_command for read/write datasource queries.
#     Mirrors docker/Dockerfile.workspace datasource section.
# -----------------------------------------------------------------------------

_section "Installing datasource CLI clients"

# Add MongoDB and Neo4j APT repos first, then a single apt-get update + install
# for all three clients (postgresql-client comes from Ubuntu's default repos so
# no custom source is needed). One update instead of three saves ~30-60s on
# slow links since each apt-get update fetches ~100 MB of metadata.
curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc \
    | sudo gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg
echo "deb [signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" \
    | sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list

curl -fsSL https://debian.neo4j.com/neotechnology.gpg.key \
    | sudo gpg --dearmor -o /usr/share/keyrings/neo4j-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/neo4j-archive-keyring.gpg] https://debian.neo4j.com stable latest" \
    | sudo tee /etc/apt/sources.list.d/neo4j.list

sudo apt-get update -y
sudo eatmydata apt-get install -y \
    postgresql-client \
    mongodb-mongosh \
    openjdk-17-jre-headless \
    cypher-shell

# -----------------------------------------------------------------------------
# 4. Tailscale (mesh VPN — joins Headscale tailnet at VM boot via cloud-init)
# -----------------------------------------------------------------------------

_section "Installing Tailscale"
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable tailscaled

# -----------------------------------------------------------------------------
# 5. Python setup (Ubuntu 24.04 ships Python 3.12)
# -----------------------------------------------------------------------------

_section "Configuring Python"

# Ensure python/python3 point to the system python
sudo update-alternatives --install /usr/bin/python python /usr/bin/python3 1 || true

# Management daemon dependencies (system-wide so they survive packer user deletion)
sudo python3 -m pip install --break-system-packages nats-py psutil

# -----------------------------------------------------------------------------
# 6. Playwright Chromium (replaces apt chromium-browser, which on Ubuntu Noble
#    is a snap shim that hangs the build with no snap-store connectivity).
#    Pin propagated from .playwright-version via the Packer var of the same
#    name; symlink is provisioned at image build time only — runtime upgrades
#    would dangle the symlink and are not supported.
# -----------------------------------------------------------------------------

_section "Installing Playwright Chromium"
: "${PLAYWRIGHT_VERSION:?PLAYWRIGHT_VERSION must be set by Packer (see .playwright-version)}"

sudo python3 -m pip install --break-system-packages "playwright==${PLAYWRIGHT_VERSION}"
# fonts-noto-core: extended Unicode/CJK coverage that --with-deps does not pull
sudo eatmydata apt-get install -y --no-install-recommends fonts-noto-core
sudo PLAYWRIGHT_BROWSERS_PATH=/opt/playwright eatmydata playwright install --with-deps chromium

# Playwright's directory layout varies by version: older revs use chrome-linux/,
# newer (>=1.49) use chrome-linux64/. Match both, exclude headless-shell builds.
CHROMIUM_BIN=$(sudo find /opt/playwright -maxdepth 4 -type f \( -path '*/chrome-linux/chrome' -o -path '*/chrome-linux64/chrome' \) ! -path '*headless_shell*' | head -1)
test -n "$CHROMIUM_BIN" || { echo "ERROR: chromium binary not found under /opt/playwright"; sudo find /opt/playwright -maxdepth 4 -type d; exit 1; }
sudo ln -sf "$CHROMIUM_BIN" /usr/local/bin/agent-chromium
echo "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright" | sudo tee -a /etc/environment
echo "Chromium installed at $CHROMIUM_BIN (symlinked /usr/local/bin/agent-chromium)"

# -----------------------------------------------------------------------------
# 7. Node.js 22 + minimal global npm packages
#    Trimmed from the prior list (typescript, ts-node, @angular/cli, eslint,
#    prettier, yarn). Only typescript + prettier are commonly invoked by
#    agent jobs; ts-node and @angular/cli have no recorded usage in src/
#    or config/, and yarn/pnpm are now provided on-demand via corepack.
# -----------------------------------------------------------------------------

_section "Installing Node.js 22"
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo eatmydata apt-get install -y nodejs

_section "Installing global npm packages"
sudo npm install -g \
    typescript \
    prettier
# Enable corepack so `yarn`/`pnpm` install on-demand via shims.
sudo corepack enable

_section_end
echo "=== Stage 1 complete ==="
