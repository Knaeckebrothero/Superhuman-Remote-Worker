# Organizations Layer (M2) — Research Foundation

**Date:** 2026-07-12
**Status:** Research input for the M2 design discussion. No decisions are made here; the
existing M2 sketch lives in [`multi_tenancy.md`](../multi_tenancy.md) §Milestone 2 and the
fork framing in [`saas_roadmap.md`](../saas_roadmap.md).
**Provenance:** Three codebase survey agents (Keycloak surface, admin surface, data model)
plus a web-research harness (5 search angles → 26 sources → 128 extracted claims → 3-vote
adversarial verification of the top 25 → synthesis). Final verification state: **24 claims
confirmed (marked ✓, most 3-0 against live primary sources), 1 refuted, the rest are direct
extractions from cited sources that never entered the verification set** (notably: all AWS
trust-center, Bitkom/GDPR, Postgres-RLS, billing-pattern, and realm-count-scaling claims —
treat those as sourced-but-unverified). The one refuted claim (0-3) was an over-read of
keycloak#43635 — "the `organization` claim disappears once a single-org user joins a second
org (bare scope)" — **do not repeat it**; the narrow, confirmed version is the
global-IdP-broker gap in B1.

---

## Part A — What the codebase says (internal survey)

### A1. Keycloak integration surface

Every Keycloak touchpoint derives from two single-valued env vars (`KEYCLOAK_REALM`,
`KEYCLOAK_ISSUER_URL`/`KEYCLOAK_URL`) consumed by ~6 singleton clients. There is no realm
resolver and no per-request realm selection anywhere.

- **Role interpretation:** `auth.py:263-269` — `is_admin = "admin" in realm_access.roles`;
  approval = `db_approved OR role_approved`. `users.is_admin` is a login-refreshed cache of
  the realm role, not a source of truth.
- **Token validation:** single issuer, single cached JWKS client (`oidc.py:23-47,143`),
  **`verify_aud: False`** — audience deliberately unverified, so any org-in-token design is
  claim-trust, not audience-enforced.
- **BFF:** cookie session (`srw_sessions`) holding KC tokens; one confidential client;
  endpoints derived from the single realm (`kc_client.py:38-66`, `bff.py:157-318`).
- **Admin API:** group sync creates `project-{project_id}` groups (`keycloak_admin.py:89-91`);
  token-exchange impersonation for per-user cloud access (`src/services/keycloak_token.py`,
  RFC 8693 `requested_subject`; realm `impersonation` role + `--features=token-exchange`).
- **Cockpit talks only to the BFF** — no realm/issuer/clientId in `env.js` anymore.
- **Helm bootstrap** (`helm/templates/keycloak/bootstrap-configmap.yaml`): idempotent
  kcadm get-or-create for ONE realm + clients (cockpit SPA, cockpit-bff, srw-mcp,
  opencloud-orchestrator) + cloud SA + groups. Bundled-KC path mirrors this via realm-export
  ConfigMap + ~340-line postStart hook, all single-realm.
- **Downstream OIDC consumers on the same realm:** Gitea (initContainer + orchestrator
  `ensure_oidc_configured`), OpenCloud (`OC_OIDC_ISSUER` + `PROXY_USER_OIDC_CLAIM=sub` with
  "changing orphans all Spaces" warning + hostAliases hack), Nextcloud. Each binds one static
  discovery URL.
- **Latent findings:** `KEYCLOAK_ADMIN_API_DISABLED` is set by Helm but read by zero Python
  code (group sync is disabled only by omitting admin creds). OpenCloud identity linkage keys
  on `{issuer, sub}` — under realm-per-org, `sub` collides across realms and downstream
  identity (OpenCloud, Gitea `login_name`) would need re-keying to `(issuer, sub)`.

**Blast radius per option:**
| Option | Keycloak surface touched |
|---|---|
| (a) App-side orgs in Postgres | **None.** DB migrations + `access.py` + cockpit switcher only. |
| (b) KC 26 native Organizations | Medium: claim-reading in `auth.py`, Organizations admin-API calls, realm-export/bootstrap additions. Issuer/JWKS/BFF/token-exchange/cockpit unchanged. |
| (c) Realm-per-org | Rewrites `oidc.py` (per-realm issuer+JWKS), `kc_client.py`+`bff.py` (org→realm resolution before login), bootstrap (realm loop), every downstream service's OIDC wiring (likely one service stack per org), MCP OAuth bridge; `sub`-collision re-keying. |

### A2. Admin surface inventory

- **75 `_require_admin` call sites** (all in `orchestrator/main.py`; definition
  `main.py:24598` checks `real_is_admin`), ~18 embedded `is_admin` branches, **12 visibility
  helpers in `security/access.py` with admin bypass**, 5 cockpit admin routes behind one
  `adminGuard`.
- **PLATFORM (inherently cross-tenant):** agent fleet (list/deregister/system-info, pod IPs),
  VM fleet, model catalog + provider keys/endpoints/defaults (`/api/admin/providers/*`),
  system settings (main-cloud router, VM kill-switch, network_tier on projects), config
  overrides, codex proxy, snapshots stats, sudo auto-rules (global), security events, raw
  table access.
- **ORG (hand to org-admins under M2):** user CRUD + `/api/admin/users` list +
  bulk-approve (app-side admission), capability grants at user/project scope (`'org'` fits the
  existing `scope_kind` enum), per-user usage/cost views, project create-on-behalf/member
  management.
- **CONTENT-READING (break-glass candidates):** the entire jobs family via
  `require_job_access` admin bypass (logs, files, diffs, chat), threads via
  `require_thread_owner`, LLM request payloads, cross-job sources, sudo request command
  strings, **raw tables** (`GET /api/tables/{name}` returns any rows), pgAdmin/Neo4j links,
  and admin-scope MCP-token/PAT minting (`main.py:22697`, `:22858`) which extends god-mode
  into credentials.
- **View-as machinery is the break-glass seam:** `X-Admin-View-As` shadows `is_admin` while
  `real_is_admin` is consumed in exactly 4 places (`_require_admin`, both admin-scope token
  mints, `log_security_event`). `security_events` records `view_as` + `real_is_admin` today
  but has **no org column**; `users` has **no org_id**.

### A3. Data model and query funnel

- **Zero org/tenant columns exist.** Schema delta for M2.A: new `organizations` +
  `organization_members(org_id, user_id, role)`; `projects.organization_id` FK (the anchor —
  everything else inherits via project → job → thread); `security_events.org_id`;
  `'org'` in `capability_grants.scope_kind`; **`organization_id` denormalized onto
  `usage_events` at emit time** (audit DB cannot FK/join the app DB).
- **The funnel is real:** `access.py:295 user_visible_project_ids` → `get_visible_jobs` /
  `_visibility_clause` emit `(user_id = $owner OR project_id = ANY($projects))`; usage ledger
  reapplies the same clause. Audit/graph/vector reads are all preceded by `require_job_access`
  or `require_project_member`.
- **Three structural weaknesses for org isolation:**
  1. Audit/vector/compute tiers store ownership **only in code** — `llm_requests` /
     `chat_history` / `agent_audit` are `job_id`-only; vector `sources` have no owner at all
     (content-hash deduped, shared via `job_sources`). Isolation is a convention (every
     endpoint must call the gate first), not a DB constraint.
  2. `sources` content-hash dedup across jobs = latent **cross-org inference channel** (a doc
     ingested by two orgs is reachable from either). Needs per-org dedup scoping.
  3. The **global `is_admin` bypass** inside every access.py helper directly contradicts
     "isolated from other orgs' admins" — the admin-flag semantics, not the schema, are the
     biggest behavioral change.
- **Personal-resource ambiguity:** user-owned, non-project resources (threads, PATs, BYOK
  keys) have no unambiguous org under "every resource belongs to exactly one org" once users
  can belong to multiple orgs. (The personal-org model resolves most of this.)
- **Metering/quota units today:** `usage_events`/`usage_daily` keyed user+project;
  `usage_rates` global; `workspace_intervals` keyed job/thread; rate-limiting v2 is design-only
  with scopes account/project/user (no org); quota enforcement engine not wired; **no wallet
  schema exists** (billing stub planned per-user wallets — greenfield, so switching the unit
  to org costs nothing).
- **Provisioning:** `upsert_user_from_oidc` (postgres.py:7290) already creates an
  "org-of-one" per user atomically (personal default project + `project_members role='owner'`
  + `default_project_id`) — the personal-org backfill is nearly mechanical.

---

## Part B — External research

### B1. Keycloak 26 Organizations vs realm-per-tenant vs app-side

All ✓ claims below were verified 3-0 against **live upstream keycloak.org docs** (the RHBK
26.6 pages are bot-blocked mirrors of these) and, where version-sensitive, against the doc
source at the exact `release/26.2` tag.

- KC Organizations = **multi-tenancy within a single realm**, positioned as the B2B entry
  point; vendor explicitly frames the capability set as partial ("for now, it provides some of
  the core capabilities"). It is an **identity-layer feature, not a tenant-management layer**
  — app-side org modeling in Postgres is necessary regardless. ✓
- **Per-org enterprise SSO with email-domain routing is native**: IdP linked to an org +
  "Redirect when email domain matches" → identity-first login auto-redirects by email domain,
  auto-membership after first-broker login. Constraints: **one IdP object can serve only one
  org** (open feature requests #31705/#38125; workaround = duplicate the IdP config per org —
  each enterprise customer needs its own IdP object created via the admin REST API); a domain
  cannot be shared by two orgs in a realm. ✓
- **Managed vs unmanaged members**: managed = federated via the org's IdP, lifecycle owned by
  the org — **deleting the org or the membership deletes the account from the realm**;
  unmanaged = realm-owned identity that survives org removal. Self-signup users who later
  join customer orgs must be *unmanaged* members (GitHub-style multi-org compatible), and any
  org-deletion flow must account for managed enterprise-SSO members being destroyed with
  their org. ✓
- **Token claim is scope-driven with a UX trap** (verified at the 26.2 branch + confirmed as
  designed behavior by the Organizations lead maintainer): plain `organization` scope +
  multi-org user → **interactive org-selection prompt at login**; `organization:<alias>` pins
  one; `organization:*` maps all (claim keyed by alias; id/attributes omitted unless the
  mapper options are enabled). A cockpit with its own org switcher should request
  `organization:*` or — better — manage active-org selection app-side. Known open edge-case
  bugs on the multi-org token path: #42836 (org selection lost after token refresh), #35830
  (claim absent for a second client), #41127 — handle claims defensively. ✓
- **Global-IdP broker gap** (the narrow, confirmed version): a multi-org user authenticating
  through a **realm-level IdP** (e.g. Google) never sees the org-selection page and the
  `organization` claim is **silently omitted** even when the scope is sent — a relying app
  cannot distinguish zero-org from multi-org on that path. Maintainer-acknowledged,
  backlogged, auto-closed "not planned" 2026-01-22 (keycloak#43635) — cite as
  acknowledged/unfixed with **no planned fix**. Org-linked IdPs are unaffected. Whether
  `organization:*` bypasses the gap is unknown (open question). ⇒ never treat an absent org
  claim as "no orgs"; resolve membership from our DB. ✓
- **No per-org RBAC, even in 26.6**: org groups cannot take role assignments ("coming in
  future releases") and are actively blocked in authorization policies (26.6 hardening
  prevents org-group IDs in group policies; only realm groups work). Org-scoped roles (owner/
  admin/member/billing) must live app-side; tokens carry membership as *input* to app-side
  RBAC only. ✓
- **VERSION FLAG for our deployed 26.2** (`helm/values.yaml:703` pins
  `quay.io/keycloak/keycloak:26.2`): hierarchical **Organization Groups landed in 26.6.0
  (April 2026)** — zero mentions in 26.2.5/26.3.5/26.4.7 docs; expanded invitation management
  is 26.5+; Fine-Grained Admin Permissions for Organizations is 26.7.0. **Adopting KC
  Organizations implies bumping the chart's Keycloak image to ≥26.6**, or designing without
  org groups. ✓

Unverified — never entered the verification set (keycloak discussion #11074 + keycloakpro
guide); the synthesis explicitly lists realm-count scaling as an open question:
- Stock KC (~17) struggled beyond **100–200 realms**; realm creation grew exponentially
  (abandoned at ~620); a 2025-10 update states **KC 26.4 handles 1000+ realms acceptably only
  with enlarged realm cache**, and the admin console stays slow (~20 s at 1000 realms, ~50 s
  at 3000). Practitioner guidance: realm-per-tenant is practical for ~5–20 tenants;
  hundreds of realms exhaust JVM heap; ~500 realms ≈ millions of config rows.
  (Even if these numbers are off, the codebase blast radius in A1 independently rejects
  realm-per-org for us.)

### B2. Org modeling precedents

Three independent first-party migrations converge on the same lesson — **all three verified
3-0 against live primary sources**:
- **Neon** (Dec 2025 post-mortem, quotes verified verbatim): "every system had to handle
  twice the number of edge cases" from supporting user-owned AND org-owned projects (APIs,
  permissions, usage tracking, billing — plus "who is actually paying for this project?"
  ambiguity); their explicit advice: **"start with team accounts from day one. Thank us
  later."** Retrofit = auto-create one "migrated organization" per user (>10M projects, zero
  downtime); org-less user API keys transparently fall back to the personal org (no breaking
  API change); billing continuity by preserving the billing-account identifier. ✓
  (Caveat: single vendor's post-mortem opinion — costed and against-interest, but the post
  also pitches Neon.)
- **Vercel** (Jan 2024 changelog, verified): automatically converted **every personal account
  into an auto-created free Hobby team** with derived slug `{username}s-projects`; stated
  rationale = tier changes previously required transferring projects between account types
  ("Upgrading and downgrading will now be easier, as they will no longer require transferring
  projects"). Username preserved as global identity. Design lessons: derive the personal-org
  slug from the username but keep it distinct; make plan tier an **org attribute** so
  upgrades never move resources. ✓
- **GitHub** (changelog verified): **retired in-place user→org account transformation
  effective 2026-01-12** after ~12 years; replacement is a selective, non-destructive "Move
  work" flow — migrate content, never transform identity. A "convert my personal org to a
  company org" feature should be resource transfer into a new org, keeping the personal org
  intact. ✓
- **Clerk** (reference B2B auth-as-a-service implementation; verified against live docs): the
  recommended model exactly — global users, multi-org membership, one **active organization
  per session** driving data access and effective role. **Documented sharp edge: the session
  cookie is a browser-global singleton, so every tab shares the most recently active tab's
  org** — "do not rely on the session cookie alone"; carry an explicit org id per request.
  Their per-org onboarding checklist: member invitations, verified email domains
  (auto-invite or admin-approved), per-org SAML/OIDC enterprise connections. ✓
- **OpenRouter** (unverified extraction): global users, multi-org, org switcher; moving
  prepaid credits from personal wallet to org wallet is a **manual support process** — a
  billing edge case to design away (never store value on the user; store it on the org).
- **WorkOS** guidance (unverified extraction): enterprise features (SSO, directory sync,
  audit logs) configured **exclusively at the org level**; duplicate users across orgs
  auto-linked into one global user record via email.

### B3. Operator access / break-glass / German compliance

**GitLab is the verified anchor here** (3-0, quotes verbatim from the live security FAQ);
AWS/Bitkom/SOC-2 items below never entered the verification set and remain extractions. Note
the synthesis caveat: GitLab's pattern is **self-attested vendor policy, not an audited
control** — citable as precedent, not as compliance evidence.

- **GitLab** ✓: (1) staff access to customer content exists and is **need-conditioned**, not
  technically impossible ("team members will not access private repositories unless required
  for support and troubleshooting"); (2) mechanism = sign-in/impersonation with a
  least-privilege scoping commitment ("we will limit the scope of our review to the minimum
  access required"), impersonation start/stop generate **dedicated audit events**; (3)
  copying content out is **consent-gated with delete-on-resolution**; two pre-declared
  consent-free exceptions (suspected ToS violations; legal compulsion) — a break-glass
  carve-out defined in advance in public policy, run under SOC 2 Type 2 + ISO 27001 in the
  GDPR processor role. Template: keep operator access possible but consent-or-need-gated,
  minimum-scope, audit-bracketed, retention-boxed — **and document it in the DPA/AVV**.
- **GitLab consensual-impersonation gap** ✓ (issue #407820 verified): impersonation today
  needs no user consent/notification — GitLab's own auth PM filed this as a privacy issue
  (2023), proposing per-user opt-in; still open/"not planned" as of 2025-08. Consent-gating
  is an unclaimed differentiator.
- **AWS** (Trust Center, operator access — unverified extraction): core services designed with **zero standing
  operator access** (KMS, EC2/Nitro, Lambda, EKS); **Forward Access Sessions** make sensitive
  operator permissions cryptographically contingent on customer authorization (consent-gated
  elevation); every operator action attributed to an individual human, no shared accounts;
  support uses dedicated documented IAM roles **customers can disable**; least-privilege +
  time-boxed + multi-person approval for sensitive ops.
- **GitLab audit-event field detail** (docs.gitlab.com, unverified extraction): each event
  ties action → actor + origin (author id/name, scoped entity, target id/type, IP, UTC
  timestamp); instance-wide operator audit view is separate from the group/project-scoped
  view customers see; operator-level audit visibility is gated to paid tiers.
- **SOC 2 / JIT** (secondary): least privilege by role; auditors want access monitoring +
  accountability evidence; JIT elevation logging request + reason + approval satisfies
  audit-trail expectations; eliminating standing access shrinks insider exposure.
- **Bitkom AVV guidance (Nov 2025, German market):**
  - Incidental, non-systematic staff access during support/maintenance (logs, error messages)
    does **not** constitute Auftragsverarbeitung (Art. 4 Nr. 8) and needs no Art. 28 DPA —
    decisive criterion is **Planmäßigkeit** (whether data access is a planned object of the
    service). Hosting the data is planned processing → DPA required (which SRW hosting is).
  - Compliance evidence for German controllers: audit trails + certifications — named
    standards are **ISO 27001, ISO 27701, BSI C5**; **SOC 2 is not among them** for the
    German market.
  - **Art. 28(10):** a processor using customer data for its own purposes becomes a controller
    with full liability — hard legal line: no unconsented analytics/training on tenant
    content.
  - Sub-processors: the "general authorization" model (14-day advance notice + genuine right
    of objection + current sub-processor list) is the established German standard —
    **LLM providers are sub-processors** and belong on that list.
  - Art. 28(3)(b): documented written confidentiality obligations for every staff member with
    potential access.

### B4. Postgres RLS vs app-layer scoping (⚠ zero claims from this angle reached adversarial
verification in either run — the synthesis explicitly flags Q4 as an open question needing a
dedicated verified pass before schema commitment; sources below genuinely disagree)

- **Pro (Svix, production):** RLS as defense-in-depth on top of app-layer checks, covering
  exactly two residual failure modes — a developer forgetting a tenant filter, and SQL
  injection. Session variable set per transaction (`set_config('current_request.account_id',
  …, true)`); **fails closed** (no context → no rows). Policies that subquery to parent
  tables are expensive → **denormalize tenant IDs onto child tables**.
- **Contra (PlanetScale, vendor):** recommends app-layer scoping at scale; volatile policy
  functions cost 3×+ in the planner; **transaction-mode pooling (PgBouncer) requires
  re-establishing tenant context via SET LOCAL every transaction**; footguns: table-owner
  bypass unless `FORCE ROW LEVEL SECURITY`, superuser always bypasses.
- **Nuance (postgres.fm):** per-row policy evaluation makes large aggregates an order of
  magnitude slower; `current_setting()` is called per row unless wrapped in a scalar subquery
  (InitPlan); **the planner does not use policy predicates for scan selection** — apps must
  duplicate the tenant filter in WHERE anyway, so RLS works best as defense-in-depth, never as
  the sole filter; most real-world "RLS is slow" cases are missing indexes.
- **Benchmark (dev.to):** simple single-condition policy on 1M rows ≈ **<2% p95 overhead**;
  the tenant_id index dominates (26× speedup) — "RLS overhead is index-shaped, not
  policy-shaped." Applies only to simple policies.
- **AWS RLS blog:** pooled (shared schema) model = lowest cost of the three partitioning
  models; app-layer WHERE characterized as "hoping"; recommends single app role (non-owner) +
  session variable + tenant_id column.

**Fit to SRW:** the funnel already duplicates tenant filters in WHERE (the postgres.fm
prerequisite), and the biggest isolation weakness (A3: ownership-in-code-only tiers, gate
convention on new endpoints) is precisely the failure mode RLS covers. Costs: asyncpg pooling
needs per-transaction context, the app role must not own the tables, and the audit/vector DBs
would need denormalized org/user columns for local policies (already planned for
`usage_events`). The existing `scripts/check_endpoint_auth.py` CI inventory is today's
(cheaper) guard for the same class.

### B5. Billing / quota units (⚠ zero claims from this angle reached adversarial verification
in either run — synthesis flags Q5 as open; items below are extractions from first-party docs)

- **OpenRouter orgs:** prepaid **shared wallet per org**; any member draws from it; only
  admins buy credits; org docs define **no per-member caps** — but the **Management API**
  supports one key per end user with a **per-key USD spend cap auto-resetting
  daily/weekly/monthly** (sub-caps within the shared wallet, not separate wallets).
  Enforcement rejects over-limit requests pre-upstream, per-request check → concurrent bursts
  can slightly overshoot. Control-plane keys can't do inference. Default org size cap: 10
  members (support exception beyond).
- **Anthropic workspaces:** hierarchical quotas — workspace (sub-org) limits can only be set
  **lower** than org limits; unset inherits; **org-wide limit always binds even if workspace
  limits oversubscribe**. Per-workspace monthly spend cap + threshold notifications +
  per-model-tier RPM/ITPM/OTPM. Per-member spend limits exist only in the auto-created Claude
  Code workspace (per-user keys minted at first sign-in, dying on member removal). Org cap:
  100 active workspaces; immutable Default Workspace.
- **AWS Builders' Library (fairness):** quotas at per-tenant granularity (reject only the
  offending tenant's excess); two quota axes = **concurrency** (things running at once) and
  **rate**; for operations whose true cost is unknown until completion (≈ LLM tokens): **admit
  at cheapest-case estimate, retroactively debit actual cost, allow the balance to go
  negative** until it replenishes; burst into unused capacity with in-quota traffic taking
  priority; cost-control quotas are a fixed, explicit product feature (unlike protection
  quotas, which auto-grow).
- **Lago / market:** wallet needs atomic balance ops; define an explicit zero-balance policy
  (hard block vs auto-top-up); hybrid seat+usage is now the majority SaaS pattern (>60%);
  Vercel = $20/seat + usage with included credit; Cursor = subscription-granted credit pool.

---

## Part C — Synthesis (recommendations to react to, not decisions)

1. **Org model:** GitHub/Neon/Vercel-convergent — users are global identities; orgs are the
   only ownership primitive; **auto-create a personal org per user at signup** (real row,
   `kind='personal'`, not NULL-means-personal); never build in-place personal→org conversion
   (GitHub deprecated theirs; provide "move project to org" instead); never store wallet value
   on the user.
2. **Keycloak:** stay single-realm; orgs are **DB-canonical** (KC has no per-org RBAC even in
   26.6, and the multi-org `organization`-claim path has a confirmed-by-maintainer gap with
   global IdPs plus an org-picker login UX we don't want; further open token-refresh /
   second-client claim bugs). Org context is carried **explicitly per request** (URL prefix
   `/o/{slug}` per the M2.B sketch, or header) — Clerk documents the browser-global
   session-cookie active-org as a multi-tab trap, so `srw_sessions` stores the *default* org,
   not the authoritative per-request context. Adopt **KC Organizations per customer org
   lazily, exactly when a customer wants enterprise SSO** — per-org IdP + email-domain
   routing + (unmanaged) membership work correctly in that configuration; each customer gets
   their own IdP object via the admin REST API (one IdP serves one org). **Adopting KC Orgs
   implies bumping the chart's Keycloak image from 26.2 to ≥26.6** (org groups 26.6.0,
   invitation management 26.5+, org fine-grained admin permissions 26.7.0). Realm-per-org is
   rejected on evidence (blast radius in A1, `sub` collisions, per-org service stacks;
   scaling numbers unverified but directionally consistent).
3. **Admin split:** `platform_operator` (fleet, catalog, system settings, org admission,
   billing ops, security events — **no content reads**) vs org roles owner/admin/member in
   `organization_members`. Content access becomes **break-glass**: consent-gated (AWS
   FAS-style; GitLab's missing-consent criticism is our differentiator), time-boxed,
   reason-logged, session-bracketed in `security_events` (GitLab start/end pattern),
   individually attributed. Kill or operator-gate the god-mode side doors (raw tables,
   admin-scope token minting). For German B2B: target ISO 27001 (+BSI C5 later), not SOC 2;
   AVV with sub-processor list incl. LLM providers; documented staff confidentiality; no
   training/analytics on tenant content without consent (Art. 28(10)).
4. **RLS:** not in M2 v1. The funnel + endpoint-inventory CI already guard the same failure
   mode more cheaply. Revisit as selective hardening (high-sensitivity tables, denormalized
   org_id, fail-closed session variable) once org columns exist — it's additive then.
5. **Billing/quotas:** the wallet moves to the org (personal org = personal wallet, uniform);
   hierarchy = org wallet → project/member **sub-caps that can only be set lower than the org
   cap** (Anthropic pattern) with per-member caps as OpenRouter-style sub-caps; LLM jobs use
   **admit-cheap → retroactive debit → negative-balance-allowed** (AWS pattern — resolves the
   billing stub's open "reserve strategy" question); quota axes = concurrent jobs +
   spend-rate; hard-stop at zero stays (already locked in the billing stub).

**Open forks needing a human call:**
- Personal orgs: hidden ("Personal" pseudo-org UX) vs visible first-class org from day one?
- Which of the ~18 PLATFORM feature kill-switches (TTS library, user-experts, sudo rules)
  should later gain per-org overrides?
- Break-glass consent model: org-owner consent required (AWS-style) vs notify-only
  (audit-bracketed) — affects support SLAs for unresponsive customers.
- When to actually build: M2 stays demand-gated (Fork B); this doc exists so the first "we
  want the hosted version for our team" request starts a ~2–3 week build, not a research
  project.
