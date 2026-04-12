#!/usr/bin/env bash
# =============================================================================
# Keycloak OpenCloud client seeder
# =============================================================================
#
# Creates (or verifies) the two Keycloak clients the OpenCloud backend needs:
#
#   * opencloud-web           — public client for user browser login
#   * opencloud-orchestrator  — confidential service-account client used by
#                               orchestrator/services/cloud/opencloud.py
#
# ...and makes sure the orchestrator's service-account user is a member of the
# `opencloud-admin` group. That group membership is what gives the service
# account's access tokens admin privileges in OpenCloud (the proxy maps the
# `opencloud-admin` group claim to the OpenCloud admin role).
#
# If the clients already exist (e.g. via realm-export.json), the script is
# a no-op verification. Safe to re-run.
#
# Usage:
#   ./docker/keycloak/setup-opencloud-client.sh
#
# Env overrides:
#   CONTAINER_RUNTIME       — podman (default) or docker
#   KEYCLOAK_CONTAINER      — container name (default: srw-keycloak)
#   KEYCLOAK_ADMIN_USER     — admin username (default: admin)
#   KEYCLOAK_ADMIN_PASSWORD — admin password (default: admin)
#   KEYCLOAK_REALM          — realm id (default: srw)
#   OPENCLOUD_KEYCLOAK_CLIENT_SECRET — service-account secret
#                             (default: opencloud-orchestrator-local-secret)
# =============================================================================
set -euo pipefail

CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-podman}"
KC_CONTAINER="${KEYCLOAK_CONTAINER:-srw-keycloak}"
KC_ADMIN="${KEYCLOAK_ADMIN_USER:-admin}"
KC_ADMIN_PASS="${KEYCLOAK_ADMIN_PASSWORD:-admin}"
KC_REALM="${KEYCLOAK_REALM:-srw}"
ORCHESTRATOR_CLIENT_ID="opencloud-orchestrator"
WEB_CLIENT_ID="opencloud-web"
# OpenCloud's built-in role assignment driver ships with default claim values
# baked into the binary: `opencloudAdmin`, `opencloudSpaceadmin`, `opencloudUser`,
# `opencloudGuest`. The Keycloak group name MUST match exactly. The role mapping
# itself is not overridable via environment variables (`This setting can only be
# configured in the configuration file and not via environment variables`), so
# matching the default is the zero-config path.
ADMIN_GROUP="opencloudAdmin"
CLIENT_SECRET="${OPENCLOUD_KEYCLOAK_CLIENT_SECRET:-opencloud-orchestrator-local-secret}"

# kcadm.sh lives inside the Keycloak container at /opt/keycloak/bin/
kcadm() {
    ${CONTAINER_RUNTIME} exec "${KC_CONTAINER}" /opt/keycloak/bin/kcadm.sh "$@"
}

log() {
    printf "[kc-opencloud] %s\n" "$*"
}

# ---- Preflight --------------------------------------------------------------

if ! ${CONTAINER_RUNTIME} inspect --format='{{.State.Running}}' "${KC_CONTAINER}" 2>/dev/null | grep -q true; then
    log "ERROR: Keycloak container '${KC_CONTAINER}' is not running."
    log "Start with: podman-compose up -d keycloak"
    exit 1
fi

log "Authenticating as ${KC_ADMIN}..."
kcadm config credentials \
    --server http://localhost:8080 \
    --realm master \
    --user "${KC_ADMIN}" \
    --password "${KC_ADMIN_PASS}" >/dev/null

# ---- opencloud-admin group --------------------------------------------------

log "Ensuring group '${ADMIN_GROUP}'..."
GROUP_ID="$(kcadm get groups -r "${KC_REALM}" -q search="${ADMIN_GROUP}" --fields id,name 2>/dev/null \
    | grep -B1 "\"name\" : \"${ADMIN_GROUP}\"" | grep '"id"' | sed -E 's/.*"id" : "([^"]+)".*/\1/' || true)"
if [ -z "${GROUP_ID}" ]; then
    GROUP_ID="$(kcadm create groups -r "${KC_REALM}" -s name="${ADMIN_GROUP}" -i)"
    log "Created group ${ADMIN_GROUP} (id=${GROUP_ID})"
else
    log "Group ${ADMIN_GROUP} already exists (id=${GROUP_ID})"
fi

# ---- opencloud-web (public client) ------------------------------------------

log "Ensuring client '${WEB_CLIENT_ID}'..."
if kcadm get clients -r "${KC_REALM}" -q clientId="${WEB_CLIENT_ID}" 2>/dev/null | grep -q "\"clientId\" : \"${WEB_CLIENT_ID}\""; then
    log "Client ${WEB_CLIENT_ID} already exists."
else
    kcadm create clients -r "${KC_REALM}" \
        -s clientId="${WEB_CLIENT_ID}" \
        -s name="OpenCloud Web" \
        -s description="OpenCloud browser login (Phase 2)" \
        -s enabled=true \
        -s publicClient=true \
        -s standardFlowEnabled=true \
        -s 'redirectUris=["http://localhost:9200/*"]' \
        -s 'webOrigins=["http://localhost:9200"]' \
        -s 'attributes={"pkce.code.challenge.method":"S256"}' >/dev/null
    log "Created client ${WEB_CLIENT_ID}"
fi

# ---- opencloud-orchestrator (service-account client) -----------------------

log "Ensuring client '${ORCHESTRATOR_CLIENT_ID}'..."
ORCH_UUID="$(kcadm get clients -r "${KC_REALM}" -q clientId="${ORCHESTRATOR_CLIENT_ID}" --fields id 2>/dev/null \
    | grep '"id"' | head -1 | sed -E 's/.*"id" : "([^"]+)".*/\1/' || true)"
if [ -z "${ORCH_UUID}" ]; then
    ORCH_UUID="$(kcadm create clients -r "${KC_REALM}" \
        -s clientId="${ORCHESTRATOR_CLIENT_ID}" \
        -s name="OpenCloud Orchestrator Service Account" \
        -s enabled=true \
        -s publicClient=false \
        -s standardFlowEnabled=false \
        -s directAccessGrantsEnabled=false \
        -s serviceAccountsEnabled=true \
        -s "secret=${CLIENT_SECRET}" \
        -i)"
    log "Created client ${ORCHESTRATOR_CLIENT_ID} (id=${ORCH_UUID})"
else
    log "Client ${ORCHESTRATOR_CLIENT_ID} already exists (id=${ORCH_UUID})"
    # Refresh the secret to whatever the env var says.
    kcadm update "clients/${ORCH_UUID}" -r "${KC_REALM}" \
        -s "secret=${CLIENT_SECRET}" >/dev/null
fi

# ---- Add service-account user to opencloud-admin group ---------------------

log "Fetching service-account user for ${ORCHESTRATOR_CLIENT_ID}..."
SA_USER_ID="$(kcadm get "clients/${ORCH_UUID}/service-account-user" -r "${KC_REALM}" --fields id 2>/dev/null \
    | grep '"id"' | head -1 | sed -E 's/.*"id" : "([^"]+)".*/\1/' || true)"
if [ -z "${SA_USER_ID}" ]; then
    log "ERROR: could not resolve service-account user for ${ORCHESTRATOR_CLIENT_ID}"
    exit 1
fi

log "Adding service account to group ${ADMIN_GROUP}..."
kcadm update "users/${SA_USER_ID}/groups/${GROUP_ID}" -r "${KC_REALM}" \
    -s realm="${KC_REALM}" \
    -s userId="${SA_USER_ID}" \
    -s groupId="${GROUP_ID}" -n >/dev/null 2>&1 || true

# ---- Profile attributes on the service account user -----------------------
# OpenCloud's proxy auto-provisions a local user record from the token claims
# on first call, and rejects the request with
#   "missing claim 'name' (displayName)"
# if the token doesn't carry a name. Service account users in Keycloak have
# no profile by default, so we populate firstName/lastName/email here — the
# `profile` + `email` client scopes (added below) then map these onto the
# token as the `name`, `given_name`, `family_name`, `email` claims.

log "Populating profile attributes on service-account user..."
kcadm update "users/${SA_USER_ID}" -r "${KC_REALM}" \
    -s firstName="OpenCloud" \
    -s lastName="Orchestrator" \
    -s email="orchestrator@srw.local" \
    -s enabled=true >/dev/null 2>&1 || true

# ---- Default client scopes -------------------------------------------------
# Without these, client_credentials tokens from the orchestrator lack the
# claims that OpenCloud's proxy needs:
#   * openid  — lets the proxy call /userinfo (else: "insufficient_scope")
#   * profile — adds `name` / `given_name` / `family_name` (else: auto-provision
#               fails with "missing claim 'name'")
#   * email   — adds `email` (else: same auto-provision failure on strict mode)
#   * groups  — surfaces the `opencloudAdmin` group membership as the `groups`
#               claim the proxy reads for role assignment
# `openid` is a reserved scope in Keycloak and doesn't need to be added as a
# default client scope explicitly — the orchestrator requests it per-call and
# Keycloak accepts it for any OIDC client. `profile`, `email`, `groups` are
# regular client scopes and MUST be attached to the client, otherwise
# Keycloak rejects the token request with `invalid_scope`.

log "Attaching default client scopes (profile, email, groups) to orchestrator..."
for SCOPE_NAME in profile email groups; do
    SCOPE_ID="$(kcadm get client-scopes -r "${KC_REALM}" --fields id,name 2>/dev/null \
        | grep -B1 "\"name\" : \"${SCOPE_NAME}\"" | grep '"id"' \
        | head -1 | sed -E 's/.*"id" : "([^"]+)".*/\1/' || true)"
    if [ -z "${SCOPE_ID}" ]; then
        log "WARNING: client scope '${SCOPE_NAME}' not found in realm — skipping"
        continue
    fi
    kcadm update "clients/${ORCH_UUID}/default-client-scopes/${SCOPE_ID}" \
        -r "${KC_REALM}" >/dev/null 2>&1 || true
done

# ---- Default client scopes for the web client ------------------------------
# The browser's oidc-client library requests `openid profile email` by default.
# If these aren't registered as default scopes on the web client, Keycloak
# returns `invalid_scope` and the OIDC redirect fails. `groups` is needed so
# OpenCloud can resolve the user's role from the token's `groups` claim.
# `openid` may need to be created first — it's a reserved scope name in OIDC
# but Keycloak doesn't always ship it as a client scope entity in all realm
# configurations.

log "Attaching default client scopes (openid, profile, email, groups) to web client..."
WEB_UUID="$(kcadm get clients -r "${KC_REALM}" -q clientId="${WEB_CLIENT_ID}" --fields id 2>/dev/null \
    | grep '"id"' | head -1 | sed -E 's/.*"id" : "([^"]+)".*/\1/' || true)"
if [ -z "${WEB_UUID}" ]; then
    log "WARNING: web client '${WEB_CLIENT_ID}' not found — skipping scope attachment"
else
    for SCOPE_NAME in openid profile email groups; do
        SCOPE_ID="$(kcadm get client-scopes -r "${KC_REALM}" --fields id,name 2>/dev/null \
            | grep -B1 "\"name\" : \"${SCOPE_NAME}\"" | grep '"id"' \
            | head -1 | sed -E 's/.*"id" : "([^"]+)".*/\1/' || true)"
        if [ -z "${SCOPE_ID}" ]; then
            # `openid` doesn't exist as a client scope entity — create it.
            if [ "${SCOPE_NAME}" = "openid" ]; then
                log "Creating client scope 'openid'..."
                SCOPE_ID="$(kcadm create client-scopes -r "${KC_REALM}" \
                    -s name=openid -s protocol=openid-connect \
                    -s 'attributes={"include.in.token.scope":"true"}' -i 2>/dev/null || true)"
            fi
            if [ -z "${SCOPE_ID}" ]; then
                log "WARNING: client scope '${SCOPE_NAME}' not found — skipping"
                continue
            fi
        fi
        kcadm update "clients/${WEB_UUID}/default-client-scopes/${SCOPE_ID}" \
            -r "${KC_REALM}" >/dev/null 2>&1 || true
    done
fi

log "Done. Next steps:"
log "  1. Set OPENCLOUD_KEYCLOAK_CLIENT_SECRET=${CLIENT_SECRET} in your .env"
log "     (the default above works out of the box with the dev compose stack)"
log "  2. Restart the orchestrator: podman-compose restart orchestrator"
log "  3. Click 'Test' in the Cockpit Cloud Storage panel to verify."
exit 0
