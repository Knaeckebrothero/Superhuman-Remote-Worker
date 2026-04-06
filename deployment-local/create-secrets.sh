#!/usr/bin/env bash
# =============================================================================
# Create K8s secrets for local K3s from .env file
# =============================================================================
#
# Reads the project .env file and creates the srw-secrets and vm-ssh-key
# Kubernetes secrets that the production manifests expect from Vault/ESO.
#
# Usage:
#   ./deployment-local/create-secrets.sh          # Uses .env in repo root
#   ./deployment-local/create-secrets.sh /path/.env  # Custom .env path
#
# Re-run to update secrets (uses --dry-run + apply pattern).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${1:-$REPO_DIR/.env}"
NAMESPACE="superhuman-remote-worker"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: .env file not found at $ENV_FILE"
    echo "Copy .env.example to .env and configure it first."
    exit 1
fi

# Parse .env file safely (handles values with commas, special chars)
while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    export "$key=$value"
done < "$ENV_FILE"

echo "Creating secrets in namespace: $NAMESPACE"
echo "Reading from: $ENV_FILE"

# Ensure namespace exists
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || \
    kubectl create namespace "$NAMESPACE"

# --- Helper: default to empty string if var is unset ---
v() { echo "${!1:-}"; }

# =============================================================================
# srw-secrets — main application secret
# =============================================================================
# Maps .env vars to the keys that K8s manifests reference via secretKeyRef.
# Keys not in .env get sensible local defaults.
#
# IMPORTANT: Database URLs must use K8s service names, not localhost.
# The .env file uses localhost (for docker-compose dev), but in K8s the
# services are reachable by their service names.
PG_USER="${POSTGRES_USER:-srw}"
PG_PASS="${POSTGRES_PASSWORD:-srw_password}"
VPG_USER="${VECTOR_POSTGRES_USER:-srw}"
VPG_PASS="${VECTOR_POSTGRES_PASSWORD:-srw_password}"

K8S_DATABASE_URL="postgresql://${PG_USER}:${PG_PASS}@srw-postgres:5432/${POSTGRES_DB:-srw}"
K8S_VECTOR_DB_URL="postgresql://${VPG_USER}:${VPG_PASS}@srw-postgres-vector:5432/${VECTOR_POSTGRES_DB:-srw_vector}"

kubectl create secret generic srw-secrets \
    --namespace="$NAMESPACE" \
    --from-literal="DATABASE_URL=${K8S_DATABASE_URL}" \
    --from-literal="VECTOR_DB_URL=${K8S_VECTOR_DB_URL}" \
    --from-literal="POSTGRES_USER=${PG_USER}" \
    --from-literal="POSTGRES_PASSWORD=${PG_PASS}" \
    --from-literal="VECTOR_POSTGRES_USER=${VPG_USER}" \
    --from-literal="OPENAI_API_KEY=$(v OPENAI_API_KEY)" \
    --from-literal="ANTHROPIC_API_KEY=$(v ANTHROPIC_API_KEY)" \
    --from-literal="GROQ_API_KEY=$(v GROQ_API_KEY)" \
    --from-literal="OPENROUTER_API_KEY=$(v OPENROUTER_API_KEY)" \
    --from-literal="GOOGLE_API_KEY=$(v GOOGLE_API_KEY)" \
    --from-literal="TAVILY_API_KEY=$(v TAVILY_API_KEY)" \
    --from-literal="SEMANTIC_SCHOLAR_API_KEY=$(v SEMANTIC_SCHOLAR_API_KEY)" \
    --from-literal="VISION_API_KEY=$(v VISION_API_KEY)" \
    --from-literal="WHISPER_API_KEY=$(v WHISPER_API_KEY)" \
    --from-literal="EMBEDDING_API_KEY=$(v EMBEDDING_API_KEY)" \
    --from-literal="CITATION_EMBEDDING_KEY=$(v CITATION_EMBEDDING_KEY)" \
    --from-literal="CITATION_DB_URL=${K8S_VECTOR_DB_URL}" \
    --from-literal="UNPAYWALL_EMAIL=$(v UNPAYWALL_EMAIL)" \
    --from-literal="CLAUDE_CODE_OAUTH_TOKEN=$(v CLAUDE_CODE_OAUTH_TOKEN)" \
    --from-literal="CODEX_MANAGEMENT_KEY=$(v CODEX_MANAGEMENT_KEY)" \
    --from-literal="BUILDER_API_KEY=${BUILDER_API_KEY:-$(v OPENAI_API_KEY)}" \
    --from-literal="GITEA_ADMIN_USER=${GITEA_ADMIN_USER:-srw}" \
    --from-literal="GITEA_ADMIN_PASSWORD=${GITEA_ADMIN_PASSWORD:-srw_gitea}" \
    --from-literal="GITEA_OIDC_CLIENT_SECRET=${GITEA_OIDC_CLIENT_SECRET:-gitea-local-secret}" \
    --from-literal="KEYCLOAK_ADMIN_USER=${KEYCLOAK_ADMIN_USER:-admin}" \
    --from-literal="KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD:-admin}" \
    --from-literal="KC_DB_PASSWORD=${KC_DB_PASSWORD:-keycloak}" \
    --from-literal="KC_REALM_ADMIN_PASSWORD=${KC_REALM_ADMIN_PASSWORD:-admin}" \
    --from-literal="NEXTCLOUD_ADMIN_PASSWORD=${NEXTCLOUD_ADMIN_PASSWORD:-admin}" \
    --from-literal="NEXTCLOUD_OIDC_CLIENT_SECRET=${NEXTCLOUD_OIDC_CLIENT_SECRET:-nextcloud-local-secret}" \
    --from-literal="NEXTCLOUD_AGENT_PASSWORD=${NEXTCLOUD_AGENT_PASSWORD:-agent-service-dev}" \
    --from-literal="NEXTCLOUD_S3_ACCESS_KEY=${NEXTCLOUD_S3_ACCESS_KEY:-srw}" \
    --from-literal="NEXTCLOUD_S3_SECRET_KEY=${NEXTCLOUD_S3_SECRET_KEY:-srw_password}" \
    --from-literal="PGADMIN_OIDC_CLIENT_SECRET=${PGADMIN_OIDC_CLIENT_SECRET:-pgadmin-local-secret}" \
    --from-literal="MCP_OIDC_CLIENT_SECRET=${MCP_OIDC_CLIENT_SECRET:-mcp-local-secret}" \
    --from-literal="MCP_INTERNAL_KEY=${MCP_INTERNAL_KEY:-dev-internal-key}" \
    --from-literal="NEO4J_AUTH=neo4j/${NEO4J_PASSWORD:-neo4j_password}" \
    --from-literal="NEO4J_USERNAME=${NEO4J_USERNAME:-neo4j}" \
    --from-literal="NEO4J_PASSWORD=${NEO4J_PASSWORD:-neo4j_password}" \
    --from-literal="DEFAULT_DS_NEO4J_USERNAME=${DEFAULT_DS_NEO4J_USERNAME:-neo4j}" \
    --from-literal="DEFAULT_DS_WEBDAV_USERNAME=${DEFAULT_DS_WEBDAV_USERNAME:-admin}" \
    --from-literal="DEFAULT_DS_WEBDAV_PASSWORD=${DEFAULT_DS_WEBDAV_PASSWORD:-admin}" \
    --from-literal="S3_ACCESS_KEY=${S3_ACCESS_KEY:-srw}" \
    --from-literal="S3_SECRET_KEY=${S3_SECRET_KEY:-srw_password}" \
    --from-literal="SMTP_USER=$(v SMTP_USER)" \
    --from-literal="SMTP_PASSWORD=$(v SMTP_PASSWORD)" \
    --from-literal="IMAP_USER=$(v IMAP_USER)" \
    --from-literal="IMAP_PASSWORD=$(v IMAP_PASSWORD)" \
    --from-literal="SLACK_WEBHOOK_URL=$(v SLACK_WEBHOOK_URL)" \
    --from-literal="DISCORD_WEBHOOK_URL=$(v DISCORD_WEBHOOK_URL)" \
    --from-literal="NTFY_TOKEN=$(v NTFY_TOKEN)" \
    --from-literal="TAILSCALE_AUTH_KEY=$(v TAILSCALE_AUTH_KEY)" \
    --from-literal="VPN_CLUSTER_HOST=$(v VPN_CLUSTER_HOST)" \
    --from-literal="VPN_CLUSTER_USER=$(v VPN_CLUSTER_USER)" \
    --from-literal="VPN_CLUSTER_PASS=$(v VPN_CLUSTER_PASS)" \
    --from-literal="VPN_CLUSTER_REALM=$(v VPN_CLUSTER_REALM)" \
    --from-literal="VPN_CLUSTER_TRUSTED_CERT=$(v VPN_CLUSTER_TRUSTED_CERT)" \
    --from-literal="VPN_CLUSTER_LLM_ENDPOINT=$(v VPN_CLUSTER_LLM_ENDPOINT)" \
    --from-literal="VPN_RESEARCH_HOST=$(v VPN_RESEARCH_HOST)" \
    --from-literal="VPN_RESEARCH_USER=$(v VPN_RESEARCH_USER)" \
    --from-literal="VPN_RESEARCH_PASS=$(v VPN_RESEARCH_PASS)" \
    --from-literal="VPN_RESEARCH_REALM=$(v VPN_RESEARCH_REALM)" \
    --from-literal="VPN_RESEARCH_TRUSTED_CERT=$(v VPN_RESEARCH_TRUSTED_CERT)" \
    --from-literal="VPN_WORKSTATION_USER=$(v VPN_WORKSTATION_USER)" \
    --from-literal="VPN_WORKSTATION_PASS=$(v VPN_WORKSTATION_PASS)" \
    --from-literal="VPN_WORKSTATION_REALM=$(v VPN_WORKSTATION_REALM)" \
    --from-literal="VPN_WORKSTATION_ENDPOINT=$(v VPN_WORKSTATION_ENDPOINT)" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "  srw-secrets: ok"

# =============================================================================
# vm-ssh-key — SSH key for workspace/VM access
# =============================================================================
# Generate a fresh key pair if none exists, or use existing.

SSH_KEY_DIR="$REPO_DIR/.local-ssh"
SSH_KEY_FILE="$SSH_KEY_DIR/id_ed25519"

if [[ ! -f "$SSH_KEY_FILE" ]]; then
    echo "  Generating SSH key pair for workspace access..."
    mkdir -p "$SSH_KEY_DIR"
    ssh-keygen -t ed25519 -f "$SSH_KEY_FILE" -N "" -C "srw-local-workspace" >/dev/null
fi

kubectl create secret generic vm-ssh-key \
    --namespace="$NAMESPACE" \
    --from-file="ssh-privatekey=$SSH_KEY_FILE" \
    --from-file="ssh-publickey=${SSH_KEY_FILE}.pub" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "  vm-ssh-key: ok"

echo ""
echo "Secrets created. Deploy with:"
echo "  kubectl apply -k deployment-local/"
