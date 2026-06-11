---
tags:
  - feature
  - auth
  - admission
  - onboarding
  - multi-tenancy
  - keycloak
related:
  - "[[saas_roadmap]]"
  - "[[multi_tenancy]]"
  - "[[auth_bff_and_api_tokens]]"
  - "[[cockpit_owned_auth_ui]]"
aliases:
  - app-side admission
  - user approval workflow
  - pending users
  - admission gate
  - bulk approve
---

# App-Side Admission (user approval moves from Keycloak roles into the orchestrator)

> Captured 2026-06-10 from the SaaS-readiness assessment follow-up conversation;
> refined same day into an implementation design (code-verified seams + slices).

**Status:** ✅ S1–S3 **shipped + tested 2026-06-10** (migration `0024`, seam
flip + login write-through in `auth.py`, bulk `POST /api/admin/users/approve`
in the endpoint inventory, cockpit pending list + `user_registered` SSE admin
toast — commit `fbce77fb` and neighbors). **S4 (drop the `user`-role fallback)
deliberately pending a soak window** — before dropping, glance at the admin
page for role-holders who never logged in and approve them manually.
**Triggered by:** [`saas_roadmap.md`](../saas_roadmap.md) Tier 0 flagged "Keycloak
self-registration broken." Discussing it reframed the problem: registration *works*
— what's missing is the **admission step** after it, and the current mechanism makes
admission a manual per-user chore in the Keycloak console.

---

## How admission works today (what led us here)

- The Keycloak `srw` realm has `registrationAllowed: true` — strangers can and do
  register.
- The orchestrator derives approval from the access token on **every request**:
  `is_approved = "user" in realm_roles or is_admin`
  (`orchestrator/security/auth.py:233`). Nothing is persisted — that one line *is*
  the entire admission system, and the `user` realm role is its only state.
- New registrants don't get the `user` role (the `default-roles-srw` composite
  doesn't include it), so they authenticate fine but every endpoint 403s via
  `require_approved_user`.
- Admitting someone therefore means: open Keycloak admin → find user → Role
  mapping → assign `user`. Keycloak's console has **no bulk role assignment**, so
  it's one user at a time, every time.

### Why that's the wrong shape

1. **Admission is product business state living in the IdP.** Pending → active →
   (later: suspended, waitlisted, banned, billing-delinquent) is an application
   workflow. The production norm is: IdP answers *who are you* (authn), the app DB
   answers *may you use this product* (admission). Stuffing admission into realm
   role mappings means every future state transition needs Keycloak plumbing.
2. **Operationally painful** — the one-at-a-time console workflow above; no
   overview of who's waiting, no notification when someone registers, no audit of
   who approved whom.
3. **Pre-approval resource provisioning.** JIT user provisioning runs *before* the
   approval gate (`auth.py:262`): an unapproved stranger's first login already
   fires `_ensure_cloud_user` + `_ensure_gitea_user` — Gitea and cloud accounts get
   created for people no human ever admitted.
4. **No pending visibility for the admin.** (Corrected during design: the
   *user-facing* pending UX already exists — the cockpit shell renders a
   pending-approval screen for authenticated-but-unapproved users, `app.ts:59`
   + the `pendingApproval` computed at `app.ts:213`, fed by `is_approved` on
   `/auth/me`; `require_approved_user`'s docstring documents this contract.)
   What's missing is the **admin** side: no pending list, no count, no approve
   action anywhere in the product.
5. **Revocation latency.** Role changes only take effect at the next token
   refresh. A DB flag checked per-request admits *and suspends* instantly — the
   suspend direction is the one that matters in an abuse scenario.

## Alternatives considered (and rejected)

- **Register-as-disabled in Keycloak** (the original idea: new accounts start
  deactivated, admin bulk-enables). Rejected: stock Keycloak cannot create
  registrants disabled — it needs a custom registration-flow SPI / event-listener
  extension (Java, maintained across ~3 KC majors a year); the admin console has
  no bulk *enable* either (only bulk delete), so the hoped-for batch workflow
  doesn't exist without custom UI anyway; and disabled users get Keycloak's raw
  "Account is disabled" error — they can't log in to see status or even complete
  email verification. Admin-approval-after-registration is one of Keycloak's
  oldest declined feature requests; upstream's position is that approval
  workflows belong in the application.
- **Group-mapped role as a stopgap** (an `approved` group carrying the `user`
  realm role; batch-approve via Groups → approved → Members → Add member, which
  *is* multi-select). Zero code, fixes the one-at-a-time annoyance today, needs no
  orchestrator change. But it fixes none of the layering, provisioning-leak, UX,
  or audit problems. Keep in the back pocket if relief is needed before this
  ships.
- **Auto-grant via the `default-roles-srw` composite** (the roadmap's original
  Tier 0 fix). Correct for fully-open SaaS, but it removes the human gate
  entirely — premature while the abuse layer (rate limits, pod quotas) doesn't
  exist.

## What app-side admission is

The standard gated-signup SaaS pattern: **Keycloak authenticates anyone who
registers; the orchestrator's `users` table owns admission.**

What already exists (code-verified 2026-06-10) and is *reused, not built*:
the user-facing pending screen (`app.ts:59`/`213`), `is_approved` serialized
through `/auth/me` (`bff.py:340`) and the `User` model (`api.model.ts:199`),
the admin Users page with checkbox/badge components (`views/admin/users/`),
the `PATCH /api/admin/users/{id}` flag-toggle endpoint (`main.py:17791`), the
notification service (SSE + email, `notification_service.py`), and the
endpoint-inventory snapshot test that will force the new endpoint to declare
its auth. The genuinely new pieces are: one migration, the seam flip, a bulk
endpoint, ~1 admin-page section, and moving two `create_task` calls.

---

## Implementation design

### 1. Data model — migration `0024_user_admission.sql`

```sql
ALTER TABLE users ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN approved_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN approved_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN preferred_username TEXT;

-- Backfill ONLY admins. Deliberately NOT a blanket TRUE: users rows already
-- exist for never-admitted registrants (JIT provisioning runs before the
-- approval gate), and a blanket backfill would silently admit them.
UPDATE users SET is_approved = TRUE, approved_at = NOW() WHERE is_admin = TRUE;
```

Legacy role-holders are *not* backfilled — they migrate organically via the
login write-through (next section). `approved_by = NULL` + `approved_at` set
means "migrated from Keycloak role / system", a real admin UUID means a human
clicked approve. `preferred_username` is persisted at JIT time because
approval-time provisioning (§5) needs it and today it's transient-only.

### 2. Admission semantics — the seam flip + organic migration

All changes live in `_resolve_user_from_claims` (`auth.py:217`):

```python
role_approved = "user" in realm_roles or is_admin   # unchanged computation
db_approved   = user_row["is_approved"]             # NEW: real column
is_approved   = db_approved or role_approved        # transition semantics
```

**Write-through migration:** when `role_approved and not db_approved`, append
`is_approved=TRUE, approved_at=now(), approved_by=NULL` to the *existing*
`needs_update` batch (the same one that already syncs email/display_name/
is_admin). Every legacy role-holder self-migrates on their first request
after deploy — zero manual work, and never-admitted strangers stay `FALSE`.

| DB flag | KC `user` role | Effective (transition) | Side effect |
|---|---|---|---|
| TRUE | any | approved | — |
| FALSE | yes | approved | write-through sets DB flag TRUE (once) |
| FALSE | no | **pending** | pending screen; admin sees them in Pending list |
| any | `admin` role | approved | admins are never pending |

- JIT first login: `upsert_user_from_oidc` (`postgres.py:5908`) gains
  `is_approved=role_approved` + `preferred_username` params.
- **PAT/MCP paths stop forcing `is_approved=True`** (`auth.py:334`, `:367`,
  `:480`): the row's flag flows through instead. Today's force-True rationale
  ("a PAT could only be minted by an approved user") breaks the moment
  suspension exists — un-approving a user must also kill their PATs/MCP
  tokens. The rows are already loaded in those paths, so this is deleting
  three lines, not adding a query.
- No lockout path: admins are always approved via `or is_admin`, and the
  existing PATCH guard already refuses self-clearing `is_admin`.

### 3. API surface

- `AdminUserUpdate` (`main.py:2805`) gains `is_approved: bool | None`. The
  PATCH handler stamps `approved_at=now(), approved_by=<admin id>` when
  setting TRUE; setting FALSE is **suspension** (flag off, `approved_at`
  kept as history).
- New `POST /api/admin/users/approve` with body `{user_ids: [...]}` —
  single transaction, same stamping, returns per-id results. Admin-only via
  `_require_admin`. Gets a row in `docs/security/endpoint_inventory.txt`
  (the snapshot test fails the build until it does — by design).
- `list_users` (`postgres.py:5836`) SELECT gains the new columns so the
  admin page can render status.
- `POST /api/users` (admin-created users, `main.py:17696`): created with
  `is_approved=TRUE` — admin creation *is* approval.

### 4. Cockpit — admin side only (user side already shipped)

`views/admin/users/admin-users.component.ts` + `admin-users.service.ts`:

- Status column: Approved / **Pending** badge (`AppBadgeComponent` already
  imported there).
- "Pending (N)" filter chip + count; default filter All.
- Checkbox multi-select column + **"Approve selected"** button → the bulk
  endpoint. This is the workflow that motivated the whole feature.
- Update the stale comment at `user.service.ts:37` ("has 'user' role in
  Keycloak") once the source of truth moves.
- i18n EN+DE for the handful of new strings (house rule from automations).

### 5. Provisioning + notifications

- Gate the two JIT `asyncio.create_task(_ensure_cloud_user/_ensure_gitea_user)`
  calls (`auth.py:276-292`) on effective approval — closes the pre-approval
  resource leak.
- Fire the same ensures at **approval time** (PATCH/bulk handlers), built
  from row data: `sub=keycloak_sub`, `email`, `display_name`,
  `preferred_username` (now persisted), issuer from OIDC config. Both
  helpers are idempotent ensure-style, so the JIT-vs-approval overlap for
  role-migrated users is harmless.
- New `notify_admins_user_registered` in `notification_service.py`
  (pattern: `notify_automation_auto_disabled` — SSE + email per
  `_get_user_channels`; respects quiet hours, registration isn't
  safety-critical). Fired from the JIT first-login branch when the new user
  lands unapproved.
- Optional polish, not v1-blocking: email the user on approval.

### 6. Slices (each independently shippable)

| Slice | Content | Effort |
|---|---|---|
| **S1 backend core** | migration + seam flip + write-through + PAT/MCP force-True removal + PATCH/bulk endpoints + inventory regen + unit tests | ~½ day |
| **S2 cockpit admin** | status column, pending filter, multi-select + Approve selected, service method, specs | ~½ day |
| **S3 provisioning + notify** | gate JIT ensures on approval, fire on approve, `notify_admins_user_registered` | ~2–3 h |
| **S4 retire fallback** (after soak) | drop `or "user" in realm_roles` (keep `or is_admin`); first check the admin page for role-holders who never logged in during the soak and approve them manually. KC's `user` role becomes decorative; realm cleanup optional | trivial |

### 7. Verification (k3d, the README smoke-test pattern)

1. Register a fresh user at `https://localhost/` → lands on the pending
   screen (exists today; now backed by the DB flag).
2. Admin sees "Pending (1)" on the Users page; SSE/email notification
   arrived.
3. Bulk-select two pending users → Approve selected → both flip; rows show
   `approved_at`/`approved_by`; Gitea + cloud users got ensured (pod logs).
4. Approved user refreshes → full UI; creates a job; **sees only their own
   jobs** — this is the live M1.A cross-user validation that motivated
   Tier 0 ordering.
5. Suspend the user (PATCH `is_approved=false`) → their next request 403s,
   pending screen returns, their PAT stops working.
6. `pytest tests/test_endpoint_inventory.py` + the new unit tests green;
   `ruff` clean; cockpit `vitest` green.

### Known limitations (accepted for v1)

- Suspension takes effect on the next HTTP request; already-open WebSocket/
  SSE streams and running jobs aren't force-closed. Follow-up if abuse ever
  makes it matter.
- Role-holders who never log in during the soak window need a one-time
  manual approve before S4 drops the fallback (admin-page glance — user
  count is small).
- The `verifyEmail`/`email.enabled` fresh-install dead-end stays a separate
  chart-defaults fix (unchanged from the concept).

## Relationship to the roadmap

- **Replaces** the Tier 0 "Keycloak self-registration fix" item in
  [`saas_roadmap.md`](../saas_roadmap.md) — same effort slot, but yields the bulk
  approval workflow + audit + pending UX instead of patching the workflow we
  dislike.
- **Fork B (open SaaS):** not throwaway — "pending approval" *is* waitlist
  infrastructure at launch, and the same flag later carries
  suspension/abuse-ban/billing-hold semantics.
- **Fork A (enterprise pilot):** unaffected — the pilot endgame is IdP
  federation/brokering where the customer's directory group governs access; that
  composes cleanly with an app-side gate (brokered users can be auto-approved
  per-IdP).
- **Orthogonal leftover:** the email-verification dead-end on fresh installs
  (`verifyEmail: true` while `email.enabled: false` → registrants can never
  verify) is a chart-defaults issue and stays its own small fix regardless of
  this doc.
