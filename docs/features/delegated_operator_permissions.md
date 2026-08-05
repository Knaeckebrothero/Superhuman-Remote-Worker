---
tags:
  - feature
  - auth
  - authorization
  - admin
  - operator
  - rbac
  - cockpit
  - orchestrator
related:
  - "[[multi_tenancy]]"
  - "[[app_side_admission]]"
  - "[[admin_view_as_user]]"
  - "[[security_event_log]]"
  - "[[global_expert_management]]"
  - "[[usage_dashboard]]"
aliases:
  - delegated operator permissions
  - operator role
  - scoped administration
  - management grants
  - delegated admin
---

# Delegated Operator Permissions — scoped administration between admin and user

> Add an **operator** tier between the current root admin and regular users, but
> do not model it as another all-or-nothing boolean. An operator is an ordinary
> approved Keycloak/SRW user who receives explicit **management permissions**
> from SRW, constrained to named resource scopes. Root admins decide which
> management surfaces an operator can use and which users, projects, teams, or
> organizations those permissions cover.

**Status:** Design refined through repository and external-practice review on
2026-08-05; not implemented.

**Primary decision:** Keycloak remains the authentication, SSO, MFA, and root-
admin identity authority. SRW Postgres is authoritative for delegated operator
permissions and their scopes. A Keycloak `operator` role is not required and,
if added later for external identity-governance visibility, is never sufficient
by itself to authorize an SRW management action.

**First delivery slice:** delegated user admission/suspension, user capability-
grant management, and scoped usage/cost visibility. Fleet, provider, system-
settings, and organization administration follow on the same foundation.

## Decision summary

1. An operator remains `is_admin=false`. Management permissions never activate
   the current admin visibility bypass.
2. Management permissions are stored separately from `capability_grants`.
   Management grants answer **who may administer something**; capability grants
   answer **what an agent configuration may use**.
3. Every persisted permission is an explicit action plus an explicit scope.
   Unknown permissions and unknown scope kinds deny by default.
4. v1 is allow-only. Within each required authorization leg, one assignment
   must independently satisfy action, scope, target, expiry, credential policy,
   and all constraints. A compound action may require several legs with
   different permission keys, but constraints from separate assignments are
   never merged within a leg to manufacture an allow.
5. Permission keys are independent: for example, `users.suspend` does not
   imply `users.read`. Presets explicitly include the companion permissions
   their workflow needs.
6. Root admins are the only principals allowed to add, change, or revoke
   operator assignments. Operators cannot delegate management authority.
7. Operators cannot mutate themselves, root admins, or any other active
   operator. Read-only visibility of privileged principals is separately
   permissioned and may be allowed.
8. Administrative authority and content access remain separate. A project
   operator does not gain access to its chats, prompts, files, or repositories
   unless they also receive ordinary project membership or an approved
   break-glass session.
9. A Postgres-backed mutation revalidates and locks its authorizing assignment
   in the same transaction as the target change and audit insert. Revocation
   therefore orders deterministically against new mutations.
10. Personal PATs, MCP tokens, project-scoped MCP tokens, internal forwarding,
    and arbitrary OIDC service-account tokens do not inherit management
    authority in v1. Explicit management service principals are a separate
    future design.
11. A successful Postgres mutation and its audit record commit atomically.
    External Keycloak/fleet/provider actions instead use a durable operation or
    outbox with `requested`, `succeeded`, and `failed` outcomes. Denied attempts
    emit the existing best-effort security event.
12. Membership changes use a compound authorization with independent
    destination and affected-user legs; permission to manage project A is not
    permission to import any arbitrary account into it.
13. Presets such as “Cost viewer” or “Project operator” expand into explicit
    assignments. A later catalog addition never silently broadens an existing
    preset assignment.
14. Permission keys are immutable contracts with a versioned catalog lifecycle.
    A key is not assignable until its resolver, enforcement points, audit
    behavior, UI copy, and security matrix all exist.

## Why this feature exists

The current system has two application-wide privilege states:

| State | Current authority |
|---|---|
| Root admin | Keycloak `admin` realm role, reconciled to `users.is_admin`; global resource bypass plus all `_require_admin` routes. |
| Approved user | `users.is_approved`; content visibility comes from ownership and `project_members`. |

There is also a per-project role hierarchy — `viewer`, `editor`, `owner` — but
those are content/workflow memberships, not delegated application management.
The Keycloak realm declares a `viewer` role for future stakeholder dashboards,
but `orchestrator/security/auth.py` currently interprets only `admin` and the
legacy `user` admission role. A system-wide stakeholder/guest principal is
therefore not implemented yet.

This leaves no safe way to express common operating responsibilities:

- approve or suspend regular users without becoming root admin;
- view cost and usage for one project, team, or organization;
- manage selected capability-grant keys for a cohort of regular users;
- operate jobs or fleet resources without reading customer content;
- administer membership for selected projects;
- manage providers or system settings without being able to appoint another
  operator or mint a god-mode token.

Giving all of these people the current `admin` role is materially broader than
their job. Adding a single `is_operator` boolean would reproduce the same
problem one level lower. The required model is scoped delegated administration.

## Repository findings and prerequisites

A 2026-08-05 implementation audit found that this feature cannot safely be
implemented as a few replacements of `_require_admin`:

- the endpoint inventory currently classifies 76 routes as
  `admin:_require_admin`; `orchestrator/main.py` contains 78 direct calls plus
  roughly 34 inline `is_admin` branches, so Phase 0 must produce a reviewed,
  machine-readable permission map rather than rely on route prefixes;
- `PUT /api/users/{user_id}` currently requires only an approved user and can
  update another user's display name, avatar, or email. It must become
  self-only (or move privileged profile changes behind an explicit management
  permission) before the protected-target invariant is considered true;
- OIDC resolution still treats the legacy Keycloak `user` realm role as
  approval and writes `is_approved=true` back into Postgres. Retiring that
  migration fallback, or adding an authoritative suspension override, is a
  hard prerequisite for `users.suspend`; otherwise a suspended legacy user can
  reapprove themselves on their next request;
- `GET /api/users` is a global authenticated directory and currently exposes
  management-adjacent flags. Management pages need a separate scoped DTO and
  SQL-filtered query; the generic directory must be reviewed and reduced to the
  fields ordinary product workflows actually need;
- cookie and direct OIDC authentication currently reach the same user resolver
  without a normalized auth-method/principal context, while PAT and MCP paths
  are tagged separately. The management gate needs an authoritative credential
  classification rather than inferring it from missing fields;
- the CSRF middleware skips its checks for a request carrying both a session
  cookie and a Bearer header, while authentication resolves the cookie first.
  Those precedence rules must be made consistent before a cookie-only
  management-mutation policy is security-relevant;
- current user, capability-grant, and bulk-approval DB helpers acquire their own
  connections. Delegated mutations need transaction-aware service methods that
  accept one caller-owned connection so authorization, row locks, mutation, and
  audit cannot split across transactions;
- usage summary, grouped usage, job statistics, and fleet statistics do not all
  share the same visibility shape. A scoped cost operator cannot safely reuse
  the current admin page or binary `is_admin` branching unchanged;
- the existing Keycloak group-sync client authenticates with broad admin
  username/password credentials and intentionally degrades on failure. Live
  protected-target verification needs a least-privilege runtime service
  credential and a fail-closed error contract; it must not silently inherit the
  group-sync client's availability behavior.

Suspension also needs a lifecycle contract beyond flipping a row: invalidate
the user's BFF sessions, prevent PAT/MCP use on the next request, and publish a
targeted account-state event that makes open Cockpit/SSE/WebSocket clients clear
data and disconnect. Already-running jobs are not cancelled implicitly; that is
a separate explicit operational action and audit event.

These are feature prerequisites, not reasons to grant the operator broader
authority. They are included in Phase 0 and the negative test matrix below.

## Goals

- Let root admins delegate individual management actions.
- Scope each action to global, organization, team, project, or individual-user
  targets as appropriate.
- Support useful named presets without making presets the authorization source
  of truth.
- Make revocation immediate without waiting for a Keycloak token refresh.
- Prevent vertical escalation into root admin or operator administration.
- Prevent horizontal escalation outside the assigned scope.
- Keep control-plane authority separate from content-plane visibility.
- Reuse the existing centralized resource-access and audit conventions.
- Fit the decided M2 organization model without requiring M2 for the first
  global/user/project-scoped slice.

## Non-goals

- Replacing Keycloak authentication, SSO, MFA, or root-admin identity.
- Giving ordinary SRW operators direct access to the Keycloak Admin Console.
- Replacing `project_members` roles or the future `organization_members` roles.
- Reusing agent capability grants as management permissions.
- Giving operators implicit access to user content.
- Completing the M2 organization/team schema in the first slice.
- Building a general-purpose policy language. Constraints are typed and
  catalog-defined, not arbitrary expressions or scripts.
- Automatically delegating all current `_require_admin` endpoints. Every admin
  surface must be classified deliberately.

## Terminology

| Term | Meaning |
|---|---|
| **Root admin** | A caller whose unshadowed `real_is_admin` is true, currently derived from the Keycloak `admin` realm role. Root admins bootstrap and control operator authority. |
| **Operator** | An approved non-admin user with at least one active SRW management grant. `is_operator` is derived, not a mutable user flag. |
| **Management permission** | A catalogued control-plane action such as `users.suspend` or `usage.read`. |
| **Management grant** | Assignment of one management permission to one user for one scope, optionally with typed constraints and expiry. |
| **Capability grant** | Existing user/project/global restriction governing agent capabilities such as shell, VM, model, and autonomy. |
| **Content membership** | Existing project viewer/editor/owner access, or future organization membership, governing ordinary resource visibility. |
| **Privileged target** | A DB-known or live-Keycloak root admin, or a user with any active management grant. Mutation of privileged targets is root-admin-only. |
| **Scope** | The bounded set of targets to which a management grant applies. |

## Authority model

```text
Keycloak identity
      │
      ▼
require_approved_user ───────────────► ordinary content gates
      │                                ownership / project membership
      │
      ▼
management permission evaluator
      │
      ├── action allowed?
      ├── target scope matched?
      ├── target protected?
      ├── constraint satisfied?
      └── auth method permitted?
      │
      ▼
delegated management endpoint
```

The management evaluator never changes `is_admin`, never returns the `"all"`
sentinel from `user_visible_project_ids`, and never participates in
`require_job_access` or `require_thread_owner`. This makes accidental content
access through operator status structurally difficult.

### Principal classes

| Principal | Authentication | Management | Content |
|---|---|---|---|
| Root admin | Keycloak `admin` realm role | Full bootstrap authority; current behavior remains during the first slice | Current admin bypass until the M2 split retires it |
| Platform operator | Ordinary Keycloak user + SRW management grants/preset | Explicit global control-plane permissions | None by default; break-glass or membership only |
| Scoped operator | Ordinary Keycloak user + scoped SRW management grants | Only assigned actions within assigned scopes | None by default; membership only |
| Regular user | Ordinary Keycloak user + SRW admission | Self-service and project workflow only | Ownership + project membership |
| Stakeholder/viewer | Future product persona built from read-only memberships | None | Read-only membership scope |

### Relationship to the M2 admin split

`docs/multi_tenancy.md` already decides that the current global `is_admin`
behavior will split into a content-blind `platform_operator` and organization
roles `owner/admin/member`, with consent-gated break-glass access to content.
This feature supplies the concrete permission mechanism underneath that
decision:

- `platform_operator` becomes a built-in preset over explicit global management
  permissions, not a second global bypass flag;
- organization owner/admin roles can later derive organization-scoped
  management permissions through the same evaluator;
- direct operator assignments remain root-admin-controlled in v1;
- retiring the root admin content bypass remains part of M2, not a prerequisite
  for the first delegated-management slice.

## Keycloak boundary

### What Keycloak can do

The repository currently deploys the `quay.io/keycloak/keycloak:26.2` image.
Keycloak 26.2 Fine-Grained Admin Permissions V2 can delegate administration of
Keycloak-owned users, groups, clients, and roles. Keycloak 26.6 added
organization groups; 26.7 added organization fine-grained administration plus
role assignment/inheritance through those groups. Those organization
operations remain much coarser than SRW's project, cost, capability, and
admission actions. Keycloak Authorization Services can represent arbitrary
resources and scopes only when an application also implements the policy-
enforcement point. For SRW's chosen per-object relationship model, that would
also require mapping/synchronizing SRW relationships or supplying equivalent
runtime attributes; Authorization Services does not universally require a
physical resource row for every protected object.

Those mechanisms are useful when the protected object is owned by Keycloak.
They protect the Keycloak Admin Console and Admin REST API, not SRW routes, and
broad Keycloak admin roles bypass fine-grained evaluation. They do not make
Keycloak understand SRW usage rows, jobs, capability grants, project contents,
fleet operations, or cross-database visibility rules.

Official references:

- https://www.keycloak.org/2025/05/fgap-kc-26-2
- https://www.keycloak.org/2026/04/keycloak-2660-released
- https://www.keycloak.org/2026/07/keycloak-2670-released
- https://www.keycloak.org/docs/latest/server_admin/#managing-access-to-realm-resources
- https://www.keycloak.org/docs/latest/authorization_services/index.html

### Decision: SRW owns operator authorization

Every human operator is a normal user in the same Keycloak realm:

```text
Keycloak:
  authenticated user
  no SRW `admin` role

SRW users row:
  is_approved = true
  is_admin = false

SRW management_grants:
  usage.read / project / <project-id>
  users.suspend / organization / <org-id>
```

This follows the existing split used by app-side admission: identity answers
“who are you,” while SRW business state answers “may you use or administer this
product.” It also avoids:

- one Keycloak role per project or user cohort;
- duplicating dynamic SRW resources into Keycloak Authorization Services;
- token-refresh latency for revocation;
- authorization outages when Keycloak is reachable for login but its policy
  service is unavailable;
- splitting the operator audit trail between Keycloak and SRW.

### Optional Keycloak role

A coarse client role such as `srw-operator-eligible` may be introduced later if
an enterprise identity-governance system needs to report who is eligible for
delegation. It is advisory input only:

- it does not make the user an operator;
- it does not open a Cockpit route;
- it does not satisfy a backend management gate;
- removing it may trigger reconciliation, but SRW grant revocation remains the
  authoritative action.

### Keycloak identity operations

If a future operator permission covers a Keycloak-owned operation — password
reset, account disablement, organization invitation, or group membership — the
operator still calls SRW:

```text
operator → SRW permission/scope check → SRW audit → Keycloak service account
```

The operator does not receive `manage-users` or direct Admin Console access.
The SRW runtime uses a dedicated least-privilege service account, not a human
operator token and not the broad master-realm credentials currently used by
project group sync. The SRW request and durable operation record remain the
source of application audit even when the downstream result is a Keycloak
event.

## External design precedents

The design deliberately adopts useful invariants from mature systems without
importing their policy engines:

| Source | Relevant behavior | SRW application |
|---|---|---|
| Keycloak 26.7 fine-grained admin permissions | Operations are independent, queries are filtered to permitted resources, broad admin roles bypass fine-grained policy, and delegated realm admins cannot assign administrative roles. | Do not infer `view` from `manage`; filter in SQL before pagination; keep root/operator assignment outside operator authority. |
| Kubernetes RBAC | A caller cannot create a role containing authority they lack or bind a role they cannot exercise unless separately granted `escalate`/`bind`; wildcards absorb future verbs/resources. | Root-only delegation in v1. If org-local delegation is later allowed, the grant must be no broader than the grantor's authority at the same or narrower scope, with any override separately named and audited. Never use wildcard permissions. |
| AWS IAM permissions boundaries | A boundary limits maximum authority but does not grant authority by itself; effective access is an intersection. | Typed constraints narrow a management assignment and never independently grant an action. |
| GitHub custom organization roles | Organization-setting permissions are separate from repository access; role definition and role assignment are separate authorities; the UI exposes only covered settings. | Keep control-plane authority separate from project content, keep catalog definition code-owned, and drive management navigation from effective permissions. |
| OpenFGA immutable models | Authorization model versions are immutable/pinned and complex changes require staged tuple migration. | Treat permission-key semantics as immutable, version the catalog, migrate assignments explicitly, and shadow-evaluate risky catalog changes before activation. |
| Zanzibar | Authorization changes and protected operations need a defined consistency order. | Recheck and lock the matching assignment in the mutation transaction; state the exact revocation linearization rule. |

Primary references:

- https://kubernetes.io/docs/reference/access-authn-authz/rbac/#privilege-escalation-prevention-and-bootstrapping
- https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html
- https://docs.github.com/en/enterprise-cloud@latest/organizations/managing-peoples-access-to-your-organization-with-roles/permissions-of-custom-organization-roles
- https://openfga.dev/docs/getting-started/immutable-models
- https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/

An external PDP remains unnecessary for v1. SRW already owns the authoritative
resource relationships and must enforce them in its API and SQL queries.
Keycloak 26.7 also exposes an experimental AuthZen-compatible PDP API, but SRW
is pinned to 26.2 and would still need a PEP plus an SRW relationship/attribute
model. Moving decisions to Authorization Services, AuthZen, or a relationship
engine now would add another availability boundary and policy administration
surface without removing SRW enforcement. The catalog and pure evaluator
remain isolated enough to revisit that decision if policy volume or cross-
service reuse later justifies it.

## Permission catalog

Permissions are code-catalogued, like capability grants. Each entry defines:

- stable permission key;
- target resource type;
- allowed scope kinds;
- read or mutation class;
- supported constraint schema;
- allowed principal/credential classes;
- risk tier, audit level, reason requirement, required LoA, and maximum
  authentication age;
- lifecycle state (`internal`, `assignable`, or `deprecated`) and catalog
  revision;
- stable `label_key`/`description_key`; localized text remains frontend-owned.

Unknown database rows never grant authority. The grant-management API rejects
unknown permission keys, scope kinds, and constraint fields.

Permission keys are immutable security contracts. A released key may be
narrowed compatibly, but it must never be reused or silently broadened to cover
a new action. Broader behavior receives a new key. Moving an entry to
`assignable` requires all scope resolvers, enforcement points, audit metadata,
localized UI copy, endpoint-inventory classification, and golden policy tests
to be present. Deprecation requires an explicit assignment migration; it is not
implemented by changing the meaning of old rows. Risky catalog revisions are
shadow-evaluated against the prior revision before activation, and decisions
record the revision that evaluated them.

### Initial catalog

| Permission | Action | Allowed scopes | Phase |
|---|---|---|---|
| `users.read` | View management-safe user projections | global, user | v1 |
| `users.approve` | Admit a pending regular user | global | v1 |
| `users.suspend` | Suspend an admitted regular user | global, user | v1 |
| `capability_grants.read` | View explicit/effective capability grants | global, project, user | v1 |
| `capability_grants.manage_user` | Set/revoke allowed capability keys on regular-user scopes | global, user | v1 |
| `usage.read` | View usage/cost aggregates without content payloads | global, project, user | v1 |
| `projects.manage_members` | Destination-side authority to add/remove/change project members | global, organization, project | follow-up |
| `users.manage_membership` | User-side authority to place a regular user into or remove them from a project/team | global, organization, team, user | follow-up |
| `jobs.operate` | Pause/resume/cancel jobs without reading prompts/files | global, organization, project | follow-up |
| `fleet.read` | View fleet health and infrastructure metadata | global | follow-up |
| `fleet.operate` | Restart/deregister/reconcile fleet resources | global | follow-up, high risk |
| `model_catalog.manage` | Manage model catalog rows/defaults | global | follow-up |
| `provider_credentials.manage` | Rotate provider credentials without returning secret values | global | follow-up, high risk |
| `system_settings.manage` | Change selected platform settings | global | follow-up, high risk |
| `security_events.read` | Read security events in scope | global, organization | follow-up |

The table lists scope combinations that are assignable in each permission's
first release. M2 may add organization scopes, and the later team schema may
add team scopes, through an explicit catalog revision and resolver tests;
existing assignments remain unchanged because every row names its scope.

`capability_grants.manage_project` is deliberately deferred. A project grant
affects every non-admin principal using that project, including another
operator. Delegating it without either calculating and protecting every
affected principal or introducing exclusions would violate the invariant that
operators cannot change other operators' grants. Root admins retain project and
global capability-grant management in v1.

### Typed constraints

Permission keys determine the surface; validated constraints narrow what may be
done within it. Constraints are not a free-form policy language.

For example:

```json
{
  "permission": "capability_grants.manage_user",
  "scope_kind": "organization",
  "scope_id": "<org-uuid>",
  "constraints": {
    "allowed_keys": ["shell_tools", "delegation"],
    "value_ceilings": {
      "permission_mode": "auto_accept"
    }
  }
}
```

Rules:

- `allowed_keys` is explicit; no wildcard means future capability keys stay
  forbidden.
- Boolean, enum, and list values validate through the existing capability
  catalog semantics.
- Enum ceilings use the catalog order.
- A requested list must be a subset of the allowed list; otherwise reject the
  request rather than silently clipping it to a different value.
- Missing required constraints deny rather than infer an unrestricted value.
- The backend validates constraints at assignment time and again at use time so
  a later catalog change cannot turn stale data into broader authority.

### Presets

Presets are an admin UX convenience:

| Preset | Example explicit permissions |
|---|---|
| Cost viewer | `usage.read` |
| User manager | `users.read`, `users.approve`, `users.suspend` |
| Grant manager | `users.read`, `capability_grants.read`, `capability_grants.manage_user` |
| Project operator | `usage.read`, `projects.manage_members`, `jobs.operate` for selected projects plus an explicit `users.manage_membership` cohort if membership changes are included |
| Platform operator | Fleet, model catalog, billing/usage, security events, and explicitly selected high-risk platform permissions; no content access |

Selecting a preset writes its current explicit rows. The database does not
store `role=platform_operator` and resolve a moving wildcard later. Updating a
preset therefore does not silently expand existing operators; the root admin
must review and apply the new permission explicitly.

## Scope model

| Scope kind | Meaning | Availability |
|---|---|---|
| `global` | All non-protected targets of the permission's resource type | v1 |
| `user` | One specific regular user | v1 |
| `project` | One project and project-owned management data | v1 |
| `organization` | One future DB-canonical organization | M2 |
| `team` | One first-class app-side user cohort within an organization | after team schema |

Scope kinds are permission-specific. For example, `usage.read` can sensibly be
project-scoped, while `users.approve` cannot be project-scoped unless a pending
invite already binds the user to that project or organization.

### User cohorts

There is no general app-side team/group entity today. Keycloak project groups
exist to synchronize downstream cloud/Gitea membership; they are not the SRW
authorization source. This feature must not turn those eventually-consistent
groups or token claims into management policy.

Until DB-canonical organizations/teams land, a root admin can delegate:

- one global permission;
- one or more explicit user scopes;
- one or more project scopes for permissions whose target is project data.

Arbitrary “this department's users” scoping requires a first-class SRW `team`
or organization membership. It should land on the M2 schema rather than create
a competing Keycloak-group authority.

### Scope evaluation

The client may request a view within one of its allowed scopes, but it never
asserts that a target belongs to that scope. The server loads the target and
derives its scope memberships before evaluation.

Examples:

- `usage.read/project/A` may query project A, never an arbitrary `user_id` sent
  by the browser.
- `users.suspend/organization/A` loads the target's active organization
  membership and checks A.
- `capability_grants.manage_user/team/A` loads team membership, confirms the
  target is not privileged, then validates the requested grant key/value.
- moving a user or project into the operator's scope is itself a separate
  permission; it cannot be used inside the same request to manufacture
  authority over the target.

Scope containment is permission-specific and server-defined:

- `global` covers every target type allowed by that catalog entry, subject to
  protected-target and constraint rules;
- `organization` dynamically covers the organization's current members,
  projects, and organization-owned management records explicitly named by the
  permission resolver;
- `team` dynamically covers current members of that app-side team, not a
  Keycloak group claim;
- `project` covers that project and the management metadata explicitly named by
  the permission, never project content by implication;
- `user` covers exactly that user.

Users may match several scopes. For reads, independently authorized target sets
are unioned and pushed into the SQL query before filtering, counting, ordering,
or pagination. For each mutation authorization leg, candidate assignments are
evaluated independently and one assignment must authorize that leg's complete
proposed change. A compound operation may require multiple independently
satisfied legs. The evaluator never takes `allowed_keys` from one row and a
value ceiling from another within a leg. A narrow assignment also does not
subtract from an independently valid broad assignment. v1 has no persisted
deny rows; non-overridable security invariants are checked after candidate
allows and can still reject the compound operation. If explicit denies are
introduced later, deny wins.

### Relationship changes require both sides

A relationship mutation such as adding user U to project P has two protected
objects. It requires all of the following in one decision:

1. one independently sufficient `projects.manage_members` grant matching
   destination P;
2. one independently sufficient `users.manage_membership` grant matching U's
   server-derived user/org/team scope;
3. organization/tenant compatibility for both objects;
4. the ordinary membership transition rules; and
5. the protected-target shield.

The resulting compound authorization retains both legs, matched grants, and
scopes. Neither `users.read` nor project authority alone supplies the other
half. This prevents a project-scoped operator from importing arbitrary platform
users into their scope and then exercising another permission over them. The
same pattern applies to team membership and future organization moves.

## Data model

### Active assignments

New app-DB tables (final migration number chosen at implementation time):

```sql
CREATE TABLE management_principal_state (
    subject_user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    revision        BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE management_grants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_user_id UUID NOT NULL
                    REFERENCES management_principal_state(subject_user_id)
                    ON DELETE CASCADE,
    permission_key  TEXT NOT NULL,
    scope_kind      TEXT NOT NULL,
    scope_id        UUID,
    constraints_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    constraint_schema_version SMALLINT NOT NULL DEFAULT 1,
    source_preset_key TEXT,
    source_preset_revision INTEGER,
    granted_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    CHECK (scope_kind IN ('global', 'user', 'project')),
    CHECK (jsonb_typeof(constraints_json) = 'object'),
    CHECK (expires_at IS NULL OR expires_at > granted_at),
    CHECK (
        (scope_kind = 'global' AND scope_id IS NULL) OR
        (scope_kind <> 'global' AND scope_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_management_grant
    ON management_grants
       (subject_user_id, permission_key, scope_kind, scope_id)
    NULLS NOT DISTINCT;

CREATE INDEX ix_management_grants_scope
    ON management_grants (scope_kind, scope_id)
    WHERE scope_id IS NOT NULL;

CREATE INDEX ix_management_grants_expiry
    ON management_grants (expires_at)
    WHERE expires_at IS NOT NULL;
```

Implementation details:

- `management_principal_state` is created with the first assignment and remains
  until the user is deleted, even after the final grant is revoked. It provides
  the synchronization row and monotonic public revision; never expose Postgres
  `xmin` as a revision;
- an approved non-admin who has never had a state row has synthetic grant
  revision `0`; a root admin also reports grant revision `0`, with root page
  access derived separately from a fresh root-role decision rather than a
  fabricated assignment;
- one row exists per `(subject_user_id, permission_key, scope_kind, scope_id)`;
- `scope_id` is polymorphic, matching the existing capability-grant pattern;
  deletion services remove rows in the same transaction as their user/project/
  organization target;
- the v1 database constraint admits only scope resolvers actually shipped in
  that release. M2 migrations deliberately extend it to `organization`, then
  `team`; merely appearing in the future catalog is not enough;
- every writer validates that the scoped target exists. User/project deletion
  snapshots affected assignments into the audit event before cascade/cleanup;
- expired rows never authorize and are pruned separately;
- assignment writes are root-admin-only and require the subject to be an
  approved non-admin human principal at assignment time. Phase 0 adds a durable
  `users.principal_kind` (`human`/`service`) after an audited backfill and the
  strict Keycloak lookup rejects service-account principals; the implementation
  never infers “human” merely from a non-null `keycloak_sub`;
- preset source fields are provenance/UI metadata only and never participate in
  authorization;
- `is_operator` is computed from active, unexpired rows and is never stored on
  `users`.

The root editor replaces a subject's complete desired grant set atomically. It
locks `management_principal_state FOR UPDATE`, compares the caller's expected
revision, validates and normalizes the complete set, applies the diff,
increments the revision once, and writes the redacted audit event. Operator
database mutations lock their actor state `FOR SHARE` and load grants only
after acquiring that lock.

Polymorphic scopes need a lifecycle lock that a foreign key cannot provide.
Every assignment, authorization, or mutation involving a non-global assignment
scope or target acquires a transaction-scoped shared Postgres advisory lock
derived from a versioned, stable `(scope_kind, scope_id)` key; deletion acquires
the exclusive form. Multiple keys are acquired in
lexicographic scope order before row locks. A hash collision may add contention
but cannot grant authority. Because a caller may need an optimistic query to
discover its existing grant scopes, it acquires the candidate locks, then
reloads under the actor-state lock and retries the transaction if an applicable
scope was not locked.

The complete canonical order is: lifecycle advisory locks, all involved
`users` rows in UUID order (shared for identity-only actors, update for mutated
targets or an assignment subject), their `management_principal_state` rows in
UUID order, capability-policy synchronization where applicable, then
membership and domain-resource rows. Root assignment follows the same order,
making first assignment serialize with a concurrent target mutation even when
the state row does not yet exist. Expiry comparisons use database `NOW()`.
Lock timeout/deadlock is retryable and never falls back to an unlocked check.

User/project/scope deletion holds the exclusive lifecycle lock, discovers and
locks every affected subject deterministically, snapshots and deletes scoped
grants, increments every affected grant revision, appends lifecycle audit
events, publishes post-commit permission invalidations, and deletes the
resource in the same transaction. Expiry pruning performs the same
revision/audit/invalidation protocol in bounded subject batches. All legacy
deletion and assignment writers must use this service before any permission is
assignable.

This gives the database-local revocation guarantee:

> When a grant-revocation response returns, every Postgres mutation authorized
> under the old revision has committed or rolled back. A later database
> authorization check observes the new revision.

It does not cancel a read response already in flight or an external operation
already dispatched. Those limits are specified below.

### Read-backed audit

Management permission changes and successful operator mutations are too
sensitive for a write-only table. Migration `0045` removed the old capability-
grant audit specifically because it had no reader. This feature therefore adds
the audit store, query endpoint, and Cockpit history view in the same slice.

Suggested `management_events` shape:

| Column | Purpose |
|---|---|
| `id`, `created_at` | Stable ordering and retention |
| `schema_version`, `event_type`, `outcome` | Versioned event contract and result |
| `actor_user_id`, `actor_external_sub`, `actor_kind` | Stable identity snapshot; no FK so history survives deletion |
| `real_actor_user_id` | Future impersonation/delegation attribution |
| `auth_method`, `oidc_client_id`, `acr`, `auth_time` | Credential and authentication strength |
| `primary_permission_key` | Queryable primary permission exercised or changed |
| `authorization_legs_json` | Bounded list of every required leg: permission, matched grant (nullable for root), scope, target, and constraint outcome |
| `actor_grant_revision`, `catalog_revision` | Grant/catalog basis shared by the legs; no FK so deletion does not erase history |
| `relationship_evidence_json` | Redacted IDs/roles/versions of memberships used for a scoped decision; empty for global/user-direct decisions |
| `scope_kind`, `scope_id` | Evaluated management scope |
| `target_type`, `target_id` | User/project/setting/etc. affected |
| `before_json`, `after_json` | Redacted state transition; never secrets or content payloads |
| `reason` | Required for assignment changes and high-risk actions |
| `request_id`, `route_template`, `http_method`, `client_ip` | Investigation correlation; network metadata is untrusted |
| `retention_class` | `privilege_lifecycle`, `operator_mutation`, or `sensitive_read` |

For a successful database mutation, the resource change and audit event commit
in one transaction. If the audit insert fails, the mutation fails. This differs
from denied-access logging: a failed security-event insert must never turn a
403 into a 500.

Denied operator attempts use `security_events` with a new
`management_denied` event type and the same best-effort logging behavior as
other access denials.

`management_events` is append-only to the application: runtime code receives
INSERT/SELECT but no UPDATE/DELETE path. That is not the same as tamper-proof —
a database owner can still rewrite the database. The initial implementation
uses a maintenance-owned retention sweeper with defaults of 365 days for
`privilege_lifecycle`/`operator_mutation` and 90 days for `sensitive_read`;
denials retain the existing `security_events` default. Deployments may lengthen
those values, and product/legal review is required before GA. A later hardening
phase may export and independently sign closed audit batches. Audit-retention
deletion and failed success/denial inserts both emit metrics.

## Authorization evaluator

### Proposed modules

- `orchestrator/security/management.py` — versioned permission catalog, typed
  targets/constraints, pure matching, and `ManagementDecision`.
- `orchestrator/services/management_authorization.py` — auth-context checks,
  scope resolution, SQL filter plans, protected-target checks, and
  transaction-aware authorization.
- `orchestrator/services/management_mutations.py` — transaction owners for
  Postgres-backed user/grant operations and audit writes.
- `orchestrator/routers/management.py` — `/api/manage/*` enforcement points.
- `orchestrator/routers/admin_operators.py` — root-only assignment, evaluation,
  and history APIs.
- `orchestrator/database/postgres.py` — low-level methods that accept an
  existing connection; they do not open a second transaction for these flows.

The pure matching logic should remain independent of FastAPI and DB access so
the security matrix can be tested hermetically. Routers remain thin and never
reconstruct a policy decision from client-provided scope data.

### Decision and enforcement shape

The evaluator returns a first-class `ManagementDecision` for one authorization
leg rather than a bare boolean. An allowed leg binds actor, permission, target,
matched grant ID and scope, actor grant revision, catalog revision,
server-derived relationship evidence, constraint result, credential class,
and audit policy. `ManagementAuthorization` contains one or more required legs
and succeeds only if all legs and cross-leg invariants pass. The grant revision
versions only the subject's assignment set; it does not pretend to version
dynamic project/organization/team relationships. A denial carries a stable
internal reason code but no sensitive trace for the operator. Root admins may
use an audited dry-run endpoint that calls this same evaluator for the editor's
effective-access preview.

Read paths resolve an immutable server-side filter plan:

```python
decision = await require_management_read(
    request,
    postgres_db,
    permission="users.read",
)
rows = await management_users.list(conn, filter_plan=decision.filter_plan)
```

Filtering happens in SQL before counts, ordering, and pagination. Loading all
rows and filtering in Python is not an acceptable enforcement point.

Mutation paths use one caller-owned transaction:

```python
async with postgres_db.acquire() as conn:
    async with conn.transaction():
        decision = await authorize_management_mutation(
            request,
            conn,
            permission="users.suspend",
            target=ManagementTarget(kind="user", id=user_id),
            proposed_change={"is_approved": False},
        )
        result = await management_users.suspend(conn, user_id=user_id)
        await append_management_event(conn, decision=decision, result=result)
```

In production the domain service owns this block so a router cannot authorize
target A and accidentally mutate target B. Evaluation order is:

1. Resolve an approved caller and a normalized human credential context;
   reject unknown, PAT, MCP, internal, or service-principal contexts.
2. Resolve the immutable catalog entry and reject unavailable action/scope/
   constraint combinations.
3. Perform any live Keycloak protected-target read before opening the database
   transaction; dependency failure returns a retryable 503 without mutation.
4. Begin the transaction and lock all involved user rows, principal-state rows,
   memberships, and target resources in the documented canonical order.
5. Revalidate actor approval/root state and load active grants only after the
   actor-state lock. An unshadowed root admin then takes the catalogued bypass.
6. Derive target scope memberships from locked authoritative rows.
7. Evaluate each required leg and its candidate assignments independently; no
   cross-row constraint merge is permitted within a leg.
8. Combine all required legs, then apply non-overridable self/privileged-
   target, tenant, relationship, and scope-laundering invariants.
9. Only after authorization succeeds, enforce any recent-authentication/LoA
   requirement. This avoids revealing high-assurance requirements to an
   unauthorized caller.
10. Mutate and append the success event, then commit once. On denial, roll back
    and emit `management_denied` best-effort with a safe internal code.

Read-only permissions may include admins/operators if scope allows. The
privileged-target shield applies to mutation, role/grant assignment, and
impersonation-style actions, not automatically to aggregate cost visibility.

### Privileged-target classification

A user is protected when any condition holds:

- `users.is_admin` is true, even if a live lookup no longer finds the role;
- SRW has any unexpired management-grant row for them, even if its permission
  has since become unknown/deprecated and therefore cannot authorize; or
- they hold the Keycloak `admin` realm role.

`users.is_admin` is conservatively trusted when true but can lag an out-of-band
Keycloak promotion when false. An operator mutation must therefore augment it
with current role state. For a Keycloak-backed target, the management service
verifies current realm roles through a dedicated least-privilege Keycloak
runtime client. If
current role state cannot be verified, the operation returns 503 and emits a
dependency-failure security signal; it does not misreport missing permission as
403. Root-admin actions retain a separately audited bootstrap path.

The user row and active management assignments must be locked consistently
during privilege classification and mutation so an operator cannot race an
operator promotion/revocation. The root-only assignment path takes the same
lock order.

A Postgres row lock cannot serialize against a concurrent role change made in
the Keycloak console. The live lookup reduces stale-cache exposure but cannot
make two control planes atomic. SRW-mediated future admin promotions must mark
the principal protected before dispatching the Keycloak operation and reconcile
the result. Out-of-band/emergency promotions retain a small lookup-to-mutation
race and are reconciled periodically or through a future Keycloak event
listener. Tests claim race freedom only for SRW management-grant changes, not
for arbitrary concurrent Keycloak console changes.

Suspending an operator is a root-only emergency action and atomically revokes
their management grants with privilege-lifecycle audit events. Reapproval does
not silently revive operator authority. A suspended account with a corrupt or
otherwise unrecognized unexpired row remains protected until root cleanup, even
though that row grants no action.

### Permission resolution and caching

Resolve from Postgres per privileged request in v1. Management calls are low
volume and correctness matters more than eliminating one indexed query. The
Cockpit permission summary is UX state, never an authorization cache.

If caching becomes necessary later:

- cache by subject plus a monotonic permission version;
- invalidate synchronously on set/revoke;
- use a short hard TTL as a fallback;
- never place the authoritative assignment set only in a BFF session or access
  token.

## Endpoint design

### Root-only operator administration

Suggested endpoints:

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/admin/operators` | List users with active assignments and summaries |
| `GET` | `/api/admin/operators/candidates` | Find approved non-admin human subjects, including existing operators |
| `GET` | `/api/admin/management-catalog` | Return the versioned assignable catalog and explicit preset definitions |
| `GET` | `/api/admin/management-scope-targets` | Search paginated valid scope targets for one permission/scope-kind pair |
| `GET` | `/api/admin/operators/{user_id}/permissions` | Complete assignment set, revision, and strong `ETag` |
| `PUT` | `/api/admin/operators/{user_id}/permissions` | Atomically replace the complete explicit set; require `If-Match` and reason |
| `POST` | `/api/admin/operators/evaluate` | Audited root-only dry run using the production evaluator |
| `GET` | `/api/admin/management-events` | Query read-backed audit by actor, target, permission, scope, and time |

Candidate and scope-target discovery are cursor-paginated, search-bounded, and
return minimum editor projections. Scope discovery validates the requested
permission/scope-kind combination against the active catalog and derives
eligible targets server-side. The catalog response carries its own immutable
catalog revision and explicit preset expansions.

The collection PUT prevents a preset or multi-row edit from partially applying.
`GET` returns only the canonical persisted assignment representation plus
`ETag: "mg-<revision>"` and `Cache-Control: no-store`; catalog labels and
time-derived `active` flags are fetched/derived separately. Every writer that
changes a GET-visible field, including expiry pruning and scope cleanup,
increments the revision. PUT requires the exact strong tag in `If-Match`;
missing preconditions return 428, stale state 412, `If-Match: *` returns a typed
400, and an invalid desired set returns 422.

The server validates and normalizes the submitted desired set before storing
it, so a successful PUT returns 204 without an ETag; Cockpit immediately
refetches the canonical GET representation and its new validator. This follows
RFC 9110's restriction on returning a validator when PUT transformed the
submitted representation. The transaction still calculates a redacted diff
and increments the subject revision once. Individual-row endpoints are
unnecessary in v1. Reference:
https://www.rfc-editor.org/rfc/rfc9110.html#section-9.3.4.

Every mutation requires a normalized reason. Root operator administration uses
a fresh interactive root-admin gate with the same credential and step-up
policy as other management calls; `_require_admin` alone is insufficient
because it currently accepts an admin's PAT/MCP credentials. Assignment and
revocation also verify the caller's current effective/composite Keycloak admin
role through the strict runtime verifier; a cached `users.is_admin` value or
old BFF claim is insufficient. Verifier unavailability returns 503 except for
a separately controlled and audited recovery procedure. No management
permission can call these routes.

### Delegable management facades

Do not loosen a mixed `/api/admin/*` route wholesale. v1 adds action-specific
facades that call shared domain services; existing root-only routes remain
compatibility wrappers until their UI and clients migrate.

| Management surface | Gate | Additional behavior |
|---|---|---|
| `GET /api/manage/users` | any of `users.read`, `users.approve`, `users.suspend` | Purpose-filtered, paginated projection with server-computed `allowed_actions`; mutation-only callers see only the minimum actionable target rows |
| `POST /api/manage/users/{id}/approve` | `users.approve` | Approve one regular target; preserve post-commit idempotent provisioning |
| `POST /api/manage/users/{id}/suspend` | `users.suspend` | Suspend one regular target; require confirmation/reason |
| `POST /api/manage/users/approve` | `users.approve` | Preflight all targets; reject the whole batch for any unauthorized/privileged target and audit each changed user |
| `GET /api/manage/capability-grants` | `capability_grants.read` or `capability_grants.manage_user` | Read permission returns its eligible view; manage-only returns only target/schema/current-value data required for its authorized edits |
| user-scope grant PUT/DELETE | `capability_grants.manage_user` | Validate protected target, constraints, and simulated post-change effective value |
| project/global grant PUT/DELETE | Root admin in v1 | Project transitive-target question deferred; global is high risk |
| `GET /api/manage/usage/dashboard` | `usage.read` | One purpose-consistent snapshot with scope and grant/catalog revision metadata |

Every authenticated management JSON response uses an envelope with safe
`management_meta`: `subject_user_id`, `grant_revision`, `catalog_revision`, and
`evaluated_at`. Problem responses carry the same metadata when a human identity
was resolved. This applies to user and capability-grant reads as well as usage;
it lets Cockpit reject an older in-flight response after an assignment change.
The grant revision does not version dynamic memberships, so scope-change events
also advance a client-local request generation as described below.

The current `PATCH /api/admin/users/{id}` combines `is_approved` and
`can_use_vm`, so changing only its route gate would permit field smuggling.
Action-specific services reject unknown/mixed fields. The generic `/api/users`
directory is not the management API; Phase 0 makes profile updates self-only
and gives directory/self/management responses distinct DTOs before delegated
user mutations ship.

All management errors use stable machine-readable problem codes (RFC 9457
`application/problem+json`) without exposing the matched policy trace. Cockpit
uses those codes to distinguish forbidden, stale revision, dependency
unavailable, validation failure, and empty data.

Mutation permission does not imply the independent general read permission.
Instead, an action contract returns only the minimum target identity and
current state required to perform that action. Thus an approve-only operator
can see in-scope pending users but cannot browse admitted users, and a
grant-manage-only operator can see the eligible target/value editor state but
not unrelated grants. `page_access` and notification deep links are calculated
from those same workflow contracts, not only from presets.

Initial page mapping is explicit: `users` is true for any of `users.read`,
`users.approve`, or `users.suspend`; `capability_grants` is true for either
`capability_grants.read` or `capability_grants.manage_user`; and `usage` is true
for `usage.read`. The server owns this mapping and tests each individual and
combined assignment case.

Bulk approval accepts 1–100 unique, valid UUIDs; duplicates or an oversized
body return 422 before dependency work. Current Keycloak role lookups use
bounded concurrency. The transaction locks all targets in UUID order,
revalidates every target and authorization leg, and is all-or-nothing: any
unauthorized, protected, missing, or invalid target changes none of them.
Successful changes receive individual audit events in the same commit and the
response reports per-target idempotent/changed status.

### Admin surface classification

Before changing endpoints, inventory every `_require_admin` call and inline
`is_admin` branch into three buckets:

1. **Root-only bootstrap/security:** root/operator assignment, admin role,
   admin-scope token minting, raw database access, break-glass override.
2. **Explicitly delegable control plane:** users, usage, grants, fleet, model
   catalog, provider credentials, system settings, security events.
3. **Content plane:** job/thread prompts, files, sources, logs, and customer
   data. Keep membership-scoped or break-glass; never open merely because a
   caller is an operator.

High-risk control-plane actions can become delegable later, but only through
their own permission keys, global scope, required reason, and any future
step-up-auth requirement. There is no `admin.everything` operator grant.

`scripts/check_endpoint_auth.py` must learn the new management gate and snapshot
`management:<literal-permission-key>`, not merely the helper name. A dynamic or
unrecognized permission argument fails inventory generation and requires
review, so CI detects both a lost gate and a route wired to the wrong action.

### External management operations

Postgres cannot atomically commit with Keycloak, a fleet API, or a provider.
External actions therefore create a durable `management_operations` row and a
`requested` event in the authorizing transaction, then return 202 and a
Location header. The row includes operation type/system, actor, permission,
operation class, request-time authorization legs/revisions, target, redacted
desired state, request fingerprint, actor/route-scoped idempotency key, state,
attempts, lease, dispatch-time authorization legs/revisions, downstream request
ID, and safe last-error code.

External request endpoints require a bounded client-supplied `Idempotency-Key`.
`(actor_user_id, route_template, key)` is unique. Reuse with the same canonical
request fingerprint returns the existing operation/Location; reuse with a
different fingerprint returns 409. The mapping remains at least as long as the
configured client retry window and never expires while its operation is still
actionable. The same key, or a stable derived child key, is propagated to a
downstream API that supports idempotency.

`GET /api/manage/operations/{operation_id}` exposes safe status only to the
original requester or a root admin; action-specific request endpoints return
that URL and never expose a downstream credential or raw provider response.

A worker first moves a `pending`/`retry` row to leased `claimed` with
`FOR UPDATE SKIP LOCKED` and commits. For an independently
`authorization_bound` command, dispatch then reruns the complete current actor,
credential, target, protected-principal, scope, and constraint decision. In one
transaction it takes the canonical lifecycle/user/actor-state/target locks,
locks the claimed operation, records the dispatch-time authorization basis, and
changes `claimed -> executing`. Root grant revocation takes the conflicting
actor-state lock and cancels that actor's queued/claimed authorization-bound
operations before committing. Only after the executing transition commits may
network I/O begin; an executing call may finish after revocation and is
reconciled.

`state_convergence` work caused by an already committed database decision is a
different class. User provisioning after approval, for example, rechecks the
current authoritative desired user state, not whether the original approver
still has a grant. It is not cancelled merely because that grant was later
revoked; a later suspension can instead change the desired state that the
worker converges toward.

Workers use an idempotent desired-state operation or downstream idempotency key.
Result persistence and the `succeeded`/`failed` event share a later database
transaction. Duplicate delivery and an ambiguous crash after an external
success remain possible unless the downstream system supports idempotency;
workers reconcile rather than claim exactly-once behavior. The explicit states
are `pending`, `claimed`, `executing`, `retry`, `succeeded`, `failed`, and
`cancelled`.

## Usage and cost scoping

The existing usage endpoints call `_visibility_kwargs_for_stats`, which has a
binary model: admin gets fleet data; non-admin gets ownership/project-member
data. Do not simulate operator access by setting `is_admin=true`.

Introduce a purpose-aware usage visibility object, for example:

```python
UsageVisibility(
    all_data=False,
    user_ids={...},
    project_ids={...},
    organization_ids={...},
    allowed_groupings={"model", "project"},
    purpose="management_dashboard",
)
```

Resolution rules:

- existing ordinary summary access retains its current own-user OR
  visible-project behavior, while existing grouped/timeseries access may remain
  self-only to avoid exposing co-member identities;
- `/api/manage/usage/dashboard` is a separate management-only view. It uses
  delegated `usage.read` scopes, not an implicit union with personal/content
  visibility, and it does not include job/fleet statistics unless separately
  permissioned;
- a root admin may request all or keep the existing view-as-me behavior;
- a requested project/user filter is intersected with the allowed set;
- grouping by user never returns users outside the resolved visibility object;
- an empty allowed set compiles to SQL `FALSE`, never to omitted filters or a
  fleet-wide query;
- raw-ledger and closed-day rollup paths consume the same visibility contract
  and have parity tests;
- the response carries the scope summary, actor grant revision, catalog
  revision, and evaluation time used for the snapshot;
- multi-query dashboard reads run in one repeatable-read transaction (or one
  equivalent SQL statement) so raw, rollup, and dimension results do not
  describe different database snapshots; the transaction remains writable
  when its catalog audit policy requires an event before response;
- organization IDs are denormalized into the audit ledger when M2 lands, as
  already decided in `multi_tenancy.md`.

A cost viewer can therefore see aggregate spend for project A without being
able to open project A's jobs, prompts, or files.

## Capability-grant management

The two grant systems must remain visually and structurally distinct:

| System | Question answered | Example |
|---|---|---|
| `management_grants` | May Alice administer this setting for this target? | Alice may manage selected user grants for Team A |
| `capability_grants` | May Bob's agent use this runtime capability? | Bob may use shell tools and auto-accept mode |

For `capability_grants.manage_user`, the evaluator checks:

1. requested scope kind is `user`;
2. target user lies within the management assignment's user/org/team scope;
3. target is neither the caller nor any admin/operator;
4. capability key is in `allowed_keys`;
5. requested value is within any value ceiling;
6. the ordinary capability-grant API validation still succeeds; and
7. both the explicit-row transition and the simulated post-change effective
   capability remain within the operator's ceiling.

That simulation must serialize with every input it reads. Add a singleton
`capability_policy_state(revision)` synchronization row. User-scope grant
writes and membership changes take it `FOR SHARE` after the canonical
management/user locks and then lock the target user's membership and applicable
grant rows. Project/global grant, application-default, and project-deletion
writers take it `FOR UPDATE`; every successful writer increments its revision.
All existing root routes and DB helpers migrate to these connection-aware
services before `capability_grants.manage_user` becomes assignable. This keeps
unrelated user mutations concurrent while preventing a broader grant or
membership change between simulation and commit. The decision and audit event
record the capability-policy revision used.

The final check applies to PUT and DELETE. Capability grants are restrict-only,
so deleting a narrow user row can fall back to a broader project/global/default
value and increase authority. The service simulates resolution with the real
`CATALOG`, `meet`, and `resolve_grants` semantics before writing; list `null`
is treated as unrestricted/top. Audit records redacted before/after explicit
rows and effective values. A delete that would exceed the assignment ceiling
is denied.

Operators do not receive the capability they are allowed to grant. Likewise,
holding a runtime capability grant gives no authority to administer it.

## Cockpit design

### Permission state

New endpoint:

```text
GET /api/manage/context
```

It is available to every approved cookie-authenticated human. A regular user
with no assignments receives a successful empty context rather than 403, with
synthetic `grant_revision: 0`; this lets guards fail closed without treating
ordinary users as an error case. A root admin also has grant revision `0` and
receives root page access only after the current-role check succeeds.

Response shape:

```json
{
  "management_meta": {
    "subject_user_id": "...",
    "evaluated_at": "2026-08-05T12:00:00Z",
    "grant_revision": 42,
    "catalog_revision": "2026-08-05.1"
  },
  "is_root_admin": false,
  "is_operator": true,
  "page_access": {
    "users": true,
    "capability_grants": false,
    "usage": true
  },
  "assignments": [
    {
      "permission": "usage.read",
      "scope": {
        "kind": "project",
        "id": "...",
        "label": "Project A"
      },
      "constraints": {},
      "expires_at": null
    }
  ]
}
```

Cockpit adds a `ManagementPermissionsService` with signals and a permission-
aware route guard. Its `ensureLoaded()` deduplicates one in-flight context
request and the guard awaits it on an initial deep link; it does not copy the
admin guard's arbitrary timeout behavior. The response uses `Cache-Control:
private, no-store`; the service verifies `subject_user_id`, clears state on
logout/identity change, refreshes on focus/navigation and at the nearest
expiry, and listens for targeted `management_permissions_changed` and
`management_scope_changed` SSE notifications. Assignment events carry the new
grant revision. A greater revision marks state stale, clears page data, and
causes lower-revision context/page responses to be discarded. At the same
revision, a later `evaluated_at` may legitimately remove naturally expired
authority. Catalog revisions are opaque: a mismatch marks the response stale
and reloads context rather than ordering the revision strings. Scope events
increment a client-local generation and clear affected data because grant
revision does not version memberships; every context and page request captures
that generation and a response from an older generation is discarded.
Project/organization/team membership changes that can alter an operator's
target set publish the scope event.

Problem handling is deliberately narrower than “any 403 means revoked”:

- `management_authority_changed` means principal/page authority was lost;
  clear data, reload context, and leave the route only if refreshed
  `page_access` denies it;
- `management_target_denied` covers protected or out-of-scope action targets;
  retain the page and show a localized action error;
- 412 stale assignment state reloads context/data but retries only after an
  explicit user action; and
- a typed 503 dependency failure keeps permissions fail-closed while showing
  unavailable state, never pretending the assignment was revoked.

Backend checks remain authoritative; `is_operator` is not added to the general
`User` model.

### Cross-replica invalidation and stream enforcement

The current notification feed is process-local and may turn authentication
failure into an anonymous subscription, so it is not a revocation boundary.
Phase 1 adds a dedicated authenticated management/account event channel backed
by a durable Postgres invalidation outbox. Assignment, scope, session, and
account-state transactions append monotonic events in the same commit. Each
orchestrator replica tails the outbox, using transactional `NOTIFY` only as a
wakeup and polling to recover missed notifications; an optional NATS transport
may accelerate fan-out but is not the source of correctness.

The dedicated SSE route rejects failed authentication, supports bounded replay
with `Last-Event-ID`, and makes a client reload context if its replay cursor is
too old. Every live SSE/WebSocket/session connection is registered locally by
subject/session. A suspension or session-revocation event reaches every replica
and terminates matching connections; periodic heartbeat revalidation catches a
missed event and closes a no-longer-approved session. Permission/scope events
clear management data but do not substitute for the backend's per-request
authorization. Client clearing is therefore privacy UX, while server-side
request/connection checks remain the enforcement boundary.

### Routes and navigation

Delegable pages should live under `/manage/*`:

- `/manage/users`
- `/manage/grants`
- `/manage/usage`
- later `/manage/projects`, `/manage/fleet`, etc.

Keep existing `/admin/users`, `/admin/grants`, and `/admin/usage` wrappers
root-only until their components are decomposed. They currently mix authority:

- Admin Users combines admission with the global VM switch and per-user VM
  capability;
- Admin Grants combines capability grants with root-only application defaults;
- Admin Usage combines usage-ledger data, ordinary content-scoped job
  statistics, and root-only fleet statistics.

Adding a new URL/guard around those components is therefore insufficient.
Extract permission-specific presentational components and ensure a hidden
subpanel also suppresses its API call. Only then may compatible admin bookmarks
redirect or share a wrapper with `/manage/*`.

The sidebar gains a translated **Manage** group separate from **Admin** and
shows only pages enabled by server-computed `page_access`. Route data contains
only a stable page key, and the guard reads `page_access[pageKey]`; Cockpit does
not maintain a second permission expression that can drift from the backend.
Each page displays its effective scope so the operator does not mistake a
filtered view for global state.

Every management service exposes a discriminated state such as `idle`,
`loading`, `loaded`, `forbidden`, `unavailable`, or `error`. It must not copy the
current admin services' behavior of translating every failure into an empty
user/grant list or “metering unavailable.” When scope narrows, old data clears
immediately; superseded requests are cancelled or generation-checked so a late
broad response cannot repaint a narrow view.

### Workflow decomposition

**Users:** `/manage/users` receives a paginated management projection containing
only `id`, display name, email, admission state/timestamps, a coarse protected
indicator, and server-derived `allowed_actions`. It excludes VM/root controls,
`default_project_id`, Keycloak subject, and raw admin/grant state. Email is
included because admission operators must identify registrants; this makes
`users.read` a PII-bearing permission and its list/export behavior is audited
at the catalogued level. Suspension requires confirmation/reason, and bulk
approval visibly reports per-target results. Registration notifications fan
out only to root
admins and `users.approve` operators whose resolved scope matches the pending
target, with the minimum PII required and a `/manage/users` deep link.

**Capability grants:** target search and scope selectors come from management
authorization, never ordinary `/projects` content visibility. The server
returns a filtered editor schema describing readable/editable mode, eligible
targets, allowed keys, enum/list domains, ceilings, and explicit/effective
values. Project/global choices and unrelated application defaults remain
root-only.

**Usage:** `/manage/usage` calls the consolidated management dashboard rather
than the current page's mixture of usage, job, and fleet endpoints. It renders
a server-supplied scope/version banner, cancels stale refreshes, and never calls
root fleet or ordinary job-statistics APIs merely because `usage.read` exists.

### Root admin operator editor

Add an Admin → Operators page with:

- approved non-admin human selector, including existing operators so their
  assignments can be edited or fully revoked;
- preset picker that previews explicit assignments;
- individual permission rows;
- scope selector;
- typed constraint editor;
- optional expiry;
- mandatory reason;
- server-evaluated effective-access preview;
- recent assignment/action history.

The editor loads and replaces the full set with ETag/`If-Match`, then refetches
after a successful 204 to obtain the canonical representation/new ETag. A stale
edit shows the server diff and requires an explicit reload rather than
overwriting a concurrent root-admin change. Preview uses the production
evaluator in dry-run mode, not a TypeScript reimplementation.

The UI refuses to select the current admin, another admin, or an existing
operator as a mutation target where the operation is forbidden. This is UX only;
the backend independently enforces the same rule.

Catalog responses expose translation keys rather than English labels. Reusing
the current admin components requires migrating their hardcoded template and
CSS-generated mobile labels as well as adding new copy to both `en.json` and
`de-DE.json`. Run `npm run i18n:check` and perform an explicit template/CSS copy
review because the current hardcoded-string checker does not cover all markup.

## Audit and security events

Record two complementary trails:

- `management_events`: authoritative, read-backed history of successful
  assignment changes, privileged Postgres mutations, external-operation stages,
  and catalogued sensitive reads; resource change + event are one transaction
  where both live in Postgres.
- `security_events`: best-effort record of denied management attempts, using
  `event_type=management_denied`.

Each read permission has a catalogued `audit_mode`. In the initial catalog,
`users.read`, `capability_grants.read`, and `usage.read` are
`required_sensitive_read`: the service performs the scoped query and inserts a
redacted event in the same writable repeatable-read transaction, commits, and
only then returns data. Audit failure returns a typed 503 and no rows. A future
`best_effort_read` mode may return data on audit failure only while incrementing
an alertable metric. Minimal action-precondition projections exposed by a
mutation permission inherit that permission's required read-audit policy. Read
events store filters/scope, page or aggregate shape, and result count, never
returned PII/cost rows themselves.

The `security_events` migration adds structured management metadata (permission,
scope, target type/id, and safe denial/dependency code, preferably in a bounded
JSONB column) rather than packing it into prose `detail`. A failed denial insert
still returns the original denial but increments an alertable metric.

Never record:

- provider secret values;
- PAT/MCP token bodies;
- user content, prompts, or file contents;
- full request bodies when a narrow before/after projection suffices.

At minimum every event carries actor, permission, evaluated scope, target,
outcome/action, request correlation, and reason where required.

All delegated mutations and all operator-assignment changes require a trimmed,
bounded reason; catalogued low-risk root compatibility calls may temporarily
omit it during migration, but they still produce an event. A reason is context,
not an authentication factor. User approval commits the SRW row and success
event atomically, then durable provisioning operations handle cloud/Gitea work;
an external provisioning result is never represented as part of the database
transaction that could not have committed it.

## Authentication-method policy

Management authority belongs to the human account but is not automatically
delegated to every credential that account can mint.

v1 policy:

| Auth path | Management reads/mutations |
|---|---|
| Cockpit BFF cookie | Allowed subject to permission evaluator and CSRF |
| Direct Keycloak OIDC bearer | Denied for delegated management in v1; a future human API profile requires strict audience/client/flow rules |
| Personal PAT (`ak_`) | Denied |
| MCP token (`srw_`) | Denied regardless of `user`/`all`/`project:*` scope |
| MCP internal forwarded identity | Denied |
| Agent/internal bootstrap key | Denied |
| Unknown or hybrid cookie-plus-Bearer/internal | Denied; never infer safety from a missing field or let CSRF/auth choose different credentials |

Authentication produces a typed credential context separate from the mutable
user dictionary: principal kind (`human`/`service`), selected auth path, OIDC
client and validated audience/authorized party, flow, session identifier hash,
`auth_time`, `acr`, and `amr` where available. Every auth branch sets it
explicitly. Cookie authentication and CSRF use the same selected-path rule;
presenting conflicting credentials is rejected rather than resolved by
different precedence in different middleware.

The policy above applies to new delegated and root operator-administration
routes. Existing `_require_admin` endpoints currently accept admin PAT/MCP and
are compatibility debt: Phase 0 classifies them, and each migrated privileged
surface adopts a catalogued credential policy deliberately. No new management
route relies on `_require_admin` alone.

### Step-up authentication

Catalog entries can define `required_acr` and `max_auth_age_seconds`. Operator
assignment/revocation, provider credentials, security settings, break-glass,
management-credential creation, and destructive fleet actions require recent
strong authentication. The BFF preserves validated `acr` and original
`auth_time`; refreshing an access token does not refresh authentication age.
Before Phase 1 enables assignment mutation, each deployment must map SRW's
logical assurance levels to exact accepted Keycloak ACR values. Missing or
weaker mapping disables that action and fails readiness for the feature; it
never silently downgrades to ordinary login.

Evaluation checks ordinary authorization first. An authorized but
under-authenticated human receives the RFC 9470
`insufficient_user_authentication` 401 result with the configured
`acr_values`/`max_age`; an unauthorized caller receives the ordinary generic
403/404 and learns no assurance requirement. Because the browser presented a
BFF cookie rather than an OAuth access token, this is an RFC-9470-shaped
application problem/continuation signal, not a claim that cookie authentication
implements the RFC's Bearer challenge protocol. A future direct OAuth profile
uses the actual `WWW-Authenticate` challenge. The BFF problem has code
`management_step_up_required` and an opaque server-side challenge ID bound to
subject, issuer/client, BFF session, required ACR/age, and a short expiry.

Cockpit passes that ID and an allowlisted relative return path to a
CSRF-protected `POST /api/auth/step-up/start`; the server, not the browser,
constructs the Keycloak authorization request. The callback performs full OIDC
validation (signature, issuer, audience/authorized party, expiry/issued-at,
state, and nonce), requires the same issuer/client and `sub` as the pre-step-up
session, validates returned ACR and `auth_time` recency, and only then upgrades
that same BFF session. The current generic auth interceptor redirects every
non-logout 401 to ordinary login, so it must recognize the typed step-up
challenge and hand it to this continuation flow instead. The existing no-
parameter `login()` helper is not reused unchanged.

An unsafe request is never automatically replayed after login. A root operator
editor may preserve only its unsaved form draft in identity-bound
`sessionStorage`; after callback it reloads the current grant set/ETag, renders
the pending diff on a confirmation screen, and submits only on another explicit
user action. The draft is cleared on identity mismatch, cancellation, use, or
expiry.

Development Cockpit is cross-origin. CORS must expose `Location` and any
`WWW-Authenticate` emitted by a future OAuth-facing profile, while also
returning equivalent safe continuation/location fields in the problem/202
body where browser handling benefits.

Keycloak supports step-up/LoA, but browser-visible authorization parameters are
not proof by themselves. The server validates returned claims and should use a
server-generated authorization request (PAR/request objects when available).
Reference: https://www.rfc-editor.org/rfc/rfc9470.html and
https://www.keycloak.org/docs/latest/server_admin/#_step-up-flow, plus the
https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation
requirements.

If automation is later required, introduce non-human service principals with
explicit fixed permissions/scopes, audience-bound short-lived credentials,
expiry, rotation, revocation, and separate audit identity. They never inherit a
human user's grants and cannot delegate/operator-manage. Do not silently make
all existing PATs operator-capable.

## Security invariants

1. **No vertical delegation:** only root admins mutate `management_grants`.
2. **No privileged-target mutation:** an operator cannot suspend, delete,
   grant to, revoke from, impersonate, or change roles for self/admin/operator.
3. **No content-by-control:** management assignments never satisfy content
   access helpers.
4. **No client-asserted scope:** the server derives target membership.
5. **No token inheritance:** ordinary PAT/MCP credentials do not inherit
   management authority.
6. **No wildcard expansion:** new permissions/capability keys default denied.
7. **No stale-cache trust:** Keycloak-backed privileged-target checks use a
   strict live verifier or fail without mutation; the residual cross-control-
   plane race is documented rather than hidden behind a DB-lock claim.
8. **No unaudited Postgres success:** privileged DB mutation and management
   audit commit together; external effects use durable staged outcomes.
9. **No scope laundering:** moving a target into scope and acting on it require
   separate authorized operations.
10. **No project-grant transitive mutation in v1:** project/global capability-
    grant writes remain root-only until privileged-principal effects are solved.
11. **No cross-grant composition within a leg:** each authorization leg needs
    one independently sufficient assignment; compound actions may have several
    legs, but partial constraints from different rows never combine within one.
12. **No one-sided relationship write:** both destination and affected-user
    authority are required for membership changes.
13. **No field smuggling:** each action-specific request model maps to one
    permission; unknown or mixed privileged fields reject.
14. **No raw-row-only grant check:** capability PUT/DELETE is authorized against
    its simulated effective post-change value.
15. **No unlocked mutation:** the actor revision/grant is revalidated under a
    transaction lock shared through mutation and audit.
16. **No exactly-once fiction:** external actions are at-least-once and
    reconcilable unless the downstream system supplies idempotency.

## Rollout and compatibility

- No management assignments are backfilled, so supported product workflows do
  not gain authority until a root admin creates the first assignment. Phase 0
  intentionally closes the unsafe cross-user profile update, over-broad
  directory projection, legacy reapproval, and hybrid-credential behaviors;
  those prerequisite hardenings are explicit compatibility exceptions.
- Interactive root admins continue to take the catalogued root bypass on new
  management routes. Legacy `_require_admin` routes retain their existing
  credential behavior only until individually classified/migrated.
- Operators remain non-admins in all content visibility helpers.
- No Keycloak version upgrade or operator realm role is required for v1, but
  strict protected-target reads require a least-privilege runtime credential,
  timeout/readiness monitoring, and a documented unavailable mode. Deployments
  without that verifier cannot enable operator assignment or delegated
  principal mutations.
- Revoking the final active assignment removes derived operator state on the
  next read and has the stronger database-mutation ordering guarantee defined
  above. It does not retroactively retract a response or an executing external
  request.
- Suspending an operator revokes their assignments; reapproval never restores
  them automatically.
- Removing the Cockpit feature does not remove backend enforcement. Rollback
  disables `/api/manage/*` and revokes assignment rows; it does not replace the
  new security prerequisites with a weaker gate.
- The future M2 organization migration adds `organization` scope resolution
  and expands the DB scope check without changing permission keys or direct
  user/project assignments.

## Implementation phases

### Phase 0 — prerequisite hardening and permission map

- Enumerate every `_require_admin` call and inline `is_admin` branch.
- Classify root-only, delegable control plane, and content plane.
- Record the chosen permission key beside every delegable endpoint.
- Make generic user profile mutation self-only and split directory,
  self-profile, and management DTOs.
- Retire the legacy Keycloak `user`-role approval write-through (or add a
  durable suspension override) before delegating suspension.
- Normalize all credential contexts; align cookie/Bearer/internal selection
  with CSRF; reject hybrids/unknowns; harden OIDC audience/authorized-party
  validation before any future direct-bearer management profile.
- Inventory non-human identities and backfill a durable `users.principal_kind`
  without assuming every existing `keycloak_sub` is human.
- Extract transaction-aware user/grant DB helpers that accept an existing
  connection.
- Update the endpoint-inventory checker before changing routes.

### Phase 1 — foundation and root-admin editor

- Add `management_principal_state`, `management_grants`, read-backed
  `management_events`, structured denial metadata, and operation/outbox schema.
- Implement the versioned catalog, pure decision engine, SQL filter plans, and
  transaction-aware mutation service.
- Implement full-set ETag assignment replacement, audit query, production
  evaluator dry run, exact-If-Match/refetch behavior, and revisioned
  expiry/pruning.
- Implement deterministic shared/exclusive scope-lifecycle locks, cleanup
  indexes, and lifecycle audit/revision/invalidation behavior.
- Route all existing admission/suspension, user deletion, project deletion,
  scoped-grant cleanup, and operator expiry writers through the shared
  lifecycle services before the first assignment can be activated.
- Add the strict least-privilege Keycloak current/composite-role verifier with
  bounded concurrency, short timeout, readiness, and 503 behavior.
- Implement `/api/manage/context`, permission-version invalidation, and
  structured problem responses.
- Add the durable cross-replica management/account invalidation outbox,
  authenticated stream, bounded replay, and server-side connection
  revalidation/termination.
- Implement the server-generated step-up continuation, interceptor branch,
  callback confirmation flow, deployment ACR mapping/readiness gate, and
  required CORS response-header exposure.
- Add the candidate, catalog/preset, and scoped-target discovery APIs plus the
  Admin → Operators Cockpit page, server-backed preview, and history.
- Add endpoint-inventory support and the full negative security matrix.
- Ship with no delegable business permission assignable yet; canary the root
  editor and evaluator first.

### Phase 2 — read-only usage canary

- Make `usage.read` assignable first.
- Implement raw/rollup-parity visibility and the consolidated
  `/api/manage/usage/dashboard` snapshot.
- Add `/manage/usage`, scope/version banner, stale-request cancellation, and
  no job/fleet side calls.
- Canary a project cost viewer and prove content gates remain unchanged.

### Phase 3 — user administration

- Make `users.read`, `users.approve`, and `users.suspend` assignable.
- Add action-specific, SQL-scoped management endpoints and transaction/audit
  services; keep VM/root fields out of their models.
- Add scoped registration notifications, session/stream suspension handling,
  and durable post-approval provisioning.
- Extract `/manage/users` from the mixed Admin Users page and browser-verify
  privileged/out-of-scope/batch/revocation behavior.

### Phase 4 — capability-grant administration

- Make `capability_grants.read` and constrained
  `capability_grants.manage_user` assignable.
- Add `capability_policy_state` serialization and migrate root user/project/
  global/default grant plus membership/deletion writers to it before enabling
  operator writes.
- Implement management-derived target/schema discovery plus simulated
  post-change effective-value checks for PUT and DELETE.
- Extract `/manage/grants` without application defaults or project/global
  writes and add the missing component/service test coverage.

### Phase 5 — project and operational control plane

- Delegate project membership only with destination-side
  `projects.manage_members` plus user-side `users.manage_membership`.
- Split content-free job operations from job content reads.
- Add fleet read/operate and model-catalog permissions.
- Add high-risk provider/system-setting permissions with mandatory reason,
  recent-MFA/step-up enforcement, and durable external operations.

### Phase 6 — organizations and teams

- Add organization scope resolution on top of M2 `organization_members`.
- Define an app-side team entity before enabling `team` scope.
- Derive selected org-owner/admin permissions through the same evaluator.
- Add organization-scoped management audit and usage dimensions.
- Decide whether org owners may delegate org-local presets; root-only remains
  the default until explicitly changed.

### Phase 7 — platform-operator split and break-glass

- Materialize the M2 `platform_operator` preset.
- Remove the current global admin content bypass from platform operators.
- Add consent-gated, time-boxed content break-glass sessions with start/end
  security events.
- Remove or break-glass-gate raw table and god-mode token side doors.

## Test strategy

### Pure policy matrix

For every catalog permission and scope kind:

- matching action + matching scope passes;
- wrong action denies;
- wrong scope denies;
- unknown permission/scope/constraint denies;
- expired assignment denies;
- constraint boundary passes and one-step-over denies;
- two partial assignments never merge into one allow;
- independently valid broad/narrow assignments are additive under the
  documented rule;
- `internal`/deprecated/unknown catalog entries cannot be assigned or used;
- catalog revision changes preserve golden decisions or require an explicit
  staged migration;
- root admin passes;
- ordinary user denies;
- operator remains `is_admin=false`.

### Privileged-target matrix

- operator cannot mutate self;
- operator cannot mutate DB-known admin;
- operator cannot mutate a freshly Keycloak-promoted admin whose DB cache is
  still false;
- operator cannot mutate another operator;
- an unrecognized but unexpired management row still protects its subject;
- read-only usage/user visibility may include privileged principals when the
  assignment explicitly allows it;
- Keycloak lookup failure returns 503 and performs no mutation;
- concurrent SRW operator assignment and target mutation serialize under the
  documented lock order;
- tests do not claim a Postgres lock serializes an arbitrary out-of-band
  Keycloak promotion; the accepted residual race and reconciliation path are
  exercised separately;
- root suspension revokes operator assignments and reapproval does not revive
  them.

### Endpoint tests

- every delegated route: root pass, matching operator pass, wrong-permission
  403, wrong-scope 403, ordinary-user 403;
- generic `/api/users` cannot mutate another user or expose management-only
  fields;
- the legacy Keycloak `user` role cannot undo a suspension;
- operator mutations write a redacted `management_events` row atomically in a
  real-Postgres integration test; audit failure rolls back the mutation;
- denied attempts write `management_denied` best-effort;
- PAT/MCP/unknown/direct-bearer versions of the same operator deny; admin
  PAT/MCP cannot call root operator APIs;
- cookie-plus-Bearer and cookie-plus-internal hybrids cannot select the cookie
  identity while bypassing CSRF;
- mixed `is_approved`/VM or unknown fields cannot smuggle a second action;
- SQL filtering occurs before paging/counting; out-of-scope direct lookups
  follow the repository's deliberate 403/404 disclosure rule;
- membership writes require both destination and user authority;
- usage operator sees only assigned dimensions; empty visibility is never
  fleet-wide and raw/rollup results match;
- cost access does not make `require_job_access` pass;
- user grant operator cannot write project/global grants, affect privileged
  targets, or DELETE into an effective value above the ceiling;
- grant-set ETag conflicts cannot lose a concurrent update;
- revocation waits out an older locked DB mutation and later checks deny;
- external operations reauthorize before dispatch, deduplicate by idempotency
  key, expose requested/succeeded/failed/cancelled stages, and reconcile an
  ambiguous worker crash;
- endpoint inventory recognizes and snapshots the literal permission key;
- failed denial logging increments its security metric without changing the
  response.

### Cockpit tests

- permission context clears on logout/identity change, refreshes on expiry/SSE,
  and fails navigation/route closed on error;
- an initial `/manage/*` deep link awaits one deduplicated context load; an
  empty revision-0 regular-user context denies cleanly without a timeout;
- one assignment exposes only its matching Manage page; partial operators see
  no root-only Admin group;
- scope selectors cannot offer unauthorized targets;
- operator editor is root-admin-only;
- users UI has no VM controls, shows protected-target affordances and batch
  results, and requires suspension reason;
- grants UI distinguishes read/edit, offers only allowed targets/keys/values,
  and never calls application-default or project/global writes;
- usage UI makes no job/fleet calls, shows server scope, distinguishes
  forbidden/unavailable/empty, and rejects a late broad response after scope
  narrows;
- revocation followed by structured 403 or version event clears data and
  removes page/navigation without reload;
- protected-target/out-of-scope 403 retains the page, while dependency 503 and
  stale 412 render their distinct non-empty states;
- the generic 401 interceptor routes a typed step-up challenge through the
  server-generated continuation, preserves only the bound draft, and never
  replays the mutation automatically;
- late context, user, grant, and usage responses are all rejected after a
  newer grant revision or scope-generation event;
- Angular signal mocks remain callable with `.set()` / `.update()`;
- both locales pass `npm run i18n:check`, plus explicit template/CSS copy
  review.

### Live acceptance

Create temporary regular users for these scenarios and remove them afterward:

1. **Cost viewer:** sees project A usage, cannot see project B usage, cannot open
   an A job without project membership.
2. **Grant manager:** changes an allowed capability key for regular user A,
   cannot change another key, self, an admin, an operator, a project grant, or a
   global grant.
3. **User manager:** approves/suspends an in-scope regular user, cannot touch a
   privileged or out-of-scope user.
4. **Revocation:** with one mutation intentionally held under the old revision,
   root revocation waits for it, then the operator's next request returns 403;
   an open Cockpit session clears data/navigation without re-login.
5. **Audit:** every successful action and assignment change is queryable with
   actor, target, matched grant/revisions, scope, reason, and redacted
   before/after state.

## Open questions

1. **Team timing.** Does the first customer needing cohort-scoped user
   management justify an app-side `teams` table before full M2, or should the
   feature ship with global/user/project scopes until organizations land?
2. **Step-up deployment mapping.** Which Keycloak ACR values represent SRW's
   required assurance levels in each deployment, and should a high-risk action
   be disabled or fall back to fresh password authentication where no stronger
   LoA flow is configured? It must never silently downgrade to no step-up.
3. **Project capability grants.** Should delegation remain permanently root-
   only, deny when any privileged principal is affected, or gain an explicit
   exclusion/provenance model?
4. **Organization-local delegation.** After M2, may an org owner appoint
   organization-scoped operators, or does root-admin-only delegation remain a
   platform invariant? Default remains root-only until decided; if enabled, a
   grantor may grant only authority they already hold at the same or a narrower
   scope unless a separately named root-only escalation privilege exists.

## Acceptance criteria

- [ ] A root admin can assign/revoke explicit management permissions and scopes
      as one ETag-protected set without changing a Keycloak role.
- [ ] An assigned user remains `is_admin=false` and gains no implicit content
      access.
- [ ] Generic user routes are self/directory scoped, the legacy approval
      write-through cannot undo suspension, and credential/CSRF selection is
      normalized before delegated mutations are enabled.
- [ ] User, grant, and usage workflows enforce action plus scope server-side.
- [ ] Operators cannot mutate themselves, admins, or other operators.
- [ ] One complete assignment authorizes each required leg; constraints from
      separate assignments never merge within a leg, and membership writes
      retain successful authorization legs for both objects.
- [ ] Capability-grant delegation is limited to explicit keys/value ceilings and
      regular-user scopes, including simulated effective values after DELETE.
- [ ] PAT/MCP/direct-bearer/unknown/hybrid credentials do not inherit delegated
      management authority; high-risk actions enforce catalogued step-up.
- [ ] A successful revocation waits out older locked Postgres mutations and all
      later checks observe the new revision without token refresh.
- [ ] Successful Postgres mutations and audit records are atomic and queryable;
      external effects use durable, idempotent/reconcilable operation stages.
- [ ] Denials appear in `security_events` without turning audit failure into a
      different response, and audit failures are observable.
- [ ] Cockpit navigation and routes reflect effective permissions but are never
      the sole enforcement layer; revocation clears open page data.
- [ ] Scoped usage raw/rollup queries agree and an empty visibility set cannot
      become a fleet-wide query.
- [ ] Existing admins and ordinary users retain supported product workflows
      when no management grants exist, except for the explicitly listed Phase
      0 security hardening of unsafe legacy behavior.
- [ ] M2 organization scope can be added without changing the permission model.

## Relevant implementation seams

- `orchestrator/security/auth.py` — Keycloak identity, admission,
  normalized credential context, `is_admin`/`real_is_admin`.
- `orchestrator/security/csrf.py` and `orchestrator/security/oidc.py` — hybrid
  credential precedence and direct-token audience/authorized-party validation.
- `orchestrator/security/access.py` — content/resource gates and admin bypasses.
- `orchestrator/main.py` — `_require_admin`, users, grants, and usage endpoints.
- new `orchestrator/routers/management.py` and `admin_operators.py` — thin
  delegable/root enforcement points rather than more growth in `main.py`.
- `orchestrator/services/grants_service.py` and
  `src/core/capability_grants.py` — existing runtime capability semantics.
- `orchestrator/services/keycloak_admin.py` — existing best-effort, broad
  project-group sync; do not reuse its failure contract for security-critical
  role verification.
- new strict Keycloak principal-protection adapter — least-privilege composite
  role reads, bounded concurrency, timeout/readiness, typed unavailable result.
- `orchestrator/services/usage_ledger.py` and `usage_rollup.py` — purpose-aware
  management visibility and raw/rollup parity.
- `orchestrator/services/notification_service.py` — scope-matched registration
  notifications and management deep links.
- `orchestrator/database/postgres.py` — connection-aware authorization,
  mutation, cleanup, revision, operation, and audit methods.
- `orchestrator/database/migrations/app/` — new schema; do not edit schema
  snapshots.
- `cockpit/src/app/app.config.ts`, `app.routes.ts`, and
  `shell/sidebar/sidebar.component.ts` — management context lifecycle, guard,
  lazy routes, and separate Manage navigation.
- `cockpit/src/app/core/guards/admin.guard.ts` — remains for root-only routes;
  add a separate management-permission guard.
- `cockpit/src/app/core/interceptors/auth.interceptor.ts` and
  `cockpit/src/app/core/services/session.service.ts` — distinguish step-up from
  expired-session 401s and initiate the server-generated continuation.
- `cockpit/src/app/views/admin/users/`, `admin/grants/`, and `admin/usage/` —
  mixed-authority components to decompose before sharing presentation with
  `/manage/*`.
- `scripts/check_endpoint_auth.py` and `docs/security/endpoint_inventory.txt` —
  authorization regression net.
