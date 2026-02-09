#!/bin/sh
# =============================================================================
# VPN Sidecar Entrypoint
# =============================================================================
# Starts microsocks (SOCKS5 proxy) and openfortivpn with auto-reconnection.
# VPN drops after ~5 hours are expected; the loop handles reconnection with
# smart backoff: quick reconnect for normal timeouts, exponential backoff for
# config/auth errors.
# =============================================================================

set -e

SOCKS_PORT="${SOCKS_PORT:-1080}"
VPN_CONFIG="${VPN_CONFIG:-/etc/openfortivpn/config}"

# ── Generate config from env vars if no config file mounted ────────────────
if [ ! -f "$VPN_CONFIG" ] && [ -n "$VPN_HOST" ]; then
    echo "[vpn] No config file found, generating from environment variables..."
    mkdir -p "$(dirname "$VPN_CONFIG")"
    cat > "$VPN_CONFIG" <<VPNEOF
host = ${VPN_HOST}
port = ${VPN_PORT:-443}
username = ${VPN_USER}
password = ${VPN_PASS}
VPNEOF
    if [ -n "$VPN_REALM" ]; then
        echo "realm = ${VPN_REALM}" >> "$VPN_CONFIG"
    fi
    # SSL verification: ca-file (proper CA chain) takes priority over trusted-cert (hash shortcut)
    if [ -n "$VPN_TRUSTED_CERT" ]; then
        echo "trusted-cert = ${VPN_TRUSTED_CERT}" >> "$VPN_CONFIG"
    fi
    if [ -f /etc/vpn/certs/ca.crt ]; then
        echo "ca-file = /etc/vpn/certs/ca.crt" >> "$VPN_CONFIG"
        echo "[vpn] Using CA certificate from /etc/vpn/certs/ca.crt"
    fi
    chmod 600 "$VPN_CONFIG"
    echo "[vpn] Config generated for ${VPN_HOST}:${VPN_PORT:-443}${VPN_REALM:+ realm=${VPN_REALM}}"
fi

# ── /dev/ppp setup (required by pppd) ──────────────────────────────────────
# In production, /dev/ppp is passed via --device=/dev/ppp. The mknod fallback
# handles cases where the device isn't mapped but the container has NET_ADMIN.
if [ ! -c /dev/ppp ]; then
    echo "[vpn] Creating /dev/ppp device node..."
    if ! mknod /dev/ppp c 108 0 2>/dev/null; then
        echo "[vpn] WARNING: Could not create /dev/ppp (pass --device=/dev/ppp to container)"
    fi
fi

# ── Graceful shutdown ──────────────────────────────────────────────────────
MICROSOCKS_PID=""
VPN_PID=""
FORWARD_PID=""

cleanup() {
    echo "[vpn] Shutting down..."
    [ -n "$VPN_PID" ] && kill "$VPN_PID" 2>/dev/null
    [ -n "$FORWARD_PID" ] && kill "$FORWARD_PID" 2>/dev/null
    [ -n "$MICROSOCKS_PID" ] && kill "$MICROSOCKS_PID" 2>/dev/null
    wait
    echo "[vpn] Stopped."
    exit 0
}

trap cleanup SIGTERM SIGINT

# ── Start microsocks ──────────────────────────────────────────────────────
echo "[vpn] Starting microsocks on port ${SOCKS_PORT}..."
microsocks -p "$SOCKS_PORT" &
MICROSOCKS_PID=$!

# Verify microsocks started
sleep 1
if ! kill -0 "$MICROSOCKS_PID" 2>/dev/null; then
    echo "[vpn] ERROR: microsocks failed to start"
    exit 1
fi
echo "[vpn] microsocks running (PID ${MICROSOCKS_PID})"

# ── Start TCP forward (if configured) ────────────────────────────────────
if [ -n "$FORWARD_PORT" ] && [ -n "$FORWARD_TARGET" ]; then
    echo "[vpn] Starting TCP forward: port ${FORWARD_PORT} -> ${FORWARD_TARGET}"
    socat TCP-LISTEN:${FORWARD_PORT},fork,reuseaddr TCP:${FORWARD_TARGET} &
    FORWARD_PID=$!
    echo "[vpn] TCP forward running (PID ${FORWARD_PID})"
fi

# ── VPN reconnection loop ─────────────────────────────────────────────────
BACKOFF=5
MAX_BACKOFF=60
MIN_CONNECTED_SECS=60

while true; do
    if [ ! -f "$VPN_CONFIG" ]; then
        echo "[vpn] ERROR: VPN config not found at ${VPN_CONFIG}"
        echo "[vpn] Copy config.example to config and fill in your credentials."
        echo "[vpn] Retrying in ${BACKOFF}s..."
        sleep "$BACKOFF"
        BACKOFF=$(( BACKOFF * 2 ))
        [ "$BACKOFF" -gt "$MAX_BACKOFF" ] && BACKOFF=$MAX_BACKOFF
        continue
    fi

    echo "[vpn] Connecting to VPN..."
    START_TIME=$(date +%s)

    # Run openfortivpn in foreground; it exits when the tunnel drops
    openfortivpn -c "$VPN_CONFIG" &
    VPN_PID=$!
    wait "$VPN_PID"
    VPN_EXIT=$?
    VPN_PID=""

    END_TIME=$(date +%s)
    CONNECTED_SECS=$(( END_TIME - START_TIME ))

    if [ "$CONNECTED_SECS" -ge "$MIN_CONNECTED_SECS" ]; then
        # Normal VPN timeout (was connected for a while) — reconnect quickly
        echo "[vpn] VPN disconnected after ${CONNECTED_SECS}s (normal timeout). Reconnecting in 5s..."
        BACKOFF=5
        sleep 5
    else
        # Quick failure — likely config/auth error, use exponential backoff
        echo "[vpn] VPN failed after ${CONNECTED_SECS}s (exit code ${VPN_EXIT}). Retrying in ${BACKOFF}s..."
        sleep "$BACKOFF"
        BACKOFF=$(( BACKOFF * 2 ))
        [ "$BACKOFF" -gt "$MAX_BACKOFF" ] && BACKOFF=$MAX_BACKOFF
    fi
done
