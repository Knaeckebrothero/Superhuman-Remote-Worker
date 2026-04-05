#!/usr/bin/env bash
# =============================================================================
# Configure Nextcloud to use Keycloak for OIDC login
# =============================================================================
#
# DEPRECATED: Use docker/nextcloud/setup-nextcloud.sh instead.
#   That script is mounted as a before-starting hook in all docker-compose files
#   and inlined in the K8s ConfigMap (deployment/19-nextcloud.yaml). It handles
#   OIDC, groupfolders, srw-agents group, and agent-service user creation.
#   This script is retained for manual OIDC-only troubleshooting.
#
# Run this ONCE after both Keycloak and Nextcloud containers are up and healthy.
# Requires: podman (or docker) with the srw-nextcloud container running.
#
# Usage:
#   ./docker/keycloak/setup-nextcloud-oidc.sh
#
# What it does:
#   1. Installs and enables the user_oidc app in Nextcloud
#   2. Registers Keycloak's srw realm as an OpenID Connect provider
#   After this, users see a "Login with Keycloak" button on Nextcloud's login page.
#
# Hostname resolution note:
#   - Discovery URI uses keycloak:8080 (container-to-container, compose DNS)
#   - The browser-facing auth redirect is handled by Keycloak's own discovery
#     metadata, which advertises localhost:${KEYCLOAK_PORT} URLs.
# =============================================================================
set -euo pipefail

CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-podman}"
NEXTCLOUD_CONTAINER="${NEXTCLOUD_CONTAINER:-srw-nextcloud}"
KEYCLOAK_PORT="${KEYCLOAK_PORT:-8180}"

# Client credentials — must match docker/keycloak/realm-export.json
CLIENT_ID="nextcloud"
CLIENT_SECRET="nextcloud-client-secret"

# Keycloak discovery endpoint (container-to-container)
DISCOVERY_URI="http://keycloak:8080/realms/srw/.well-known/openid-configuration"

echo "Configuring Nextcloud OIDC with Keycloak..."
echo "  Container: ${NEXTCLOUD_CONTAINER}"
echo "  Keycloak port: ${KEYCLOAK_PORT}"
echo ""

# Check if Nextcloud container is running
if ! ${CONTAINER_RUNTIME} inspect --format='{{.State.Running}}' "${NEXTCLOUD_CONTAINER}" 2>/dev/null | grep -q true; then
    echo "ERROR: Nextcloud container '${NEXTCLOUD_CONTAINER}' is not running."
    echo "Start it with: podman-compose -f docker-compose.dev.yaml up -d nextcloud keycloak"
    exit 1
fi

# Wait for Nextcloud to be ready (it has a long startup on first run)
echo "Waiting for Nextcloud to be ready..."
for i in $(seq 1 30); do
    if ${CONTAINER_RUNTIME} exec "${NEXTCLOUD_CONTAINER}" curl -sf http://localhost/status.php >/dev/null 2>&1; then
        echo "  Nextcloud is ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Nextcloud did not become ready within 150 seconds."
        echo "Check container logs: ${CONTAINER_RUNTIME} logs ${NEXTCLOUD_CONTAINER}"
        exit 1
    fi
    sleep 5
done

# Check if OIDC provider is already configured (idempotency)
if ${CONTAINER_RUNTIME} exec -u www-data "${NEXTCLOUD_CONTAINER}" php occ user_oidc:provider 2>/dev/null | grep -q "Keycloak"; then
    echo "Keycloak OIDC provider already configured in Nextcloud. Skipping."
    echo ""
    echo "To reconfigure, first delete the existing provider via Nextcloud admin settings"
    echo "or remove the user_oidc app data."
    exit 0
fi

# Install the user_oidc app (|| true handles "already installed" case)
echo "Installing user_oidc app..."
${CONTAINER_RUNTIME} exec -u www-data "${NEXTCLOUD_CONTAINER}" php occ app:install user_oidc 2>/dev/null || true

# Enable the app (idempotent)
echo "Enabling user_oidc app..."
${CONTAINER_RUNTIME} exec -u www-data "${NEXTCLOUD_CONTAINER}" php occ app:enable user_oidc

# Register Keycloak as OIDC provider
echo "Registering Keycloak OIDC provider..."
${CONTAINER_RUNTIME} exec -u www-data "${NEXTCLOUD_CONTAINER}" php occ user_oidc:provider:create "Keycloak" \
    --clientid="${CLIENT_ID}" \
    --clientsecret="${CLIENT_SECRET}" \
    --discoveryuri="${DISCOVERY_URI}" \
    --unique-uid=0 \
    --check-bearer=1

echo ""
echo "Done! Keycloak OIDC provider registered in Nextcloud."
echo ""
echo "Users can now:"
echo "  1. Open Nextcloud at http://localhost:8800"
echo "  2. Click 'Login with Keycloak'"
echo "  3. Authenticate via Keycloak (admin / admin)"
echo "  4. Nextcloud auto-creates their account on first OIDC login"
