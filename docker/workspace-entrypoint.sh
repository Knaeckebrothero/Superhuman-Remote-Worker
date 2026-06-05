#!/usr/bin/env bash
# =============================================================================
# Workspace container entrypoint
# Seeds dotfiles, installs SSH key, starts code-server + SSHD.
# =============================================================================

set -e

# ---------------------------------------------------------------------------
# 1. Seed dotfiles from skeleton (idempotent)
#    Volume mounts shadow everything baked into /home/agent-host by the
#    Dockerfile. The skeleton restores dotfiles (.bashrc, .gitconfig, etc.)
#    on first boot without overwriting files that already exist (-n).
# ---------------------------------------------------------------------------
if [ ! -f /home/agent-host/.workspace-initialized ]; then
    cp -rn /etc/skel.agent-host/. /home/agent-host/
    chown -R agent-host:agent-host /home/agent-host
    touch /home/agent-host/.workspace-initialized
fi

# ---------------------------------------------------------------------------
# 1b. Ensure workspace directory exists (clean target for git clone)
# ---------------------------------------------------------------------------
mkdir -p /home/agent-host/workspace
chown agent-host:agent-host /home/agent-host/workspace

# ---------------------------------------------------------------------------
# 2. Install SSH public key (outside home dir)
#    Key is mounted from a K8s secret at /tmp/ssh-pubkey/. Written to
#    /etc/ssh/authorized_keys/agent-host which sshd finds via the
#    AuthorizedKeysFile /etc/ssh/authorized_keys/%u directive.
#    Root ownership satisfies StrictModes.
# ---------------------------------------------------------------------------
if [ -f /tmp/ssh-pubkey/ssh-publickey ]; then
    cp /tmp/ssh-pubkey/ssh-publickey /etc/ssh/authorized_keys/agent-host
    chmod 644 /etc/ssh/authorized_keys/agent-host
fi

# ---------------------------------------------------------------------------
# 2b. Seed per-user code-server config (theme / keybindings / snippets)
#     The orchestrator mounts a ConfigMap carrying a self-contained seed.sh
#     that writes the user's stored config into /var/lib/code-server/User and
#     sets each file's mtime (so the settings sweeper won't re-pull an
#     untouched seed). Runs as root before the su to agent-host below, so its
#     chown succeeds. Best-effort — a failure must not stop the workspace.
# ---------------------------------------------------------------------------
if [ -f /mnt/code-server-config/seed.sh ]; then
    sh /mnt/code-server-config/seed.sh || echo "code-server seed failed (non-fatal)" >&2
fi

# ---------------------------------------------------------------------------
# 2c. Wait (bounded) for the orchestrator to deliver license/globalStorage
#     state. Only when the seed ConfigMap signalled state is expected. The
#     orchestrator streams the bundle in over SSH then touches the sentinel.
#     Bounded so a slow/absent orchestrator can't wedge startup.
# ---------------------------------------------------------------------------
if [ -f /mnt/code-server-config/expect-state ]; then
    i=0
    while [ ! -f /var/lib/code-server/.ide-seed-state-done ] && [ "$i" -lt 30 ]; do
        sleep 1; i=$((i+1))
    done
    [ -f /var/lib/code-server/.ide-seed-state-done ] || \
        echo "ide state seed sentinel timed out after ${i}s (non-fatal)" >&2
fi

# ---------------------------------------------------------------------------
# 3. Start code-server as agent-host (background)
#    --user-data-dir and --extensions-dir outside home keep the IDE file
#    explorer clean. Opens /home/agent-host/workspace as the workspace root.
# ---------------------------------------------------------------------------
su -c 'code-server \
    --bind-addr 0.0.0.0:38080 \
    --user-data-dir /var/lib/code-server \
    --extensions-dir /var/lib/code-server/extensions \
    /home/agent-host/workspace' agent-host &

# ---------------------------------------------------------------------------
# 4. Start SSHD in foreground (PID 1 — container stays alive)
# ---------------------------------------------------------------------------
exec /usr/sbin/sshd -D -e
