#!/bin/bash
# =============================================================================
# Nextcloud auto-configuration (before-starting hook)
# =============================================================================
# Configures Nextcloud for the SRW platform on every container start.
# Fully idempotent — safe to re-run. Non-fatal — failures won't block startup.
#
# What this does:
#   1. OIDC: Registers Keycloak as OpenID Connect provider
#   2. OIDC: Enables group provisioning with whitelist regex
#   3. Group Folders: Installs and enables the groupfolders app
#   4. Service account: Creates srw-agents group and agent-service user
#
# Required environment variables (for OIDC — skip OIDC section if unset):
#   NEXTCLOUD_OIDC_CLIENT_SECRET   — OIDC client secret (from Keycloak realm)
#   NEXTCLOUD_OIDC_DISCOVERY_URI   — Keycloak OIDC discovery endpoint (internal)
#
# Optional environment variables:
#   NEXTCLOUD_AGENT_PASSWORD       — Password for agent-service user (default: random)
#
# Usage:
#   Docker Compose: mounted at /docker-entrypoint-hooks.d/before-starting/
#   Kubernetes: inlined in ConfigMap srw-nextcloud-hooks (deployment/19-nextcloud.yaml)
# =============================================================================
# Non-fatal: setup failure must not prevent Nextcloud from starting
set +e

CLIENT_ID="${NEXTCLOUD_OIDC_CLIENT_ID:-nextcloud}"
CLIENT_SECRET="${NEXTCLOUD_OIDC_CLIENT_SECRET:-}"
DISCOVERY_URI="${NEXTCLOUD_OIDC_DISCOVERY_URI:-}"
PROVIDER_NAME="${NEXTCLOUD_OIDC_PROVIDER_NAME:-Keycloak}"
AGENT_PASSWORD="${NEXTCLOUD_AGENT_PASSWORD:-}"

# Helper: run occ (entrypoint already runs hooks as www-data via run_as)
occ() {
    php /var/www/html/occ "$@"
}

# Check if Nextcloud is installed (first-boot race: DB may not be ready yet)
if ! occ status --output=json 2>/dev/null | grep -q '"installed":true'; then
    echo "[nc-setup] Nextcloud not yet installed, skipping (will retry on next start)"
    exit 0
fi

# =============================================================================
# 0. Allow connections to local/internal services (SSRF protection override)
#    Required when Keycloak runs as a local Docker/K8s service.
# =============================================================================
occ config:system:set allow_local_remote_servers --value=true --type=boolean 2>&1 || true

# Allow OIDC login over plain HTTP (local dev without TLS reverse proxy).
# Only effective when OVERWRITEPROTOCOL != https (i.e. dev environments).
if [ "$(occ config:system:get overwriteprotocol 2>/dev/null)" != "https" ]; then
    echo "[nc-setup] Plain HTTP detected, enabling allow_insecure_http for OIDC..."
    occ config:app:set user_oidc allow_insecure_http --value=1 2>&1 || true
fi

# =============================================================================
# 1. OIDC provider registration
# =============================================================================
if [ -z "$CLIENT_SECRET" ] || [ -z "$DISCOVERY_URI" ]; then
    echo "[nc-setup] NEXTCLOUD_OIDC_CLIENT_SECRET or DISCOVERY_URI not set, skipping OIDC"
else
    echo "[nc-setup] Ensuring user_oidc app is installed..."
    occ app:install user_oidc 2>/dev/null || true
    occ app:enable user_oidc 2>/dev/null || true

    if occ user_oidc:providers 2>/dev/null | grep -q "$PROVIDER_NAME"; then
        echo "[nc-setup] OIDC provider '$PROVIDER_NAME' already configured"
    else
        echo "[nc-setup] Registering OIDC provider '$PROVIDER_NAME'..."
        if occ user_oidc:provider "$PROVIDER_NAME" \
            --clientid="$CLIENT_ID" \
            --clientsecret="$CLIENT_SECRET" \
            --discoveryuri="$DISCOVERY_URI" \
            --unique-uid=1 \
            --mapping-uid=preferred_username \
            --mapping-display-name=name \
            --check-bearer=1 \
            --group-provisioning=1 2>&1; then
            echo "[nc-setup] OIDC provider registered successfully"
        else
            echo "[nc-setup] WARNING: Failed to register OIDC provider (non-fatal)"
        fi
    fi

    # Enable group provisioning with whitelist so only project-* groups are
    # OIDC-managed. This protects srw-agents and other manually-managed groups
    # from being purged when a user logs in via OIDC.
    echo "[nc-setup] Configuring OIDC group provisioning whitelist..."
    occ user_oidc:provider "$PROVIDER_NAME" \
        --group-provisioning=1 \
        --group-whitelist-regex='/^project-/' 2>&1 || true
fi

# =============================================================================
# 2. Group Folders app (required for project cloud folders)
# =============================================================================
echo "[nc-setup] Ensuring groupfolders app is installed..."
occ app:install groupfolders 2>/dev/null || true
occ app:enable groupfolders 2>/dev/null || true

# =============================================================================
# 3. srw-agents group (agent service accounts are members of this group;
#    granted access to every project's Group Folder at creation time)
# =============================================================================
if occ group:list --output=json 2>/dev/null | grep -q '"srw-agents"'; then
    echo "[nc-setup] Group 'srw-agents' already exists"
else
    echo "[nc-setup] Creating group 'srw-agents'..."
    occ group:add srw-agents 2>&1 || true
fi

# =============================================================================
# 4. agent-service user (WebDAV service account for agent file operations)
# =============================================================================
if occ user:info agent-service --output=json 2>/dev/null | grep -q '"user_id"'; then
    echo "[nc-setup] User 'agent-service' already exists"
else
    echo "[nc-setup] Creating user 'agent-service'..."
    if [ -z "$AGENT_PASSWORD" ]; then
        AGENT_PASSWORD="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
        echo "[nc-setup] Generated random password for agent-service (set NEXTCLOUD_AGENT_PASSWORD to fix it)"
    fi
    export OC_PASS="$AGENT_PASSWORD"
    occ user:add --password-from-env --group=srw-agents --display-name="SRW Agent Service" agent-service 2>&1 || true
fi

echo "[nc-setup] Nextcloud configuration complete"
exit 0
