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
