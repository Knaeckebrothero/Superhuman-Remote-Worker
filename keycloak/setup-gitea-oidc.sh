#!/usr/bin/env bash
# =============================================================================
# Register Keycloak as OAuth2 provider in Gitea
# =============================================================================
#
# Run this ONCE after both Keycloak and Gitea containers are up.
# Requires: podman (or docker) with the srw-gitea container running.
#
# Usage:
#   ./keycloak/setup-gitea-oidc.sh
#
# What it does:
#   Registers Keycloak's srw realm as an OpenID Connect auth source in Gitea.
#   After this, users see a "Sign in with Keycloak" button on Gitea's login page.
#
# Hostname resolution note:
#   - Auth URL uses localhost:${KEYCLOAK_PORT} (browser-facing, reaches host port)
#   - Token/profile URLs use keycloak:8080 (container-to-container, compose DNS)
#   This split is required because the browser and Gitea server resolve hostnames
#   differently in a container environment.
# =============================================================================
set -euo pipefail

CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-podman}"
GITEA_CONTAINER="${GITEA_CONTAINER:-srw-gitea}"
KEYCLOAK_PORT="${KEYCLOAK_PORT:-8180}"

# Client credentials — must match keycloak/realm-export.json
CLIENT_ID="gitea"
CLIENT_SECRET="gitea-client-secret"

# Keycloak endpoints
# Browser-facing (auth): user's browser hits localhost
AUTH_URL="http://localhost:${KEYCLOAK_PORT}/realms/srw/protocol/openid-connect/auth"
# Server-to-server (token, profile): Gitea container hits Keycloak container
TOKEN_URL="http://keycloak:8080/realms/srw/protocol/openid-connect/token"
PROFILE_URL="http://keycloak:8080/realms/srw/protocol/openid-connect/userinfo"
# Discovery URL (internal, for JWKS and metadata)
DISCOVER_URL="http://keycloak:8080/realms/srw/.well-known/openid-configuration"

echo "Registering Keycloak OAuth2 provider in Gitea..."
echo "  Container: ${GITEA_CONTAINER}"
echo "  Keycloak port: ${KEYCLOAK_PORT}"
echo ""

# Check if Gitea container is running
if ! ${CONTAINER_RUNTIME} inspect --format='{{.State.Running}}' "${GITEA_CONTAINER}" 2>/dev/null | grep -q true; then
    echo "ERROR: Gitea container '${GITEA_CONTAINER}' is not running."
    echo "Start it with: podman-compose -f docker-compose.dev.yaml up -d gitea keycloak"
    exit 1
fi

# Check if auth source already exists
if ${CONTAINER_RUNTIME} exec "${GITEA_CONTAINER}" gitea admin auth list 2>/dev/null | grep -q "Keycloak"; then
    echo "Keycloak auth source already registered in Gitea. Skipping."
    echo ""
    echo "To re-register, first delete the existing source:"
    echo "  ${CONTAINER_RUNTIME} exec ${GITEA_CONTAINER} gitea admin auth delete --id <ID>"
    echo "  (find the ID with: ${CONTAINER_RUNTIME} exec ${GITEA_CONTAINER} gitea admin auth list)"
    exit 0
fi

# Register Keycloak as OAuth2 provider
${CONTAINER_RUNTIME} exec "${GITEA_CONTAINER}" gitea admin auth add-oauth \
    --name "Keycloak" \
    --provider "openidConnect" \
    --key "${CLIENT_ID}" \
    --secret "${CLIENT_SECRET}" \
    --auto-discover-url "${DISCOVER_URL}" \
    --use-custom-urls \
    --custom-auth-url "${AUTH_URL}" \
    --custom-token-url "${TOKEN_URL}" \
    --custom-profile-url "${PROFILE_URL}" \
    --group-claim-name "groups" \
    --admin-group "admin" \
    --skip-local-2fa

echo ""
echo "Done! Keycloak OAuth2 provider registered in Gitea."
echo ""
echo "Users can now:"
echo "  1. Open Gitea at http://localhost:3000"
echo "  2. Click 'Sign in with Keycloak'"
echo "  3. Authenticate via Keycloak (admin / admin)"
echo "  4. Gitea auto-creates their account on first OIDC login"
