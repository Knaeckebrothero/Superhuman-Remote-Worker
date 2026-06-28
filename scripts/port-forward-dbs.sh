#!/usr/bin/env bash
# =============================================================================
# Port-forward the main-cluster databases to localhost for IDE access
# (PyCharm / DataGrip / psql / cypher-shell).
#
# Every DB service in the cluster is ClusterIP — not exposed to the network.
# This tunnels each one to a localhost port via `kubectl port-forward` (through
# the kube-apiserver); nothing becomes reachable from outside your machine.
#
# Forwards (defaults; override any port via env var):
#   srw-postgres  (app DB)        -> localhost:5432   [APP_PG_PORT]
#   srw-pgvector  (embeddings)    -> localhost:5433   [VECTOR_PG_PORT]
#   srw-auditdb   (audit trail)   -> localhost:5434   [AUDIT_PG_PORT]
#
# All three Postgres instances listen on 5432 in-cluster, so they are mapped to
# distinct LOCAL ports here.
#
# Neo4j is intentionally NOT forwarded — reach it via the Neo4j Browser on the web;
# JetBrains' DB tools have no Neo4j integration anyway.
#
# Credentials live in the `srw` Secret (keys: POSTGRES_*, VECTOR_POSTGRES_*,
# AUDIT_POSTGRES_*). Read one with, e.g.:
#   kubectl --context main -n superhuman-remote-worker \
#     get secret srw -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d; echo
#
# Usage:
#   scripts/port-forward-dbs.sh            # forward all three Postgres DBs; Ctrl-C to stop
#   APP_PG_PORT=15432 scripts/port-forward-dbs.sh   # remap if 5432 is taken
#   KUBE_CONTEXT=main KUBE_NAMESPACE=... scripts/port-forward-dbs.sh
#
# Forwards auto-restart if they drop (laptop sleep, network blip, pod restart).
# =============================================================================
set -uo pipefail

CTX="${KUBE_CONTEXT:-main}"
NS="${KUBE_NAMESPACE:-superhuman-remote-worker}"
BACKOFF="${RESTART_BACKOFF:-2}"   # seconds to wait before restarting a dropped forward
STAGGER="${FORWARD_STAGGER:-0.5}" # seconds between starting each forward. Launching them
                                  # simultaneously races on SPDY stream setup through the
                                  # apiserver and one forward can fail with
                                  # "error creating error stream ... Timeout occurred".

APP_PG_PORT="${APP_PG_PORT:-5432}"
VECTOR_PG_PORT="${VECTOR_PG_PORT:-5433}"
AUDIT_PG_PORT="${AUDIT_PG_PORT:-5434}"

# label | service | one-or-more "local:remote" port maps (space-separated)
FORWARDS=(
  "app-postgres|srw-postgres|${APP_PG_PORT}:5432"
  "pgvector|srw-pgvector|${VECTOR_PG_PORT}:5432"
  "auditdb|srw-auditdb|${AUDIT_PG_PORT}:5432"
)

log() { printf '%s\n' "$*" >&2; }

command -v kubectl >/dev/null 2>&1 || { log "ERROR: kubectl not found in PATH."; exit 1; }

# --- Preflight: confirm the services exist (also validates context/namespace) ---
mapfile -t SVCS < <(printf '%s\n' "${FORWARDS[@]}" | cut -d'|' -f2 | sort -u)
if ! kubectl --context "$CTX" -n "$NS" get svc "${SVCS[@]}" >/dev/null 2>&1; then
  log "ERROR: could not find the database services in context '$CTX' namespace '$NS'."
  log "       Check: kubectl --context $CTX -n $NS get svc"
  exit 1
fi

# --- Warn about local ports already in use (the forward would just fail+retry) ---
port_in_use() {
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH "sport = :$p" 2>/dev/null | grep -q .
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && { exec 3>&- 3<&-; return 0; } || return 1
  fi
}
for p in "$APP_PG_PORT" "$VECTOR_PG_PORT" "$AUDIT_PG_PORT"; do
  if port_in_use "$p"; then
    log "WARNING: local port $p is already in use — that forward will fail until it's free."
    log "         (override the port via the matching *_PORT env var, e.g. APP_PG_PORT=15432)"
  fi
done

# --- One supervised forward (auto-restarts on drop until told to stop) ---
forward() {
  local label="$1" svc="$2"; shift 2   # remaining args = port maps
  local kpid="" stop=0
  trap 'stop=1; [[ -n "$kpid" ]] && kill "$kpid" 2>/dev/null; exit 0' TERM INT
  while [[ "$stop" -eq 0 ]]; do
    kubectl --context "$CTX" -n "$NS" port-forward "svc/$svc" "$@" \
      > >(sed "s/^/[$label] /" >&2) 2>&1 &
    kpid=$!
    wait "$kpid"
    [[ "$stop" -eq 1 ]] && break
    log "[$label] forward dropped — restarting in ${BACKOFF}s…"
    sleep "$BACKOFF"
  done
}

PIDS=()
cleanup() {
  log ""
  log "Stopping all port-forwards…"
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  log "Done."
}
trap cleanup EXIT INT TERM

log "Port-forwarding databases from context '$CTX' (namespace '$NS'):"
log ""
printf '  %-26s -> %s\n' "srw-postgres (app DB)"      "localhost:${APP_PG_PORT}"     >&2
printf '  %-26s -> %s\n' "srw-pgvector (embeddings)"  "localhost:${VECTOR_PG_PORT}"  >&2
printf '  %-26s -> %s\n' "srw-auditdb (audit)"        "localhost:${AUDIT_PG_PORT}"   >&2
log ""
log "Leave this running. Press Ctrl-C to stop all forwards."
log ""

for entry in "${FORWARDS[@]}"; do
  IFS='|' read -r label svc maps <<< "$entry"
  # shellcheck disable=SC2086  # intentional word-split: maps may hold several port pairs
  forward "$label" "$svc" $maps &
  PIDS+=($!)
  sleep "$STAGGER"   # stagger startups so the forwards don't race on SPDY stream setup
done

wait
