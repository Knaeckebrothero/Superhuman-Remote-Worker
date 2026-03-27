# MCP OAuth Bridge

Make the MCP server accessible as a connector in Claude.ai, ChatGPT, and other platforms that require OAuth 2.1 — without replacing the existing `srw_*` token system.

## Context

The MCP server currently authenticates via `srw_*` bearer tokens. Users generate tokens in the cockpit settings page (already authenticated via Keycloak), configure scopes (user/project/admin), set expiry, and paste the token into their `.mcp.json`. This works for Claude Code CLI (via `headers`) and any client that supports static bearer tokens.

**Problem:** Claude.ai custom connectors and ChatGPT MCP connectors require OAuth 2.1 Authorization Code flow. They don't accept pasted bearer tokens — the platform initiates an OAuth handshake with the MCP server. Without OAuth endpoints, the MCP server can't be added as a connector in these web UIs.

**Constraint:** The existing token system has valuable features that a pure OIDC passthrough (e.g., `RemoteAuthProvider` pointing at Keycloak) would lose:
- Project-scoped tokens (`project:<uuid>`)
- Per-token expiry (30d, 90d, 1y, never) — Keycloak sets expiry per-client, not per-token
- Admin-only full-access scope
- Revocation of individual tokens via cockpit UI
- Audit trail (`last_used_at` tracking)

## MCP Authorization Spec (June 2025 Revision)

The MCP authorization spec was substantially rewritten in June 2025. Key changes from the original March 2025 version:

- **MCP server = Resource Server only.** The MCP server no longer acts as an authorization server. A separate authorization server (Keycloak, Auth0, etc.) handles login and token issuance.
- **RFC 9728 (Protected Resource Metadata):** MCP servers MUST expose `/.well-known/oauth-protected-resource` describing themselves and pointing to their authorization server(s).
- **RFC 8707 (Resource Indicators):** Clients MUST include a `resource` parameter in authorization requests to bind tokens to a specific MCP server.
- **PKCE S256 is mandatory** for all clients — plain method is rejected.
- **DCR (Dynamic Client Registration)** is a SHOULD (recommended), not a MUST. But in practice ChatGPT requires it (see platform-specific notes below).

The November 2025 update added **CIMD (Client ID Metadata Documents)** as a future alternative to DCR, where the `client_id` is a URL pointing to client metadata. Not yet widely adopted.

## Platform-Specific Requirements

### Claude.ai

- **Callback URL:** `https://claude.ai/api/mcp/auth_callback` (also allowlist `https://claude.com/api/mcp/auth_callback` — domain migration possible)
- **Client name:** `"Claude"`
- **DCR:** Supported. Claude registers once and re-registers on `invalid_client` errors. Since July 2025, users can also manually enter `client_id`/`client_secret` for servers without DCR.
- **Token refresh:** Supported and expected — Claude handles token expiry and refresh gracefully.
- **Re-registration signal:** Return HTTP 401 with `error=invalid_client` from the token endpoint to trigger re-registration.
- **IP allowlisting:** Claude publishes IP ranges for server-side filtering.

### ChatGPT

- **Callback URL:** `https://chatgpt.com/connector/oauth/{callback_id}` — **dynamic per session** since March 2026. Each new connection generates a unique callback URL.
- **DCR:** Effectively required. ChatGPT registers a new OAuth client per session, generating many short-lived clients. There is no manual client_id fallback for connectors.
- **PKCE:** Enforced — `S256` required, refuses to proceed without it.
- **Developer Mode:** Required for regular ChatGPT conversations (Settings > Connectors > Developer Mode). Pro/Team/Enterprise/Edu only.
- **Static tokens:** NOT supported for connectors — OAuth only.
- **Cleanup needed:** ChatGPT's per-session DCR creates many clients. Implement a cleanup policy (e.g., expire unused clients after 30 days).

### Claude Code CLI

- **No OAuth needed.** Static bearer tokens via `headers` in `.mcp.json` continue to work unchanged.
- **DCR supported** but optional — the CLI can also use the OAuth flow if available.

### OpenWebUI

- **Flexible.** Supports both static bearer tokens and OAuth. No changes needed for current users.

## Design: OAuthProxy Subclass with `srw_*` Token Issuance

FastMCP 3.0+ includes `OAuthProxy` — a battle-tested OAuth proxy that handles DCR, PKCE, consent pages, metadata endpoints, and token exchange. Rather than reimplementing these from scratch, we subclass `OAuthProxy` and override the token issuance step to create `srw_*` tokens instead of FastMCP's default JWTs.

For backward compatibility, `MultiAuth` composes the OAuth proxy (for web UI flows) with the existing `McpTokenVerifier` (for CLI static tokens). Both paths resolve to the same user_id + scope model.

```
Claude.ai / ChatGPT / OpenWebUI             Claude Code CLI
    │                                              │
    │  OAuth 2.1 Authorization Code + PKCE         │  Authorization: Bearer srw_*
    ▼                                              ▼
┌───────────────────────────────────────────────────────────────┐
│  MCP Server (mcp.superhuman-remote-worker.com)                │
│                                                               │
│  MultiAuth                                                    │
│  ├── SRWOAuthProxy (subclass of OAuthProxy)                   │
│  │   ├── /.well-known/oauth-protected-resource  (RFC 9728)    │
│  │   ├── /oauth/register   (DCR — handles ChatGPT dynamic    │
│  │   │                      redirect URIs)                    │
│  │   ├── /oauth/authorize  → redirect to Keycloak login       │
│  │   ├── /oauth/callback   → Keycloak identity + consent page │
│  │   ├── /oauth/token      → exchanges code for srw_* token   │
│  │   └── Token validation  → validates srw_* (not JWT)        │
│  │                                                            │
│  └── McpTokenVerifier (existing, unchanged)                   │
│       └── validates srw_* tokens from .mcp.json headers       │
│                                                               │
│  Both paths → user_id + scope → _get_client() → MCP tools    │
└───────────────────────────────────────────────────────────────┘
         │                             │
         ▼                             ▼
┌─────────────────┐          ┌──────────────────┐
│   Keycloak      │          │   PostgreSQL     │
│   (identity)    │          │   mcp_tokens     │
│                 │          │   oauth_clients  │
└─────────────────┘          └──────────────────┘
```

### Flow in Detail

**Step 1 — Discovery.** Platform makes an unauthenticated request to the MCP server. Server returns `401 Unauthorized` with:
```
WWW-Authenticate: Bearer resource_metadata="https://mcp.superhuman-remote-worker.com/.well-known/oauth-protected-resource"
```
Platform fetches the protected resource metadata, which points to the MCP server's own OAuth endpoints (the proxy acts as the authorization server from the client's perspective, while delegating identity to Keycloak).

**Step 2 — Dynamic Client Registration.** Platform `POST`s to `/oauth/register` with its `redirect_uris` and `client_name`. The proxy stores the registration and returns a `client_id`. This is handled entirely by FastMCP's `OAuthProxy` — no custom code needed. ChatGPT registers a new client per session (with a unique callback URL); Claude.ai registers once and reuses.

**Step 3 — Authorization.** Platform redirects the user's browser to `/oauth/authorize?client_id=...&code_challenge=...&scope=user&resource=https://mcp.superhuman-remote-worker.com`. The proxy:
1. Validates `client_id`, PKCE challenge (`S256` only), and `redirect_uri` against the DCR registration
2. Stores the OAuth transaction
3. Redirects to Keycloak login (`auth.superhuman-remote-worker.com/realms/srw/protocol/openid-connect/auth`)
4. Keycloak authenticates the user (same login as cockpit)

**Step 4 — Keycloak Callback + Consent.** Keycloak redirects back to `/oauth/callback` with an authorization code. The proxy:
1. Exchanges the Keycloak code for an ID token (gets user identity: `sub`, `email`, `realm_access.roles`)
2. Looks up or JIT-creates the user in the orchestrator (same as cockpit login)
3. Shows a **consent page** where the user selects:
   - Scope: "My Data Only" (default), "Project: X" (if member), "Full Access" (if admin)
   - Expiry: 30 days, 90 days, 1 year, never
   - The requesting application name (from DCR `client_name`, e.g., "Claude" or "ChatGPT")
4. On consent, creates an `srw_*` token via the existing `create_mcp_token` logic (with the selected scope, expiry, and `origin` set to `oauth:<client_name>`)
5. Issues an authorization code and redirects back to the platform

**Step 5 — Token Exchange.** Platform calls `POST /oauth/token` with the authorization code + PKCE `code_verifier`. The proxy validates and returns:
```json
{
  "access_token": "srw_...",
  "token_type": "Bearer",
  "expires_in": 7776000,
  "scope": "user",
  "refresh_token": "srw_refresh_..."
}
```

**Step 6 — Normal MCP operation.** Platform sends `Authorization: Bearer srw_...` on every MCP request. The existing `McpTokenVerifier` validates it — no changes to the validation path.

**Step 7 — Token Refresh (optional).** When the access token expires, the platform calls `POST /oauth/token` with `grant_type=refresh_token`. The proxy creates a new `srw_*` token (same scope/user), revokes the old one, and returns the new token. This avoids forcing users through the full OAuth flow again.

### What Changes vs. What Stays

| Component | Changes? | Notes |
|-----------|----------|-------|
| `McpTokenVerifier` | No | Validates `srw_*` tokens as before |
| `mcp_tokens` table | Minor | New nullable `origin` column |
| Token scoping | No | OAuth consent page maps to existing scopes |
| Token revocation | No | Cockpit revoke button works on OAuth-created tokens |
| `last_used_at` tracking | No | Still updated on every MCP request |
| MCP tools | No | Scope headers injected via same `_get_client()` path |
| Cockpit token UI | Minor | Show origin column ("manual" vs "oauth:Claude") |
| `.mcp.json` (Claude Code) | No | Static bearer token via `headers` still works |

## Implementation

### Prerequisites

- **FastMCP >= 3.1.0** required (`MultiAuth` was introduced in 3.1.0; current install is 3.0.2)
- Update `orchestrator/mcp/requirements.txt`: `fastmcp>=3.1.0`

### Subclassing OIDCProxy

`OIDCProxy` (subclass of `OAuthProxy`) auto-discovers Keycloak's authorization, token, and JWKS endpoints from the OIDC configuration URL — no need to hardcode them. We override two methods to swap JWT issuance for `srw_*` token creation, plus the consent page for scope/expiry selection:

**Override points used** (all confirmed overridable from FastMCP source):

| Method | Purpose | FastMCP default |
|--------|---------|-----------------|
| `exchange_authorization_code` | Token issuance | Issues HS256 JWT with JTI mapping |
| `load_access_token` | Token validation | Verifies JWT, swaps for upstream token |
| `_show_consent_page` | Consent UI | Generic approve/deny page |
| `_submit_consent` | Consent form handler | Stores consent in signed cookie |

```python
# orchestrator/mcp/oauth_bridge.py

from fastmcp.server.auth import OIDCProxy, AccessToken
from mcp.shared.auth import OAuthToken

class SRWOAuthProxy(OIDCProxy):
    """OIDC proxy that issues srw_* tokens instead of FastMCP JWTs.

    Keycloak handles identity (login, OIDC discovery, JWKS).
    We handle authorization (scope selection, token creation).
    """

    def __init__(self, *, mcp_verifier: McpTokenVerifier, **kwargs):
        super().__init__(**kwargs)
        self._mcp_verifier = mcp_verifier

    async def exchange_authorization_code(self, client, authorization_code):
        """Override: create srw_* token instead of JWT."""
        # 1. Get upstream Keycloak tokens from the stored authorization code
        code_model = await self._code_store.get(key=authorization_code.code)
        if not code_model:
            raise ValueError("Invalid or expired authorization code")
        idp_tokens = code_model.idp_tokens

        # Delete code (single-use)
        await self._code_store.delete(key=authorization_code.code)

        # 2. Extract user identity from Keycloak ID token
        user_info = decode_id_token(idp_tokens["id_token"])

        # 3. Resolve scope and expiry from the consent step
        #    (stored in authorization_code.scopes by _submit_consent)
        scope = authorization_code.scopes[0] if authorization_code.scopes else "user"
        expiry_days = self._resolve_expiry_from_scopes(authorization_code.scopes)

        # 4. Create srw_* token via orchestrator API (same logic as cockpit)
        srw_token = await create_oauth_token(
            user_sub=user_info["sub"],
            scope=scope,
            origin=f"oauth:{client.client_name or 'unknown'}",
            expires_in_days=expiry_days,
        )

        # 5. Optionally create a refresh token (separate srw_* entry)
        refresh = None
        if idp_tokens.get("refresh_token"):
            refresh = await create_oauth_refresh_token(
                user_sub=user_info["sub"],
                linked_token_id=srw_token["id"],
            )

        return OAuthToken(
            access_token=srw_token["token"],
            token_type="Bearer",
            expires_in=srw_token.get("expires_in"),
            scope=scope,
            refresh_token=refresh["token"] if refresh else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Override: validate srw_* tokens instead of FastMCP JWTs."""
        if token.startswith("srw_"):
            return await self._mcp_verifier.verify_token(token)
        return None

    async def _show_consent_page(self, request):
        """Override: add scope and expiry selectors to consent page."""
        # Extract transaction from request state (set by OAuthProxy)
        txn = request.state.oauth_transaction
        client = await self.get_client(txn.client_id)

        # Look up user's projects for scope dropdown
        projects = await get_user_projects(txn.upstream_claims.get("sub"))
        is_admin = "admin" in txn.upstream_claims.get("realm_access", {}).get("roles", [])

        return HTMLResponse(render_consent_template(
            client_name=client.client_name if client else "Unknown",
            user_email=txn.upstream_claims.get("email", ""),
            projects=projects,
            is_admin=is_admin,
        ))
```

### Server Initialization

```python
# orchestrator/mcp/server.py (modified)

from fastmcp.server.auth import MultiAuth

_transport = os.environ.get("MCP_TRANSPORT", "http").lower()
_auth = None

if _transport == "http":
    _token_verifier = McpTokenVerifier()

    if os.environ.get("MCP_OAUTH_ENABLED", "").lower() == "true":
        _oauth_proxy = SRWOAuthProxy(
            # OIDCProxy auto-discovers endpoints from this URL:
            config_url=os.environ.get(
                "MCP_OIDC_CONFIG_URL",
                "http://keycloak:8080/realms/srw/.well-known/openid-configuration",
            ),
            upstream_client_id=os.environ["MCP_OIDC_CLIENT_ID"],
            upstream_client_secret=os.environ["MCP_OIDC_CLIENT_SECRET"],
            token_verifier=_token_verifier,
            mcp_verifier=_token_verifier,
            base_url=os.environ.get(
                "MCP_BASE_URL", "https://mcp.superhuman-remote-worker.com"
            ),
            issuer_url=os.environ.get(
                "MCP_OIDC_ISSUER",
                "https://auth.superhuman-remote-worker.com/realms/srw",
            ),
            require_authorization_consent=True,
        )
        # MultiAuth: OAuth proxy owns routes, McpTokenVerifier handles CLI static tokens
        _auth = MultiAuth(server=_oauth_proxy, verifiers=[_token_verifier])
    else:
        _auth = _token_verifier

mcp = FastMCP("cockpit-debug", auth=_auth)
```

**Note:** `OIDCProxy` takes a `config_url` parameter and auto-discovers `authorization_endpoint`, `token_endpoint`, `jwks_uri`, and `revocation_endpoint` from the OIDC configuration document. The `issuer_url` is the public-facing issuer (for token validation), while `config_url` can point to the internal Keycloak URL (avoids hairpinning through the public ingress).

### What FastMCP Handles Automatically

When `OAuthProxy` is the `server` in `MultiAuth`, FastMCP automatically registers these routes:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/.well-known/oauth-protected-resource` | GET | RFC 9728 metadata — points clients to the proxy's auth endpoints |
| `/.well-known/oauth-authorization-server` | GET | RFC 8414 metadata — authorization, token, registration endpoints |
| `/oauth/register` | POST | DCR — clients self-register with redirect_uris |
| `/oauth/authorize` | GET | Starts Authorization Code + PKCE flow |
| `/oauth/token` | POST | Code-for-token exchange (and refresh) |
| `/oauth/revoke` | POST | Token revocation (optional) |

Plus the consent page UI (built-in, customizable via subclass).

### Consent Page Customization

FastMCP's `OAuthProxy` includes a production-ready consent page (`ConsentMixin`) with CSRF protection via signed cookies. The scope/expiry selectors are added via the `_show_consent_page` override shown in the `SRWOAuthProxy` class above.

The consent template is a single HTML file (`orchestrator/mcp/templates/consent.html`) styled consistently with the cockpit dark theme. The form submits scope and expiry selections, which `_submit_consent` encodes into the authorization code's scopes before redirecting back to the platform.

### New Keycloak Client

Add a confidential OIDC client to the `srw` realm. The MCP server uses this to redirect users to Keycloak login and exchange codes for identity tokens:

```yaml
# In deployment/18-keycloak.yaml realm config, clients array
- clientId: mcp-server
  name: MCP OAuth Bridge
  enabled: true
  publicClient: false
  standardFlowEnabled: true
  directAccessGrantsEnabled: false
  serviceAccountsEnabled: false
  redirectUris:
    - "http://localhost:8055/oauth/callback"
    - "https://mcp.superhuman-remote-worker.com/oauth/callback"
  webOrigins:
    - "http://localhost:8055"
    - "https://mcp.superhuman-remote-worker.com"
  defaultClientScopes:
    - openid
    - profile
    - email
    - roles
```

### Storage

FastMCP's `OAuthProxy` manages its own state for OAuth transactions, authorization codes, and DCR clients using pluggable storage backends. Default is encrypted file storage (`FileTreeStore`), but PostgreSQL and Redis backends are also supported.

For production, use FastMCP's PostgreSQL backend (or its default encrypted filesystem if K8s persistent volumes are available). This stores:

| Store | Contents | Lifetime |
|-------|----------|----------|
| `_client_store` | DCR-registered clients (client_id, redirect_uris) | Long-lived (cleanup after 30 days unused) |
| `_transaction_store` | In-flight OAuth transactions | 10 minutes |
| `_code_store` | Authorization codes + upstream tokens | 5 minutes, single-use |
| `_upstream_token_store` | Encrypted Keycloak tokens | Until srw_* token is created |

No additional `oauth_clients` or `oauth_transactions` tables needed in our schema — FastMCP handles this internally.

The only schema change is a new nullable `origin` column on `mcp_tokens`:

```sql
ALTER TABLE mcp_tokens ADD COLUMN origin TEXT;
-- Values: NULL (manual/legacy), 'oauth:Claude', 'oauth:ChatGPT', etc.
```

### Environment Variables

```bash
# Existing (unchanged)
COCKPIT_API_URL=http://localhost:8085
MCP_INTERNAL_KEY=...
MCP_HOST=0.0.0.0
MCP_PORT=8055

# New
MCP_OAUTH_ENABLED=true                      # Enable OAuth proxy (default: false)
MCP_OIDC_CLIENT_ID=mcp-server               # Keycloak client ID
MCP_OIDC_CLIENT_SECRET=...                   # Keycloak client secret
MCP_OIDC_CONFIG_URL=http://keycloak:8080/realms/srw/.well-known/openid-configuration
                                             # Internal URL for OIDC discovery (avoids hairpin)
MCP_OIDC_ISSUER=https://auth.superhuman-remote-worker.com/realms/srw
                                             # Public issuer URL (for token validation)
MCP_BASE_URL=https://mcp.superhuman-remote-worker.com  # Public URL for OAuth metadata
```

When `MCP_OAUTH_ENABLED` is false (default), behavior is identical to today — only `McpTokenVerifier` is active. The `OIDCProxy` auto-discovers all Keycloak endpoints from the config URL, so no need to hardcode authorization/token/JWKS URLs.

### Deployment

| File | Change |
|------|--------|
| `deployment/18-keycloak.yaml` | Add `mcp-server` client to realm ConfigMap |
| `deployment/23-mcp.yaml` | Add OAuth env vars (`MCP_OIDC_*`) from secrets |
| `deployment/30-ingress.yaml` | No changes (already routes `mcp.superhuman-remote-worker.com`) |

### Cockpit Settings Page

Minor update to the MCP tokens table — add an "Origin" column:
- Blank or "Manual" — created via the cockpit form
- "Claude" — created via OAuth from Claude.ai
- "ChatGPT" — created via OAuth from ChatGPT
- Other client names as registered via DCR

The "Connection" section (`.mcp.json` snippet) stays for Claude Code CLI users.

## Security Considerations

**PKCE S256 mandatory.** Enforced by FastMCP's `OAuthProxy` — authorization requests without a valid `code_challenge` are rejected. This is required by both Claude.ai and ChatGPT.

**Consent is explicit.** Users see the requesting application name, their identity, and must actively select a scope and expiry. No silent token creation. FastMCP's consent page includes CSRF protection with signed cookies.

**Short-lived authorization codes.** Codes are single-use with a 5-minute TTL, stored encrypted. Handled by FastMCP internally.

**Redirect URI validation.** DCR-registered `redirect_uris` are strictly matched on every authorization request — no open redirectors. ChatGPT's dynamic callback URLs are handled via per-session DCR registration.

**DCR client cleanup.** ChatGPT creates a new client per session. Run periodic cleanup (e.g., delete clients not used in 30 days) to prevent unbounded growth. Add to the existing hourly `cleanup_expired_mcp_tokens` task.

**Token never stored in plaintext.** OAuth-created tokens use the same `create_mcp_token` path — plaintext appears only in the token exchange response, database stores SHA-256 hash only.

**Upstream token encryption.** FastMCP encrypts Keycloak tokens at rest using Fernet (AES-128-CBC + HMAC-SHA256). These are short-lived — only needed during the authorization flow.

**HTTPS everywhere.** All OAuth endpoints require HTTPS in production. Only `localhost` redirect URIs may use HTTP (for development).

**Audience binding.** Tokens are scoped to the MCP server via the `resource` parameter (RFC 8707). Keycloak's audience mapper ensures tokens are bound correctly.

**IP allowlisting (optional).** Both Claude.ai and ChatGPT publish IP ranges. Can be enforced at the ingress level for additional security.

## Known Gotchas

**Keycloak CORS.** Keycloak's metadata endpoints have CORS restrictions that may affect browser-based clients. The OAuthProxy handles this by proxying to Keycloak server-side rather than having the browser talk to Keycloak directly.

**Keycloak RFC 8707.** Keycloak doesn't fully respect the `resource` parameter from RFC 8707. Audience binding should use Keycloak's own audience mapper instead of relying on the resource parameter.

**ChatGPT client proliferation.** Plan for thousands of DCR clients over time. The cleanup policy is essential.

**CVE-2025-69196 (token reuse).** Fixed in FastMCP 2.14.2 — OAuth Proxy didn't properly respect the `resource` parameter, allowing token reuse across MCP servers. Our minimum version (3.1.0) includes the fix.

**Spec is a moving target.** Three spec versions in 9 months (March, June, November 2025). FastMCP tracks the spec, so keeping the dependency updated is important.

**Claude callback domain.** May migrate from `claude.ai` to `claude.com` — allowlist both in DCR redirect_uri validation (FastMCP handles this per-registration, so no issue).

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `orchestrator/mcp/requirements.txt` | Modify | Bump `fastmcp>=3.1.0` (for `MultiAuth`) |
| `orchestrator/mcp/oauth_bridge.py` | Create | `SRWOAuthProxy` subclass (token issuance + consent override) |
| `orchestrator/mcp/templates/consent.html` | Create | Consent page with scope/expiry selectors |
| `orchestrator/mcp/server.py` | Modify | Import `SRWOAuthProxy`, wire up `MultiAuth` |
| `orchestrator/mcp/auth.py` | No change | `McpTokenVerifier` unchanged |
| `orchestrator/database/schema.sql` | Modify | Add `origin` column to `mcp_tokens` |
| `orchestrator/database/postgres.py` | Modify | Support `origin` in token creation/listing |
| `deployment/18-keycloak.yaml` | Modify | Add `mcp-server` client to realm |
| `deployment/23-mcp.yaml` | Modify | Add OAuth env vars from secrets |
| `cockpit/.../settings.component.ts` | Minor | Show token origin in table |

## Out of Scope

- **Replacing the existing token system** — this is additive, not a migration
- **Per-tool scope enforcement** — separate feature; works with either token origin
- **CIMD support** — future spec addition, not yet adopted by Claude.ai or ChatGPT
- **Multiple upstream IdPs** — single Keycloak realm for now; composable later via additional `OAuthProxy` instances

## Priority

Medium-high. This is the only blocker for users connecting via Claude.ai or ChatGPT web UIs. Claude Code CLI and OpenWebUI already work with the current bearer token approach.
