#!/usr/bin/env bash
# Provision the production Dynamic Canvas gateway PostgreSQL role without
# putting either database password in argv, a persisted manifest, or terminal
# output. Secret YAML exists only inside the direct kubectl-to-kubectl pipe.

set -euo pipefail
set +x
umask 077

# Keep an optionally exported database-owner password local to this shell. It
# is re-exported only for psql, never for stat, kubectl, grep, or cleanup tools.
ADMIN_PGPASSWORD_SET=false
ADMIN_PGPASSWORD=""
if [[ ${PGPASSWORD+x} ]]; then
  ADMIN_PGPASSWORD_SET=true
  ADMIN_PGPASSWORD="$PGPASSWORD"
  unset PGPASSWORD
fi
unset CANVAS_VIEWER_POSTGRES_PASSWORD || true

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROLE_SQL="$ROOT_DIR/helm/files/canvas-viewer-role.sql"
APPLY=false
APPLY_SECRET=false
TMP_DIR=""

usage() {
  cat <<'EOF'
Usage: provision-canvas-gateway-database.sh [--apply | --apply-secret]

Without an apply flag, this performs a read-only database/schema preflight.

  --apply         Reconcile and verify the restricted PostgreSQL role only.
  --apply-secret  Reconcile the role, verify it, and create/update the dedicated
                  Kubernetes Secret. This implies --apply.

Required environment:
  PGHOST, PGPORT, PGDATABASE, PGUSER
      Explicit libpq coordinates for an application-database owner/admin.
      Supply its password through PGPASSWORD, PGPASSFILE, or another libpq
      mechanism; never put a credential in a command-line URL.
  CANVAS_VIEWER_POSTGRES_PASSWORD_FILE
      A mode-0600 regular file containing the new restricted-role password.

Optional environment:
  CANVAS_VIEWER_POSTGRES_USER       (default: srw_canvas_gateway)
  CANVAS_VIEWER_SECRET_PASSWORD_KEY (default: CANVAS_VIEWER_POSTGRES_PASSWORD)

Required with --apply-secret:
  KUBE_CONTEXT, KUBE_NAMESPACE, CANVAS_VIEWER_SECRET_NAME

The target database must already contain Canvas migrations through 0062. The
script never creates a namespace and never accepts a Kubernetes context by
implicit current-context selection.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  unset ADMIN_PGPASSWORD CANVAS_VIEWER_POSTGRES_PASSWORD || true
  if [[ -n "$TMP_DIR" ]]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

admin_psql() {
  if [[ "$ADMIN_PGPASSWORD_SET" == true ]]; then
    PGPASSWORD="$ADMIN_PGPASSWORD" command psql "$@"
  else
    command psql "$@"
  fi
}

role_admin_psql() {
  if [[ "$ADMIN_PGPASSWORD_SET" == true ]]; then
    PGPASSWORD="$ADMIN_PGPASSWORD" \
    CANVAS_VIEWER_POSTGRES_USER="$CANVAS_VIEWER_POSTGRES_USER" \
    CANVAS_VIEWER_POSTGRES_PASSWORD="$CANVAS_VIEWER_POSTGRES_PASSWORD" \
      command psql "$@"
  else
    CANVAS_VIEWER_POSTGRES_USER="$CANVAS_VIEWER_POSTGRES_USER" \
    CANVAS_VIEWER_POSTGRES_PASSWORD="$CANVAS_VIEWER_POSTGRES_PASSWORD" \
      command psql "$@"
  fi
}

validate_secret_shape() {
  local secret_type immutable actual_keys expected_keys
  secret_type="$(
    kubectl --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE" \
      get secret "$CANVAS_VIEWER_SECRET_NAME" -o jsonpath='{.type}'
  )"
  [[ "$secret_type" == "Opaque" ]] \
    || die "the dedicated Canvas database Secret must have type Opaque"
  immutable="$(
    kubectl --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE" \
      get secret "$CANVAS_VIEWER_SECRET_NAME" -o jsonpath='{.immutable}'
  )"
  [[ "$immutable" != "true" ]] \
    || die "the dedicated Canvas database Secret is immutable and cannot be reconciled"
  actual_keys="$(
    kubectl --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE" \
      get secret "$CANVAS_VIEWER_SECRET_NAME" \
      -o go-template='{{range $key, $_ := .data}}{{$key}}{{"\n"}}{{end}}' \
      | LC_ALL=C sort
  )"
  expected_keys="$CANVAS_VIEWER_SECRET_PASSWORD_KEY"
  [[ "$actual_keys" == "$expected_keys" ]] \
    || die "the dedicated Canvas database Secret must contain exactly the configured password key"
}

for argument in "$@"; do
  case "$argument" in
    --apply)
      APPLY=true
      ;;
    --apply-secret)
      APPLY=true
      APPLY_SECRET=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $argument"
      ;;
  esac
done

for binary in psql; do
  command -v "$binary" >/dev/null 2>&1 || die "missing required binary: $binary"
done

for variable in PGHOST PGPORT PGDATABASE PGUSER \
  CANVAS_VIEWER_POSTGRES_PASSWORD_FILE; do
  [[ -n "${!variable:-}" ]] || die "$variable is required"
done

[[ "$PGPORT" =~ ^[0-9]+$ ]] \
  && ((10#$PGPORT >= 1 && 10#$PGPORT <= 65535)) \
  || die "PGPORT must be between 1 and 65535"

CANVAS_VIEWER_POSTGRES_USER="${CANVAS_VIEWER_POSTGRES_USER:-srw_canvas_gateway}"
CANVAS_VIEWER_SECRET_PASSWORD_KEY="${CANVAS_VIEWER_SECRET_PASSWORD_KEY:-CANVAS_VIEWER_POSTGRES_PASSWORD}"

[[ "$CANVAS_VIEWER_POSTGRES_USER" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] \
  || die "CANVAS_VIEWER_POSTGRES_USER is not a valid restricted role name"
[[ "$CANVAS_VIEWER_SECRET_PASSWORD_KEY" =~ ^[A-Za-z0-9._-]+$ ]] \
  || die "CANVAS_VIEWER_SECRET_PASSWORD_KEY is not a valid Secret data key"

PASSWORD_FILE="$CANVAS_VIEWER_POSTGRES_PASSWORD_FILE"
[[ -f "$PASSWORD_FILE" && ! -L "$PASSWORD_FILE" ]] \
  || die "CANVAS_VIEWER_POSTGRES_PASSWORD_FILE must be a regular, non-symlink file"

password_mode="$(stat -c '%a' "$PASSWORD_FILE" 2>/dev/null)" \
  || die "could not inspect CANVAS_VIEWER_POSTGRES_PASSWORD_FILE permissions"
[[ "$password_mode" =~ ^[0-7]{3,4}$ ]] \
  || die "could not validate CANVAS_VIEWER_POSTGRES_PASSWORD_FILE permissions"
if ((8#$password_mode & 077)); then
  die "CANVAS_VIEWER_POSTGRES_PASSWORD_FILE must not be group/world accessible"
fi

CANVAS_VIEWER_POSTGRES_PASSWORD="$(<"$PASSWORD_FILE")"
[[ ${#CANVAS_VIEWER_POSTGRES_PASSWORD} -ge 16 ]] \
  || die "Canvas gateway database password must contain at least 16 characters"
[[ "$CANVAS_VIEWER_POSTGRES_PASSWORD" != *$'\n'* \
   && "$CANVAS_VIEWER_POSTGRES_PASSWORD" != *$'\r'* ]] \
  || die "Canvas gateway database password must be a single line"

schema_ready="$(
  admin_psql --no-psqlrc --quiet --tuples-only --no-align \
    --set ON_ERROR_STOP=1 \
    --command "SELECT to_regclass('public.canvas_origin_sessions') IS NOT NULL AND COUNT(*) = 5 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'canvas_view_bootstraps' AND column_name IN ('authorized_at', 'browser_binding_hash', 'challenge_hash', 'exchange_token_hash', 'ready_receipt_hash')"
)"
[[ "$schema_ready" == "t" ]] \
  || die "Canvas viewer schema is not ready; apply migrations through 0062 first"

if [[ "$APPLY_SECRET" == true ]]; then
  command -v kubectl >/dev/null 2>&1 || die "missing required binary: kubectl"
  for variable in KUBE_CONTEXT KUBE_NAMESPACE CANVAS_VIEWER_SECRET_NAME; do
    [[ -n "${!variable:-}" ]] || die "$variable is required with --apply-secret"
  done
  [[ ${#KUBE_NAMESPACE} -le 63 \
     && "$KUBE_NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] \
    || die "KUBE_NAMESPACE is not a valid Kubernetes namespace"
  [[ ${#CANVAS_VIEWER_SECRET_NAME} -le 253 \
     && "$CANVAS_VIEWER_SECRET_NAME" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] \
    || die "CANVAS_VIEWER_SECRET_NAME is not a valid Kubernetes Secret name"

  context_name="$(kubectl config get-contexts "$KUBE_CONTEXT" -o name)"
  [[ "$context_name" == "$KUBE_CONTEXT" ]] || die "KUBE_CONTEXT does not exist"
  kubectl --context "$KUBE_CONTEXT" get namespace "$KUBE_NAMESPACE" >/dev/null
  if kubectl --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE" \
      get secret "$CANVAS_VIEWER_SECRET_NAME" >/dev/null 2>&1; then
    # Reject a conflicting pre-existing object before rotating the database
    # role, avoiding a preventable cross-system partial update.
    validate_secret_shape
  fi
fi

if [[ "$APPLY" != true ]]; then
  printf 'Canvas gateway database provisioning preflight passed; no state changed.\n'
  printf 'Rerun with --apply or --apply-secret after checking the explicit targets.\n'
  exit 0
fi

role_admin_psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 --file "$ROLE_SQL"

restricted_identity_ok="$(
  PGUSER="$CANVAS_VIEWER_POSTGRES_USER" \
  PGPASSWORD="$CANVAS_VIEWER_POSTGRES_PASSWORD" \
  psql --no-psqlrc --quiet --tuples-only --no-align \
    --set ON_ERROR_STOP=1 \
    --command "SELECT session_user = current_user AND current_user = '$CANVAS_VIEWER_POSTGRES_USER' AND has_database_privilege(current_user, current_database(), 'CONNECT') AND NOT has_database_privilege(current_user, current_database(), 'CREATE') AND has_schema_privilege(current_user, 'public', 'USAGE') AND NOT has_schema_privilege(current_user, 'public', 'CREATE')"
)"
[[ "$restricted_identity_ok" == "t" ]] \
  || die "restricted Canvas database identity verification failed"

if [[ "$APPLY_SECRET" == true ]]; then
  # The database work is complete. Ensure no subsequent child process inherits
  # either database password while retaining the viewer password as a local
  # shell variable for the private Secret files.
  ADMIN_PGPASSWORD=""

  TMP_DIR="$(mktemp -d)"
  printf '%s' "$CANVAS_VIEWER_POSTGRES_PASSWORD" >"$TMP_DIR/password"

  kubectl --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE" \
    create secret generic "$CANVAS_VIEWER_SECRET_NAME" \
    --from-file="$CANVAS_VIEWER_SECRET_PASSWORD_KEY=$TMP_DIR/password" \
    --dry-run=client -o yaml \
    | kubectl --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE" \
        apply -f - >/dev/null

  kubectl --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE" \
    get secret "$CANVAS_VIEWER_SECRET_NAME" >/dev/null
  validate_secret_shape
  printf 'Restricted Canvas database role and dedicated Secret were provisioned.\n'
else
  printf 'Restricted Canvas database role was provisioned and verified.\n'
  printf 'No Kubernetes Secret was changed.\n'
fi
