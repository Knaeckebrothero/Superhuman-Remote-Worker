#!/usr/bin/env bash
# =============================================================================
# Integration Test — Real Sudo on a VM
# =============================================================================
#
# Run this ON a VM where the sudo approval gate is installed.
# Requires a mock orchestrator running externally (or the real orchestrator).
#
# Usage:
#   # On the orchestrator host (or any machine with NATS access):
#   python3 sudo-gated/test/mock_orchestrator.py --nats nats://<hub-ip>:4222 --mode interactive
#
#   # On the VM:
#   bash /tmp/integration_test_vm.sh
#
# CRITICAL: Run this from a root shell. If the plugin breaks, you need root
# access to fix /etc/sudo.conf. Keep `virtctl console` ready as a backup.
# =============================================================================

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'
PASS=0
FAIL=0

check() {
    local name="$1"
    local expect="$2"  # "approve" or "deny"
    shift 2
    echo -n "  TEST: $name ... "

    # Run the sudo command as agent-host
    local output
    local exit_code
    output=$(sudo -u agent-host sudo "$@" 2>&1) && exit_code=0 || exit_code=$?

    if [ "$expect" = "approve" ] && [ "$exit_code" -eq 0 ]; then
        echo -e "${GREEN}PASS${NC} (approved, exit 0)"
        PASS=$((PASS + 1))
    elif [ "$expect" = "deny" ] && [ "$exit_code" -ne 0 ]; then
        echo -e "${GREEN}PASS${NC} (denied, exit $exit_code)"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}FAIL${NC} (expected $expect, got exit $exit_code)"
        echo "    Output: $output"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Sudo Approval Gate — VM Integration Tests ==="
echo ""
echo -e "${YELLOW}IMPORTANT: Approve or deny each request as prompted by the mock orchestrator.${NC}"
echo ""

# Verify components are in place
echo "--- Pre-flight checks ---"
echo -n "  Plugin installed: "
[ -f /usr/libexec/sudo/sudo_gate.so ] && echo -e "${GREEN}yes${NC}" || echo -e "${RED}no${NC}"
echo -n "  Daemon binary: "
[ -x /usr/local/bin/sudo-gated ] && echo -e "${GREEN}yes${NC}" || echo -e "${RED}no${NC}"
echo -n "  Socket active: "
systemctl is-active sudo-gated.socket >/dev/null 2>&1 && echo -e "${GREEN}yes${NC}" || echo -e "${RED}no${NC}"
echo -n "  Plugin in sudo.conf: "
grep -q sudo_gate_approval /etc/sudo.conf 2>/dev/null && echo -e "${GREEN}yes${NC}" || echo -e "${RED}no${NC}"

echo ""
echo "--- Verify plugin loads ---"
echo -n "  sudo -V shows plugin: "
if sudo -V 2>&1 | grep -q "sudo_gate"; then
    echo -e "${GREEN}yes${NC}"
    PASS=$((PASS + 1))
else
    echo -e "${RED}no${NC}"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "--- Test: Approve a read-only command ---"
echo -e "${YELLOW}>>> APPROVE the next request in the mock orchestrator <<<${NC}"
check "ls approved" approve ls /tmp

echo ""
echo "--- Test: Deny a command ---"
echo -e "${YELLOW}>>> DENY the next request in the mock orchestrator <<<${NC}"
check "ls denied" deny ls /tmp

echo ""
echo "--- Test: Approve a package install ---"
echo -e "${YELLOW}>>> APPROVE the next request <<<${NC}"
check "apt-get install" approve apt-get install -y curl

echo ""
echo "--- Test: Verify syslog entries ---"
echo -n "  syslog has sudo_gate entries: "
if journalctl -t sudo_gate --no-pager -n 5 2>/dev/null | grep -q "sudo_gate"; then
    echo -e "${GREEN}yes${NC}"
    PASS=$((PASS + 1))
else
    echo -e "${YELLOW}not found (check journalctl -t sudo_gate)${NC}"
fi

echo ""
echo -e "=== Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC} ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
