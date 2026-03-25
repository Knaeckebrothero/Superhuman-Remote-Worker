#!/usr/bin/env bash
# =============================================================================
# Integration Test — Sudo Approval Gate (end-to-end local flow)
# =============================================================================
#
# Exercises the full local flow:
#   mock_plugin.py → sudo-gated daemon → NATS → mock_orchestrator.py
#
# Prerequisites:
#   - NATS server running locally (nats://127.0.0.1:4222)
#   - sudo-gated binary compiled (make -C ../../sudo-gated)
#   - Python 3 with nats-py installed (pip install nats-py)
#
# Run from the repo root or from sudo-gated/test/:
#   bash sudo-gated/test/integration_test.sh
#
# For real sudo testing on a VM, use integration_test_vm.sh instead.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DAEMON_BIN="$REPO_ROOT/sudo-gated/sudo-gated"
SOCKET_PATH="/tmp/sudo-gated-test-$$.sock"
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
MOCK_ORCH_PID=""
DAEMON_PID=""
PASS=0
FAIL=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

cleanup() {
    echo ""
    echo "--- Cleaning up ---"
    [ -n "$MOCK_ORCH_PID" ] && kill "$MOCK_ORCH_PID" 2>/dev/null || true
    [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null || true
    rm -f "$SOCKET_PATH"
    wait 2>/dev/null || true
    echo -e "\n=== Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC} ==="
    [ "$FAIL" -eq 0 ] && exit 0 || exit 1
}
trap cleanup EXIT

assert_ok() {
    local name="$1"
    shift
    echo -n "  TEST: $name ... "
    if "$@" >/dev/null 2>&1; then
        echo -e "${GREEN}PASS${NC}"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}FAIL${NC}"
        FAIL=$((FAIL + 1))
    fi
}

assert_fail() {
    local name="$1"
    shift
    echo -n "  TEST: $name ... "
    if ! "$@" >/dev/null 2>&1; then
        echo -e "${GREEN}PASS${NC}"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}FAIL (expected failure)${NC}"
        FAIL=$((FAIL + 1))
    fi
}

# =============================================================================
# Pre-flight checks
# =============================================================================

echo "=== Sudo Approval Gate — Integration Tests ==="
echo ""

# Check NATS connectivity
echo "--- Pre-flight checks ---"
if ! python3 -c "
import asyncio, nats
async def check():
    nc = await nats.connect('$NATS_URL')
    await nc.close()
asyncio.run(check())
" 2>/dev/null; then
    echo -e "${RED}ERROR: Cannot connect to NATS at $NATS_URL${NC}"
    echo "Start NATS first: podman-compose -f docker-compose.dev.yaml up -d nats"
    exit 1
fi
echo "  NATS: connected"

# Check daemon binary
if [ ! -x "$DAEMON_BIN" ]; then
    echo -e "${YELLOW}Daemon binary not found at $DAEMON_BIN, trying go build...${NC}"
    if command -v go &>/dev/null; then
        (cd "$REPO_ROOT/sudo-gated" && CGO_ENABLED=0 go build -o sudo-gated ./cmd/sudo-gated/)
    else
        echo -e "${RED}ERROR: Go not installed and binary not found${NC}"
        exit 1
    fi
fi
echo "  Daemon binary: $DAEMON_BIN"
echo ""

# =============================================================================
# Test 1: Basic approval flow
# =============================================================================

echo "--- Test group 1: Basic approval flow ---"

# Start mock orchestrator in auto-approve mode
python3 "$SCRIPT_DIR/mock_orchestrator.py" --nats "$NATS_URL" --mode approve &
MOCK_ORCH_PID=$!
sleep 1

# Start daemon with skip-verify (we're testing with mock_plugin.py, not real sudo)
JOB_ID=test-001 NATS_URL="$NATS_URL" "$DAEMON_BIN" \
    --skip-verify \
    --config /dev/null 2>/dev/null &
DAEMON_PID=$!

# Wait for daemon to create socket
# The daemon without a config file uses defaults, so override socket path via env
# Actually, we need to pass the socket path. Let me use a temp config.
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true

# Create temp config pointing to our test socket
TEMP_CONFIG=$(mktemp /tmp/sudo-gated-test-XXXXXX.yaml)
cat > "$TEMP_CONFIG" <<EOF
socket_path: $SOCKET_PATH
nats:
  url: $NATS_URL
  subject_prefix: sudo.request
timeouts:
  nats_request: 10s
  socket_read: 5s
  graceful_shutdown: 5s
log_level: debug
job_id: test-001
vm_id: test-vm-001
EOF

JOB_ID=test-001 NATS_URL="$NATS_URL" "$DAEMON_BIN" \
    --skip-verify \
    --config "$TEMP_CONFIG" 2>/tmp/sudo-gated-test.log &
DAEMON_PID=$!
sleep 1

# Verify daemon is running
assert_ok "daemon started" kill -0 "$DAEMON_PID"
assert_ok "socket exists" test -S "$SOCKET_PATH"

# Send a request via mock plugin
assert_ok "basic approval flow" \
    python3 "$SCRIPT_DIR/mock_plugin.py" --socket "$SOCKET_PATH" "apt-get install curl"

# =============================================================================
# Test 2: Denial flow
# =============================================================================

echo ""
echo "--- Test group 2: Denial flow ---"

# Kill approve orchestrator, start deny orchestrator
kill "$MOCK_ORCH_PID" 2>/dev/null || true
wait "$MOCK_ORCH_PID" 2>/dev/null || true
sleep 0.5

python3 "$SCRIPT_DIR/mock_orchestrator.py" --nats "$NATS_URL" --mode deny &
MOCK_ORCH_PID=$!
sleep 1

assert_fail "denial returns non-zero exit" \
    python3 "$SCRIPT_DIR/mock_plugin.py" --socket "$SOCKET_PATH" "rm -rf /etc"

# =============================================================================
# Test 3: Rate limiting
# =============================================================================

echo ""
echo "--- Test group 3: Rate limiting ---"

# Kill deny orchestrator, start approve orchestrator
kill "$MOCK_ORCH_PID" 2>/dev/null || true
wait "$MOCK_ORCH_PID" 2>/dev/null || true
sleep 0.5

python3 "$SCRIPT_DIR/mock_orchestrator.py" --nats "$NATS_URL" --mode approve &
MOCK_ORCH_PID=$!
sleep 1

# Flood with requests — some should be rate-limited
echo -n "  TEST: rate limiting kicks in ... "
FLOOD_OUTPUT=$(python3 "$SCRIPT_DIR/mock_plugin.py" --socket "$SOCKET_PATH" --flood 8 2>&1)
if echo "$FLOOD_OUTPUT" | grep -q "rate limited"; then
    echo -e "${GREEN}PASS${NC}"
    PASS=$((PASS + 1))
else
    echo -e "${RED}FAIL (no rate limiting detected)${NC}"
    FAIL=$((FAIL + 1))
fi

# =============================================================================
# Test 4: NATS down → denial
# =============================================================================

echo ""
echo "--- Test group 4: NATS unavailable ---"

# Kill orchestrator (no one listening on NATS)
kill "$MOCK_ORCH_PID" 2>/dev/null || true
wait "$MOCK_ORCH_PID" 2>/dev/null || true
MOCK_ORCH_PID=""
sleep 0.5

assert_fail "no responder returns denial" \
    python3 "$SCRIPT_DIR/mock_plugin.py" --socket "$SOCKET_PATH" "apt-get update"

# =============================================================================
# Test 5: Daemon restart
# =============================================================================

echo ""
echo "--- Test group 5: Daemon restart ---"

# Restart mock orchestrator
python3 "$SCRIPT_DIR/mock_orchestrator.py" --nats "$NATS_URL" --mode approve &
MOCK_ORCH_PID=$!
sleep 0.5

# Kill and restart daemon
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true

JOB_ID=test-001 NATS_URL="$NATS_URL" "$DAEMON_BIN" \
    --skip-verify \
    --config "$TEMP_CONFIG" 2>>/tmp/sudo-gated-test.log &
DAEMON_PID=$!
sleep 1

assert_ok "daemon restarted" kill -0 "$DAEMON_PID"
assert_ok "approval works after restart" \
    python3 "$SCRIPT_DIR/mock_plugin.py" --socket "$SOCKET_PATH" "apt-get install git"

# =============================================================================
# Test 6: Delayed approval
# =============================================================================

echo ""
echo "--- Test group 6: Delayed approval (3s) ---"

kill "$MOCK_ORCH_PID" 2>/dev/null || true
wait "$MOCK_ORCH_PID" 2>/dev/null || true

python3 "$SCRIPT_DIR/mock_orchestrator.py" --nats "$NATS_URL" --mode delay --delay 3 &
MOCK_ORCH_PID=$!
sleep 0.5

assert_ok "delayed approval succeeds" \
    python3 "$SCRIPT_DIR/mock_plugin.py" --socket "$SOCKET_PATH" "systemctl restart nginx"

# Clean up temp config
rm -f "$TEMP_CONFIG"

echo ""
echo "--- All test groups complete ---"
