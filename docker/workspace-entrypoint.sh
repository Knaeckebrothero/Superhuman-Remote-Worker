#!/usr/bin/env bash
# =============================================================================
# Workspace container entrypoint
# Starts code-server (background) and SSHD (foreground).
# =============================================================================

set -e

# Copy SSH public key from K8s secret mount (root-owned) to user-owned location.
# OpenSSH StrictModes requires authorized_keys to be owned by the target user.
if [ -f /tmp/ssh-pubkey/ssh-publickey ]; then
    cp /tmp/ssh-pubkey/ssh-publickey /home/agent-host/.ssh/authorized_keys
    chown agent-host:agent-host /home/agent-host/.ssh/authorized_keys
    chmod 600 /home/agent-host/.ssh/authorized_keys
fi

# Start code-server as agent-host (background)
su -c 'code-server --bind-addr 0.0.0.0:8080 /home/agent-host/workspace' agent-host &

# Start SSHD in foreground (PID 1 — container stays alive)
exec /usr/sbin/sshd -D -e
