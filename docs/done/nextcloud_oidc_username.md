# Nextcloud OIDC User Gets UUID as Username

**Status:** Open  
**Priority:** Low (workaround in place)  
**Date:** 2026-04-08

## Problem

When a user logs into Nextcloud via Keycloak OIDC SSO, their Nextcloud account is created with:
- **User ID:** `18eeea34-b0b2-4de1-bed3-0d8883b876d4` (Keycloak `sub` claim UUID)
- **Display name:** `18eeea34-b0b2-4de1-bed3-0d8883b876d4` (same UUID)
- **Email:** correctly set from the `email` claim

This makes the user unrecognizable in Nextcloud's UI and caused session folder shares to silently fail — `resolve_nc_username()` tried the user's email and display name as exact user-ID lookups, neither matched the UUID.

## Root Cause

Two configuration issues combine:

### 1. `--unique-uid=0` in OIDC provider setup

`docker/nextcloud/setup-nextcloud.sh:69` (also `deployment/19-nextcloud.yaml:84`):
```bash
occ user_oidc:provider "$PROVIDER_NAME" \
    --clientid="$CLIENT_ID" \
    --clientsecret="$CLIENT_SECRET" \
    --discoveryuri="$DISCOVERY_URI" \
    --unique-uid=0 \       # <-- uses `sub` (UUID) as NC username
    --check-bearer=1 \
    --group-provisioning=1
```

No `--mapping-uid` flag is set, so Nextcloud defaults to using the `sub` claim (Keycloak's internal UUID) as the user ID.

### 2. Missing `profile` scope mappers in Keycloak

`docker/keycloak/realm-export.json` defines the `profile` client scope but has **no protocol mappers** for it. Keycloak never sends `preferred_username`, `name`, `given_name`, or `family_name` claims. The `email` scope works correctly (has mappers), but profile-related claims are empty.

As a result, Nextcloud receives no human-readable identifier and falls back to the UUID for both user ID and display name.

## Current Workaround

`orchestrator/services/nextcloud_admin.py` — `resolve_nc_username()` now has a two-pass strategy:
1. Exact user-ID lookup (original behavior)
2. Search API fallback (`/ocs/v2.php/cloud/users?search=<email>`) that matches by email

This finds the UUID-named user via their email address and enables session folder sharing.

## Proper Fix

### Step 1: Add profile scope mappers to Keycloak realm

In `docker/keycloak/realm-export.json`, add protocol mappers to the `profile` client scope:

| Mapper Name | Protocol | Mapper Type | User Attribute | Token Claim |
|---|---|---|---|---|
| `username` | openid-connect | User Property | `username` | `preferred_username` |
| `full name` | openid-connect | User's Full Name | — | `name` |
| `given name` | openid-connect | User Property | `firstName` | `given_name` |
| `family name` | openid-connect | User Property | `lastName` | `family_name` |

### Step 2: Configure Nextcloud to use `preferred_username`

Update the OIDC provider setup in `docker/nextcloud/setup-nextcloud.sh` and `deployment/19-nextcloud.yaml`:

```bash
occ user_oidc:provider "$PROVIDER_NAME" \
    --clientid="$CLIENT_ID" \
    --clientsecret="$CLIENT_SECRET" \
    --discoveryuri="$DISCOVERY_URI" \
    --unique-uid=1 \
    --mapping-uid=preferred_username \
    --mapping-displayName=name \
    --check-bearer=1 \
    --group-provisioning=1
```

### Step 3: Migrate existing user (optional)

The existing UUID-named user would need to be either:
- Deleted and re-provisioned on next login (data loss)
- Renamed via `occ user:setting` commands (manual)
- Left as-is (the search fallback handles it)

## Files Involved

| File | What |
|---|---|
| `docker/keycloak/realm-export.json` | Keycloak realm config — missing profile mappers |
| `docker/nextcloud/setup-nextcloud.sh:65-71` | OIDC provider setup — `--unique-uid=0`, no `--mapping-uid` |
| `deployment/19-nextcloud.yaml:70-99` | K8s equivalent of the setup script |
| `docker/keycloak/setup-nextcloud-oidc.sh:86-92` | Deprecated setup script (same issue) |
| `orchestrator/services/nextcloud_admin.py:149-190` | `resolve_nc_username()` with search fallback (workaround) |
