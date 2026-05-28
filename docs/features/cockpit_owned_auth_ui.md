---
tags:
  - feature
  - auth
  - cockpit
  - keycloak
  - ux
  - saas
aliases:
  - cockpit auth UI
  - own login page
  - cockpit-owned auth
  - social login
  - magic link sign-in
  - passwordless
related:
  - "[[auth_bff_and_api_tokens]]"
  - "[[multi_tenancy]]"
  - "[[admin_view_as_user]]"
  - "[[keycloak_self_registration_broken]]"
---

# Cockpit-owned auth UI (login, register, social, magic-link)

> Replace Keycloak's themed login/register pages with cockpit-native forms. Keep Keycloak as the identity backend (users, password hashes, sessions, social IdP brokering) but never show its UI to end users. Adds three auth methods: username/email + password (default), social providers (Google/GitHub/Microsoft/etc.), and optional magic-link email-only sign-in. Email-only becomes the default register option with "use password instead" as a toggle.

**Status:** Design only — no code yet. Captured 2026-05-28 during M1.B planning so we can come back later without re-litigating decisions.
**Triggered by:** M1.B planning surfaced that the quick Keycloak self-reg fix (~2h) is throwaway UI work; the cockpit-owned version is ~1–1.5d and is the real v1. User also wants social login (Google/GitHub) and email-only magic-link as part of the same effort. Today's Keycloak-themed pages look unprofessional and feel disjoint from the rest of the cockpit.
**Scope:** Login form, register form, social-provider buttons, magic-link request + consume flow, email verification, password reset. **Does not** replace Keycloak as the identity store, does not implement MFA enrollment in our UI, does not touch the admin user-management page (already cockpit-native).

## TL;DR

| Layer | Change |
|---|---|
| **Architecture** | Hybrid model. Cockpit owns the visible UI. Keycloak stays as the identity backend (user store, password hashing, token issuance, social IdP brokering). Keycloak's themed pages are never shown — every flow runs through cockpit forms that hit BFF endpoints, which call Keycloak's REST APIs (Admin API for create, Direct Access Grants for password login, identity broker for social). |
| **Backend (orchestrator BFF)** | New endpoints under `/auth/*`: `POST /auth/login` (password), `POST /auth/register` (Admin API create-user), `POST /auth/magic-link/request`, `GET /auth/magic-link/consume`, `POST /auth/password-reset/request`, `POST /auth/password-reset/consume`, `POST /auth/verify-email`. Extends the existing cookie-issuing layer from `auth_bff_and_api_tokens`. |
| **Frontend (cockpit)** | New routes `/auth/login`, `/auth/register`, `/auth/magic-link`, `/auth/verify-email`, `/auth/reset-password`. Unauthenticated landing page. Email-only signup by default, with "use password instead" toggle. Social-provider button row. Password rules + complexity hints. Standalone Angular components, design-system styled. |
| **Keycloak config** | New confidential `cockpit-resource-owner` client with `directAccessGrantsEnabled=true` (locked to BFF service account, IP-restricted via Keycloak client authentication). Existing `cockpit-bff` retains `directAccessGrantsEnabled=false` (PKCE-only). Identity-broker providers (Google/GitHub) configured per-environment. `default-roles-srw` carries the `user` realm role (fixes the [[keycloak_self_registration_broken]] gap). |
| **Security responsibilities** | We take ownership of: login rate limiting, register CAPTCHA, email verification flow, password reset flow, magic-link single-use enforcement, IP rate limiting on magic-link request, account lockout after N failed attempts. Keycloak's chain (VERIFY_EMAIL, MFA enrollment, required-actions) is bypassed; v1 defers MFA. |

**Estimated effort:** ~5-9 days total across three independently-shippable slices. Slice 1 (password login + register, ~1.5d) is the foundation; Slice 2 (social, ~½d per provider) plugs into the same login page; Slice 3 (magic-link, ~3-5d) is its own thing and can come last.

## Why now (vs. just fixing the Keycloak self-reg page)

The M1.B #1 quick fix in [[multi_tenancy]] was framed as "wire SMTP + add `user` role to `default-roles-srw`". Both are still needed regardless of which UI we use — but the time we'd spend re-theming Keycloak's FreeMarker register page is throwaway work once we have a cockpit-native page. The cheapest "good" v1 is to skip the Keycloak-themed page and build the cockpit version directly.

**What still gets done in M1.B #1 (the cheap fix), even if we choose this design:**
- Add the `user` realm role to `default-roles-srw` (one CLI command in the bootstrap script + a values toggle).
- Wire SMTP in `values-local.yaml` and the dev cluster (env var + secret).
- These unblock _any_ register path — Keycloak-themed, cockpit-owned, magic-link, social. Independent of the larger UI decision.

**What changes if we commit to cockpit-owned UI:**
- Don't bother editing Keycloak's `register.ftl` template (skip the ~2h theming).
- The bootstrap configmap stops needing `internationalizationEnabled` tweaks specifically for the login pages.
- We do need to add a new `cockpit-resource-owner` client to the realm import JSON.

So the path is: **do the SMTP + default-role fix as M1.B #1 (2h), then come back here and do Slice 1 (1.5d) when ready for a real user signup flow**.

## Why cockpit-owned UI (vs. Keycloak themes)

Options evaluated:

1. **Status quo — Keycloak's themed pages.** Pro: free, battle-tested, MFA + required-actions chain works out of the box. Con: looks like Keycloak (we'd have to invest in theming anyway for production), UX disjoint from cockpit (different fonts, layout, button styles), redirect dance is visible to the user, hard to add custom fields (e.g., "How did you hear about us?").
2. **Keycloak theme heavily customized.** Pro: keeps Keycloak's flow engine. Con: FreeMarker templates are painful, theming locks you to KC version, still get the redirect dance, still hard to add custom fields. ~3-5d to make it look acceptable and we'd still want to redo it later.
3. **Cockpit-owned UI, Keycloak as identity backend** (recommended). Pro: full UX control, integrated with design system, easy to add fields, fast iteration, no redirect dance for password login, foundation for magic-link + social. Con: we take ownership of rate limiting / CAPTCHA / lockout / email flows; bypasses Keycloak's required-actions chain (no built-in MFA enrollment prompt — fine for v1, defer to v1.5).
4. **Replace Keycloak entirely** (Auth0/Clerk model). Pro: total control. Con: massive scope, redoes everything currently working — token issuance, group claims, project membership sync, MCP OIDC bridge, OpenCloud SSO. Not happening.

**Pick: option 3.** The reason is that we _already_ run a cockpit-owned auth surface for PATs (`/settings/api-keys` shipped in [[auth_bff_and_api_tokens]] PR 3) — that page is design-system-styled, i18n, has good UX. The login + register pages should match that quality. Keycloak's themed pages don't.

## Architecture

```
┌─────────────────┐
│  Cockpit (SPA)  │   /auth/login, /auth/register, /auth/magic-link, /auth/reset-password
└────────┬────────┘
         │  fetch with X-CSRF + cookies
         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Orchestrator BFF (orchestrator/auth/bff.py + ext)              │
│                                                                  │
│  /auth/login        → Direct Access Grants → KC /token          │
│  /auth/register     → KC Admin API → create user                │
│  /auth/social/{p}   → 302 → KC /auth?kc_idp_hint=p              │
│  /auth/magic-link/* → SMTP + auth_magic_links table             │
│  /auth/reset/*      → SMTP + auth_password_resets table         │
│  /auth/verify-email → KC Admin API → set emailVerified          │
│                                                                  │
│  All flows issue srw_session cookie via existing                │
│  _create_session() helper from auth_bff_and_api_tokens PR 1.    │
└────────┬────────────────────────────────────────────────────────┘
         │
         ├────────────────► Keycloak Admin API (manage-users role)
         ├────────────────► Keycloak Token endpoint (Direct Access Grants)
         ├────────────────► Keycloak Identity Broker (kc_idp_hint redirect)
         └────────────────► SMTP relay (magic-link + reset + verify)
```

**Two Keycloak clients in the realm:**

| Client | `directAccessGrants` | `standardFlow` | Used by | Purpose |
|---|---|---|---|---|
| `cockpit` (public) | false | true | Legacy — kept for back-compat / rollback | PKCE redirect flow |
| `cockpit-bff` (confidential) | false | true | BFF redirect callback flow today | Code exchange for cookie issuance |
| `cockpit-resource-owner` (confidential, **new**) | true | false | BFF password flow | Direct Access Grants for cockpit-owned login form |

The new client is **service-account locked**: only the BFF can call its token endpoint (mutual TLS or shared secret in Vault), no end-user redirect possible. This is the protection against the Direct Access Grants "less secure" criticism — the password isn't ever sent from a browser to Keycloak, it's sent through the BFF on the same origin as the cockpit.

## Slice 1 — Password login + register (~1.5 days)

**Goal:** A user can land on `/auth/register`, fill an email/username/password form, and end up authenticated with a `srw_session` cookie. A returning user can land on `/auth/login`, fill the form, and same outcome.

### Backend changes

**1. New Keycloak client config** (`helm/templates/keycloak/bootstrap-configmap.yaml` realm import JSON):

```json
{
  "clientId": "cockpit-resource-owner",
  "name": "Cockpit Resource Owner (Direct Access Grants)",
  "enabled": true,
  "publicClient": false,
  "secret": "${COCKPIT_RESOURCE_OWNER_CLIENT_SECRET}",
  "standardFlowEnabled": false,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": true,
  "serviceAccountsEnabled": false,
  "redirectUris": [],
  "webOrigins": []
}
```

Secret synced via Vault/ESO under `srw-secrets` (same pattern as `KC_CLIENT_SECRET`).

**2. Extend `KeycloakGroupSync` → rename `KeycloakAdminAPI`** in `orchestrator/services/keycloak_admin.py`. Add methods:

```python
async def create_user(
    self,
    email: str,
    username: str | None = None,
    password: str | None = None,
    email_verified: bool = False,
    attributes: dict | None = None,
) -> dict:
    """POST /admin/realms/srw/users; returns created user dict."""

async def set_email_verified(self, user_id: str, verified: bool = True) -> None:
    """PUT /admin/realms/srw/users/{id}; sets emailVerified."""

async def trigger_password_reset_email(self, user_id: str) -> None:
    """PUT /admin/realms/srw/users/{id}/execute-actions-email?lifespan=86400
    with body ['UPDATE_PASSWORD']. Used if we want to delegate password reset
    to Keycloak instead of building our own reset flow (deferred design choice)."""

async def user_exists(self, email: str) -> bool:
    """GET /admin/realms/srw/users?email={email}&exact=true; returns true if any match."""
```

**3. New BFF endpoints** in `orchestrator/auth/bff.py`:

```python
@router.post("/auth/login")
async def login(body: LoginRequest, response: Response):
    """
    POST { email_or_username, password } →
    1. Call KC /token with grant_type=password, client_id=cockpit-resource-owner.
    2. On 200: issue srw_session cookie via _create_session().
    3. On 401: return generic { error: "invalid_credentials" } — never distinguish
       "no such user" from "wrong password" (prevents user enumeration).
    4. On 403: account locked / disabled → distinct error.
    """

@router.post("/auth/register")
async def register(body: RegisterRequest, response: Response):
    """
    POST { email, username, password, accept_terms } →
    1. CAPTCHA verify (hCaptcha or Turnstile — config knob).
    2. Rate-limit check (per-IP, per-email-domain).
    3. user_exists(email) → 200 with success message anyway (anti-enumeration).
    4. KC Admin API create_user(email_verified=false).
    5. Issue email verification token, store in auth_email_verifications table,
       send SMTP message with /auth/verify-email?token=... link.
    6. Do NOT issue srw_session cookie — user must verify email first.
       Return 200 { check_email: true }.
    """

@router.post("/auth/verify-email")
async def verify_email(body: VerifyEmailRequest, response: Response):
    """
    POST { token } →
    1. Look up token in auth_email_verifications, check unused + not expired.
    2. KC Admin API set_email_verified(user_id, true).
    3. Mark token consumed.
    4. Issue srw_session cookie (user is now authenticated).
    """
```

**4. New tables** (migration `0012_auth_self_service.sql`):

```sql
CREATE TABLE auth_email_verifications (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL,
  email        TEXT NOT NULL,                  -- snapshot, anti-tamper
  expires_at   TIMESTAMPTZ NOT NULL,
  consumed_at  TIMESTAMPTZ,
  created_ip   INET,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_auth_email_verifications_token_hash
  ON auth_email_verifications(token_hash)
  WHERE consumed_at IS NULL;

-- auth_magic_links + auth_password_resets follow the same shape;
-- defined in slice 3 + (a future slice) so they don't ship with slice 1.
```

**5. Rate limiting + lockout.** New per-route limits enforced in middleware (e.g., `slowapi` or hand-rolled with Redis/Postgres):

| Endpoint | Limit | Lockout |
|---|---|---|
| `POST /auth/login` | 5 attempts / 15 min / IP+email tuple | 6th fails → 15min lockout per tuple |
| `POST /auth/register` | 3 attempts / 1h / IP | + CAPTCHA always |
| `POST /auth/verify-email` | 10 attempts / 15 min / IP | no lockout (legit user retrying link) |

Lockout state can live in `auth_rate_limits` table (or Redis if we ever add one). Postgres is fine for v1 at our scale.

### Frontend changes

**1. New routes** in `app.routes.ts`:

```ts
{ path: 'auth/login',         loadComponent: () => import('./auth/login.component').then(c => c.LoginComponent) },
{ path: 'auth/register',      loadComponent: () => import('./auth/register.component').then(c => c.RegisterComponent) },
{ path: 'auth/verify-email',  loadComponent: () => import('./auth/verify-email.component').then(c => c.VerifyEmailComponent) },
```

Routes are public (no `authGuard`). `authGuard` redirects unauthenticated traffic to `/auth/login?return_to=...` instead of the current Keycloak login URL.

**2. Login page** (`cockpit/src/app/auth/login.component.ts`):

```
┌─────────────────────────────────┐
│         SRW                     │
│                                 │
│   [ Email or username ]         │
│   [ Password         ] [👁]     │
│                                 │
│   [    Sign in    ]             │
│                                 │
│   ──────── or ────────          │
│                                 │
│   [G] Continue with Google      │
│   [⌥] Continue with GitHub      │
│                                 │
│   Don't have an account? Sign up│
│   Forgot password?              │
└─────────────────────────────────┘
```

Signal-based, no form library needed. Submits to `/auth/login`. On 401 shows generic error. On 200 the cookie is set; SPA calls `/auth/me` and redirects to `return_to`.

**3. Register page** (`cockpit/src/app/auth/register.component.ts`):

Two modes via toggle near the top:

```
[ Email-only sign-up  |  Use a password instead ]
```

**Email-only mode** (default):
```
   [ Email                       ]
   [ ☐ I accept the Terms        ]
   [ Send me a sign-in link      ]
```
Hits `/auth/magic-link/request` (deferred to slice 3; for now this mode is hidden behind a feature flag).

**Password mode**:
```
   [ Email                       ]
   [ Username (optional)         ]
   [ Password         ] [👁]     │
   │ Strength: ●●●○○             │
   │ • At least 12 characters    │
   │ • Mixed case + numbers      │
   [ ☐ I accept the Terms        ]
   [    Create account    ]
```

Submits to `/auth/register`. On success shows "Check your email — we sent a verification link to ${email}".

**4. Verify-email page** receives `?token=...` from email link, POSTs to `/auth/verify-email`, on 200 redirects to `/builder` (the post-login landing page).

**5. SessionService changes** — `session.login()` (which currently redirects to KC) becomes `router.navigate(['/auth/login'], { queryParams: { return_to: location.pathname } })`. The Keycloak redirect path stays available as a fallback for org-realm logins (future M2 work) but isn't the default anymore.

### Acceptance probes (slice 1)

1. Fresh visit to `/sessions` → 401 from `/auth/me` → redirect to `/auth/login` (no longer KC).
2. Register flow: submit form → 200 `check_email: true` → user row created in `users` table + Keycloak realm, `email_verified=false`, no cookie set.
3. SMTP relay receives the email → link contains `/auth/verify-email?token=...`.
4. Click link → cockpit verify page → POST `/auth/verify-email` 200 → cookie set → redirected to `/builder`.
5. Login flow: submit form with that user's credentials → 200 → cookie set → `/sessions` renders.
6. Wrong password 5x → 6th attempt 429 lockout → 15 min later allowed again.
7. Register with already-existing email → 200 same as success (anti-enumeration) → no duplicate user created → no email sent.
8. Logout: POST `/auth/logout` → 200 → `/auth/me` 401 → next nav goes to `/auth/login`.

## Slice 2 — Social providers (~½ day per provider)

**Goal:** "Continue with Google / GitHub / Microsoft / Apple" buttons on the login page. Each maps to a Keycloak identity-broker configured provider.

### Backend changes

**1. New endpoint** `/auth/social/{provider}`:

```python
@router.get("/auth/social/{provider}")
async def social_login(provider: str, return_to: str | None = None):
    """
    Redirects to KC standard flow with kc_idp_hint=<provider>.
    KC handles the OAuth dance, provisions the user from identity claims,
    redirects back to /auth/callback (existing endpoint).
    From there the BFF code-exchange flow takes over — same as a password flow's
    cookie issuance step.
    """
    state = _generate_state()
    await _store_pre_auth_state(state, return_to, provider_hint=provider)
    kc_url = f"{KC_BASE}/realms/srw/protocol/openid-connect/auth?" + urlencode({
        "client_id": "cockpit-bff",
        "redirect_uri": f"{BFF_BASE}/auth/callback",
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
        "kc_idp_hint": provider,
    })
    return RedirectResponse(kc_url)
```

The user briefly sees Keycloak's "redirecting to Google…" page (or no page at all if KC's `kc_idp_hint` skips its own login form, which it does when configured correctly). Then Google's standard login page. Then back to our cockpit, authenticated.

**2. Realm config** — for each provider, add to the realm import JSON:

```json
{
  "identityProviders": [
    {
      "alias": "google",
      "providerId": "google",
      "enabled": true,
      "firstBrokerLoginFlowAlias": "first broker login",
      "config": {
        "clientId": "${GOOGLE_OAUTH_CLIENT_ID}",
        "clientSecret": "${GOOGLE_OAUTH_CLIENT_SECRET}",
        "defaultScope": "openid email profile",
        "syncMode": "IMPORT"
      }
    },
    {
      "alias": "github",
      "providerId": "github",
      ...
    }
  ]
}
```

Provider secrets via Vault/ESO. Each provider needs an OAuth app registered (Google Cloud Console, GitHub Developer Settings, etc.).

**3. First-broker-login flow** — Keycloak's default flow prompts the user to "confirm your account" on first social login. For UX, override `firstBrokerLoginFlowAlias` with a custom flow that auto-creates the local Keycloak user from the social identity claims without any cockpit-facing prompts. Keycloak admin console → Authentication → Flows.

### Frontend changes

**1. Add buttons to login page** (also register page):

```ts
const PROVIDERS = [
  { id: 'google',    label: 'Continue with Google',    icon: 'logo-google' },
  { id: 'github',    label: 'Continue with GitHub',    icon: 'logo-github' },
  // microsoft, apple, etc. as enabled
];
```

Click handler: `window.location.href = '/auth/social/' + provider`.

**2. UserService** — On the callback after a social login, `/auth/me` returns the JIT-provisioned user. No special handling needed; same shape as a password login. The only nuance: if Keycloak auto-provisioned the user with `is_approved=false`, the cockpit shows the "pending approval" page until an admin approves (or registration auto-approves — see M1.B #1).

### Tradeoffs

| Aspect | Notes |
|---|---|
| User briefly sees Keycloak | The `kc_idp_hint` flow does a 302 dance through `auth.superhuman-remote-worker.com` to the provider. If correctly configured ("Hide on Login Page" + idp hint), no Keycloak UI renders — user only sees Google's login. If user has cached Google session, the whole hop completes in <2s. |
| Username for social users | Keycloak's default behavior: use the email's local part. Customize via the broker mapper if a different convention is wanted. |
| Account linking | If a user signs up with email/password and later "Sign in with Google" using the same email, KC's `firstBrokerLoginFlowAlias` decides: link by email (recommended), force re-login as the existing user, or create a duplicate. Lock the choice now. |
| Email verification on social | Social-provided emails are typically already verified by the IdP (Google, GitHub). Map their `email_verified` claim → our `email_verified` column to skip our verification step. |

### Acceptance probes (slice 2)

1. Click "Continue with Google" → 302 → Google login → 302 back → `/builder` with cookie.
2. Same flow with GitHub.
3. Sign up with email/password as `alice@gmail.com`. Sign out. Sign in with Google using `alice@gmail.com`. → Account linked, no duplicate user row.
4. Social user has `email_verified=true` on first login.
5. New social user has `is_approved=` whatever the default is (M1.B #1 makes this `true`).

## Slice 3 — Magic-link email-only sign-in (~3-5 days)

**Goal:** A user enters their email, clicks "Send me a sign-in link", receives an email, clicks the link, lands authenticated with no password ever involved.

Becomes the **default register flow** once shipped (with "use password instead" as a toggle on the register page). Sign-in flow keeps password as default; magic-link as a secondary option ("Email me a link instead").

### Backend changes

**1. New table** `auth_magic_links`:

```sql
CREATE TABLE auth_magic_links (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT NOT NULL,
  user_id       UUID REFERENCES users(id) ON DELETE CASCADE,  -- null if signup
  token_hash    TEXT NOT NULL UNIQUE,
  purpose       TEXT NOT NULL CHECK (purpose IN ('signup', 'signin')),
  expires_at    TIMESTAMPTZ NOT NULL,                          -- 15 min
  consumed_at   TIMESTAMPTZ,
  created_ip    INET,
  consumed_ip   INET,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_auth_magic_links_token_hash
  ON auth_magic_links(token_hash)
  WHERE consumed_at IS NULL;
CREATE INDEX idx_auth_magic_links_email_recent
  ON auth_magic_links(email, created_at DESC);  -- for rate limiting
```

**2. New BFF endpoints:**

```python
@router.post("/auth/magic-link/request")
async def request_magic_link(body: MagicLinkRequest):
    """
    POST { email, purpose: 'signin'|'signup' } →
    1. CAPTCHA verify (mandatory).
    2. Rate limit: 3 requests / 10 min / email; 10 / 10 min / IP.
    3. Look up user by email.
       - If signin + no user: return 200 success anyway (anti-enumeration), send NOTHING.
       - If signup + user exists: return 200 success anyway, send the signin link (not signup).
       - If signin + user exists: generate token, store, send signin email.
       - If signup + no user: generate token, store with user_id=null, send signup email.
    4. Email content: "Click this link to sign in. Expires in 15 minutes. If you didn't
       request this, ignore." Plain text + HTML.
    5. Return 200 { check_email: true }.
    """

@router.get("/auth/magic-link/consume")
async def consume_magic_link(token: str, response: Response):
    """
    GET ?token=<urlsafe> →
    1. Hash + look up token.
    2. Check not consumed, not expired.
    3. Mark consumed.
    4. If user_id is null (signup):
       - Call KC Admin API create_user(email_verified=true, no password).
       - Insert users row.
       - Set user_id on the magic link row.
    5. Issue srw_session cookie via _create_session().
    6. Redirect to /builder (or return_to query param).
    """
```

**3. Keycloak side.** Magic-link users have no password in Keycloak. They can't use the password login form. If they ever set a password (via "Set a password" account action — deferred), the Admin API call sets it.

### Frontend changes

**1. Register page mode toggle** — default to magic-link mode:

```
What's your email?
[ alice@example.com           ]

[ ☐ I accept the Terms          ]

[    Send me a sign-in link    ]

Prefer a password? Use email + password instead →
```

After submit:

```
Check your email
We sent a sign-in link to alice@example.com.
The link expires in 15 minutes.

Didn't get it? Check spam, or [resend in 60s].
```

**2. Login page** — secondary "Email me a link instead" link below the password form.

**3. Magic-link consume route** — `/auth/magic-link?token=...`. Component fires `GET /auth/magic-link/consume` on init; on 200 redirects to the post-login landing. On 4xx shows "This link expired or was already used" with a button to request a new one.

### The hard parts

| Concern | Mitigation |
|---|---|
| Email-bombing (attacker triggers thousands of register emails to victims) | Mandatory CAPTCHA on request, per-email rate limit, per-IP rate limit, exponential backoff after N requests/hour to the same email. |
| Phishing risk (user clicks a link in a sketchy email thinking it's from us) | Email design clearly branded, "If you didn't request this, ignore" prominently. Link domain is our own. Standard caveats apply — same risk as password resets. |
| SMTP deliverability | Use a transactional email service (Postmark, SendGrid, AWS SES) not raw SMTP. SPF + DKIM + DMARC properly configured. This is its own ~half-day setup task that benefits Keycloak emails too. |
| Token security in URLs | 32-byte urlsafe token in query param. URL leakage (browser history, Referer header, proxy logs) is mitigated by 15-min expiry + single-use enforcement. Don't put the token in fragments (lost across redirects). |
| User opens link on different device than they requested | Acceptable. Token is short-lived and single-use. Common UX. Some apps refuse it as a phishing defense; we don't — too friction-y. |
| Magic-link login bypasses our password lockout | True. Magic-link is its own rate-limit surface; lockout is per-email regardless of method. |

### Acceptance probes (slice 3)

1. Request signup link for new email → email received within 30s → click link → user row created → cookie set → `/builder` renders.
2. Request signin link for existing email → email received → click → cookie set → no new user row created.
3. Request signup link for **existing** email → email received (signin variant) → click → signed in as existing user, not a new account.
4. Request signin link for **non-existent** email → 200 same as success, no email sent.
5. Click expired link → "Link expired" page → request new link.
6. Click consumed link (same link twice) → "Link already used" page.
7. Request 4th link within 10 min → 429 rate limit on the same email.
8. Request 11th link within 10 min from same IP (different emails) → 429 rate limit on IP.

## Security responsibilities we take on

| Responsibility | Owner today (Keycloak) | Owner after this | v1 plan |
|---|---|---|---|
| Login rate limiting | KC brute-force-protection | Us | Per-IP+email tuple, 5/15min, lockout |
| Register rate limiting | KC brute-force-protection | Us | Per-IP, 3/h, plus CAPTCHA always |
| Register CAPTCHA | KC reCAPTCHA plugin | Us | Cloudflare Turnstile (no Google dependency) |
| Account lockout | KC | Us | 15min per IP+email after 5 fails |
| Email verification | KC required-action `VERIFY_EMAIL` | Us | `auth_email_verifications` table + SMTP |
| Password reset | KC `update-password` action | Us OR KC | Decision below |
| MFA enrollment | KC `CONFIGURE_TOTP` etc. | **Deferred to v1.5** | No MFA in v1; document risk |
| Password complexity | KC password policy | Us | Min 12 chars; "leaked password" check via HaveIBeenPwned API (optional) |
| Password hashing | KC argon2 | KC (still) | We never see the password hash; only forward plaintext to KC over HTTPS |
| Session management | KC SSO sessions | Us (`srw_sessions` table from PR 1) | Existing implementation, unchanged |
| Account linking | KC `firstBrokerLoginFlowAlias` | KC | Stays in KC realm config |

### Password reset: build our own vs. delegate to Keycloak

Two options:

**A. Delegate to Keycloak** (~½d): user clicks "Forgot password" → BFF calls `Admin API: execute-actions-email(UPDATE_PASSWORD)` → Keycloak emails its own themed reset page → user resets → comes back to our login. **Pro:** zero new tables, zero password-handling code on our side. **Con:** user sees Keycloak's themed page during reset (jarring after using our login page).

**B. Build our own** (~1d): mirror the magic-link table for reset tokens, our own reset page. **Pro:** full UX continuity. **Con:** ~½d more work, we own a password-handling code path.

**Lean toward B for v1**, because we'll have built the magic-link infrastructure for slice 3 and the reset flow is structurally identical (one table column difference: `purpose='password_reset'`). If we ship slice 1 first without slice 3, option A is the bridge.

## Tradeoffs to know going in

### Direct Access Grants is sometimes labeled "deprecated"

The OAuth 2.0 Security Best Current Practice (RFC 9700, formerly the BCP draft) discourages Resource Owner Password Credentials grant for public clients because the password leaks to the client. **For our use case it's fine** because:

1. The "client" is the BFF, not the browser. The browser → BFF call is same-origin HTTPS; the BFF → Keycloak call is internal cluster HTTPS. The password never leaves our trust boundary on its way to Keycloak.
2. The BFF is a confidential client (`cockpit-resource-owner`) with a secret in Vault. Only the BFF can exercise this grant.
3. We're not using third-party trust — we own both the cockpit and Keycloak.

The criticism applies to mobile apps embedding ROPC and to legacy SPAs collecting passwords directly to call KC's token endpoint. Neither is what we're doing.

### MFA gap

Keycloak's required-actions chain handles MFA enrollment (TOTP, WebAuthn) without our code knowing about it. With our own login form, MFA enrollment must be built explicitly. v1 ships without MFA. **Document this as a known limitation**. v1.5 adds a "Security" page in cockpit settings where users enroll TOTP via the Admin API (`Admin API: execute-actions-email(CONFIGURE_TOTP)` or a custom enrollment endpoint that bypasses the email and uses KC's `/users/{id}/totp` directly).

### Required-actions chain bypass

Same idea, more general: Keycloak has a chain of "required actions" that fire on login (e.g., "you must change your password", "you must verify your email", "you must accept new ToS"). With our own login form, we bypass the chain. v1 implementations:

- VERIFY_EMAIL → handled by our verify-email flow.
- UPDATE_PASSWORD → handled by our reset flow.
- CONFIGURE_TOTP → deferred to v1.5.
- TERMS_AND_CONDITIONS → we own the cockpit, we can render a ToS modal on login if needed.

This isn't load-bearing for v1, but document it so future-us doesn't expect KC's required-actions to fire.

### Anti-enumeration

User-facing flows must never reveal whether an email is registered. Implementations:

- Login error is always "Invalid credentials" — never "no such user".
- Register success is always "Check your email" — even if the email is already taken (and we send a "you tried to sign up but you already have an account" email instead).
- Magic-link request always returns 200 + "Check your email" — even when no link was sent.
- Password reset request same.

The cost is that legitimate users who typo their email don't get an error — they just don't get an email. Acceptable tradeoff.

### CAPTCHA dependency

We need a CAPTCHA service for register + magic-link request. Options:

- **Cloudflare Turnstile** — invisible, no Google dependency, free up to high volumes. Recommended.
- **hCaptcha** — accessible alternative, privacy-friendly. Also free at low volumes.
- **reCAPTCHA v3** — works but is Google-dependent.

Pick Turnstile. Config: site key in `env.js`, secret in `srw-secrets`, verify-token endpoint in BFF.

## Phased rollout

| Slice | Effort | Status | Depends on |
|---|---|---|---|
| **Pre-req (M1.B #1)** — SMTP wiring + `user` role on `default-roles-srw` | ~2h | Open | — |
| **Slice 1** — Password login + register UI + verify email + lockout | ~1.5d | Designed | Pre-req |
| **Slice 2a** — Google identity broker | ~½d | Designed | Slice 1 |
| **Slice 2b** — GitHub identity broker | ~½d | Designed | Slice 1 |
| **Slice 2c** — Microsoft / Apple / etc. | ~½d each | Designed | Slice 1 |
| **Slice 3** — Magic-link email-only sign-in | ~3-5d | Designed | Slice 1 + CAPTCHA + SMTP |
| **v1.5** — MFA enrollment (TOTP) | ~2-3d | Out of scope | Slice 1 shipped + at least one prod user |
| **v1.5** — Password reset via our own flow (if delegated to KC in v1) | ~½d | Conditional | Slice 1 shipped |

Slice 1 is the keystone. Everything else plugs into a working login page.

## Decisions to lock when we come back

1. **Default register mode**: email-only (magic-link) or password? — Lean magic-link as default once slice 3 ships; password until then.
2. **Password reset**: delegate to KC (Option A) or build our own (Option B)? — Lean B if slice 3 is shipping anyway.
3. **CAPTCHA provider**: Turnstile recommended.
4. **SMTP provider**: Postmark, SendGrid, AWS SES, or self-hosted Postfix relay? — Likely Postmark or SES for deliverability; cost-compare at decision time.
5. **Account linking on social**: link by email (recommended), force re-login, or create duplicate? — Recommend "link by email".
6. **Password policy**: min length, complexity rules? — Lean 12 chars min, no complexity requirements (NIST 800-63B style), HIBP check optional.
7. **Username vs email login**: support both, or email only? — Lean both (Keycloak supports both natively).
8. **ToS acceptance UI**: checkbox at register, modal on first login, or none for v1? — Lean checkbox at register; modal on policy changes.

## Out of scope

- MFA enrollment (deferred to v1.5).
- Federated identity beyond standard OAuth IdPs (e.g., SAML for enterprise customers — M2 territory).
- Per-tenant Keycloak realms (M2.C in [[multi_tenancy]]).
- Account self-deletion + data export (M1.E in [[multi_tenancy]]).
- Bulk user-role assignment in the admin UI (separate small UX task surfaced during M1.B planning; ~1h fix in `/admin/users`).
- Org-level invite flows ("invite your team" UI — M2 territory).
- Mobile-shell parity for these pages (`simple/` shell doesn't have an auth surface yet).
- Replacing Keycloak as the identity store (explicit non-goal).
- Replacing the existing PAT system at `/settings/api-keys` (already shipped).

## References

- [[auth_bff_and_api_tokens]] — the cookie BFF foundation this builds on (`srw_session`, `_create_session`, `cockpit-bff` confidential client).
- [[keycloak_self_registration_broken]] — the M1.B #1 prerequisites (SMTP + `default-roles-srw` role wiring).
- [[multi_tenancy]] — parent doc; this feature lives in M1.B and is a v1 unlock for public signup.
- [[admin_view_as_user]] — design-doc style reference for this format.
- Keycloak Admin REST API: https://www.keycloak.org/docs-api/latest/rest-api/index.html
- OAuth 2.0 Security BCP (RFC 9700): https://www.rfc-editor.org/rfc/rfc9700
- Cloudflare Turnstile docs: https://developers.cloudflare.com/turnstile/
