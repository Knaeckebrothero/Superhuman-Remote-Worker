#!/usr/bin/env bash
# =============================================================================
# Workspace container entrypoint
# Starts code-server (background) and SSHD (foreground).
# =============================================================================

set -e

# If authorized_keys was mounted read-only by K8s, fix ownership
if [ -f /home/agent-host/.ssh/authorized_keys ]; then
    chmod 600 /home/agent-host/.ssh/authorized_keys 2>/dev/null || true
fi

# Start code-server as agent-host (background)
su -c 'code-server --bind-addr 0.0.0.0:8080 /home/agent-host/workspace' agent-host &

# Start SSHD in foreground (PID 1 — container stays alive)
exec /usr/sbin/sshd -D -e
