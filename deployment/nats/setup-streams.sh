#!/usr/bin/env bash
# Create JetStream streams on the NATS hub.
#
# Run from the main cluster after deploying nats-hub-values.yaml.
# Requires: nats CLI (https://github.com/nats-io/natscli)
#
# Usage:
#   # Port-forward to NATS in the cluster
#   kubectl port-forward -n nats svc/nats 4222:4222 &
#   ./setup-streams.sh
#
#   # Or specify the URL directly
#   NATS_URL=nats://<node-ip>:4222 ./setup-streams.sh

set -euo pipefail

NATS_URL="${NATS_URL:-nats://localhost:4222}"

echo "Creating JetStream streams on ${NATS_URL}..."

# VM lifecycle events + status reports
# 24h retention, 1 GB max. 3 replicas (3-node cluster).
nats stream add VM_EVENTS \
  --subjects "vm.lifecycle.>,vm.status.>" \
  --retention limits \
  --max-age 24h \
  --max-bytes 1073741824 \
  --replicas 3 \
  --storage file \
  --discard old \
  --no-deny-delete \
  --no-deny-purge \
  --server "$NATS_URL"

echo "Created stream: VM_EVENTS"

# Agent heartbeats (short retention for dashboard/monitoring)
# 1h retention, 100 MB max.
# Uses single-token wildcard (*), so agent.{id}.heartbeat matches (3 tokens)
# but agent.vm.{id}.heartbeat does NOT (4 tokens). No overlap with VM_EVENTS.
nats stream add AGENT_HEARTBEATS \
  --subjects "agent.*.heartbeat" \
  --retention limits \
  --max-age 1h \
  --max-bytes 104857600 \
  --replicas 3 \
  --storage file \
  --discard old \
  --no-deny-delete \
  --no-deny-purge \
  --server "$NATS_URL"

echo "Created stream: AGENT_HEARTBEATS"

# Job assignments (work queue for at-least-once delivery)
# Work queue retention: message removed after ack.
# 512 MB max (total JetStream store is 2Gi across all streams).
nats stream add JOB_ASSIGNMENTS \
  --subjects "agent.*.job.>" \
  --retention work \
  --max-bytes 536870912 \
  --replicas 3 \
  --storage file \
  --no-deny-delete \
  --no-deny-purge \
  --server "$NATS_URL"

echo "Created stream: JOB_ASSIGNMENTS"

echo ""
echo "All streams created. Verify with:"
echo "  nats stream ls --server $NATS_URL"
