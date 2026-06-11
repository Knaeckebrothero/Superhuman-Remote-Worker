---
tags:
  - strategy
  - roadmap
  - security
  - multi-tenancy
  - saas
  - isolation
related:
  - "[[multi_tenancy]]"
  - "[[2026-06-09-roadmap-priorities]]"
  - "[[2026-06-09-release-package-and-licensing]]"
  - "[[workspace_network_isolation]]"
  - "[[saas_billing_and_metering]]"
  - "[[auth_bff_and_api_tokens]]"
  - "[[cockpit_owned_auth_ui]]"
  - "[[observability_and_quotas]]"
aliases:
  - saas readiness
  - saas roadmap
  - tenant isolation roadmap
  - security hardening plan
---

# SaaS Readiness & Tenant Isolation — Assessment and Plan

**Date:** 2026-06-10 (truth-up 2026-06-11 — Tier 0 #1 + #3 done; M1.C mount path shipped)
**Status:** Assessment + sequenced plan. Code-verified against the working tree, not
inferred from the tracking doc.
**Purpose:** Establish the *actual* security/isolation posture for going public,
reconcile it against the drifted [`multi_tenancy.md`](multi_tenancy.md) tracker,
and lay out a fork-aware plan that wastes no effort regardless of the
pilot-vs-SaaS decision.

> This doc is the security-hardening companion to the strategic
> [`2026-06-09-roadmap-priorities.md`](strategy/2026-06-09-roadmap-priorities.md).
> Where that doc decides *what wedge to sell*, this one decides *what must be
> true before strangers (or a pilot) touch the system*. The detailed milestone
> tracker remains [`multi_tenancy.md`](multi_tenancy.md); this doc supersedes its
> status columns where they have drifted (see the reconciliation table).

---

## TL;DR

The hard part is done. API-layer tenant isolation (every REST/WS/SSE/MCP
endpoint gated, enforced by a snapshot test) is real and shipped. Two
verified facts change the picture from what the tracker claims:

1. **Workspace egress + per-tenant network tiering is fully shipped**, not
   "design pending." The whole column → seeder → label → policy chain exists.
2. **The cloud per-user credential blocker is smaller than billed.** The
   per-user seam already exists in code; it's filled with static env vars
   today. The job is wiring OAuth into an existing seam, not building from
   scratch.

That leaves **one genuine blocker for open SaaS** (per-user cloud OAuth) and
**one onboarding bug that blocks even testing** (Keycloak self-registration).
Everything else is defense-in-depth or GDPR table-stakes that can follow.

**For near-term revenue, the single-tenant pilot path is nearly ready** and
matches the strategic roadmap's bet. Open multi-tenant SaaS is a ~4–6 week
hardening arc plus billing.

> **Truth-up 2026-06-11 — both headline gaps closed within 24h of writing.**
> (1) **Admission shipped**: [app-side admission](features/app_side_admission.md)
> S1–S3 implemented + tested 2026-06-10 (`users.is_approved`, cockpit
> bulk-approve, registration toast; S4 role-fallback drop pending soak).
> (2) **M1.C's mount path shipped**: workspace cloud mounts are per-user via
> Keycloak token-exchange impersonation, live-validated on k3d + dev
> ([rclone_cloud_mount.md](features/rclone_cloud_mount.md) Phase 6). M1.C
> residual = the personal WebDAV *datasource* still uses the shared service
> account (`main.py:17863`) + BYO Nextcloud env-var creds (by design).
>
> **Truth-up 2026-06-11 (later the same day): Tier 0 is complete.** The 403
> audit log shipped + k3d-verified ([security_event_log.md](features/security_event_log.md)):
> every `access.py` 403 + admin-gate + IDE-proxy denial now writes a
> `security_events` row (with `view_as`/`real_is_admin` enrichment) + a
> WARNING line; read path `GET /api/admin/security-events`; 90d retention
> sweeper. **Nothing on this roadmap precedes the fork decision anymore.**

---

## Verified posture (code-checked 2026-06-10)

The [`multi_tenancy.md`](multi_tenancy.md) tracker was last trued-up
2026-05-18 and has drifted. Reconciliation:

| M1 item | Tracker claims | Verified reality (file:line) | Real status |
|---|---|---|---|
| **A — API data isolation** | shipped | `orchestrator/security/access.py` `require_*` gate family raising 403/404 consistently; `tests/test_endpoint_inventory.py` re-runs `scripts/check_endpoint_auth.py` vs committed `docs/security/endpoint_inventory.txt`, with `# nosec: public` opt-out | ✅ Real and enforced |
| **A — Agent↔orchestrator boundary** | shipped | `X-Internal-Key` enforced in `auth.py:466` + `access.py:713` (`is_internal_request`); gates real endpoints in `main.py` | ✅ Real |
| **D#3 — Workspace egress + tiering** | "new, design pending" / "PR3 deferred" | **Fully shipped:** migration `0016_project_network_tier.sql`; `init.py:1166` `_seed_operator_network_tier`; `container_provisioner.py:689` `_resolve_network_tier`; label stamped at `container_provisioner.py:680` (`srw.io/network-tier`); per-tier policies + fail-closed fallback-deny in `helm/templates/workspace-network-policy.yaml` | ✅ **Done — tracker stale** |
| **C — Cloud per-user credentials** | "THE blocker", pending | **Mount path shipped 2026-06-10**: Keycloak token-exchange impersonation (`auth.type = keycloak_user_impersonation` + `target_user_sub`), user-scoped tokens refreshed at expiry−90s, validated k3d + dev — impersonated token reaches *only* that user's Space, client secret absent from workspace. Residual: personal WebDAV datasource on shared creds (`main.py:17863`); Nextcloud BYO env-var creds by design | ✅ **Mounts done; datasource path residual** |
| **B#1 — Keycloak self-registration** | open | **Superseded + shipped 2026-06-10** as [app-side admission](features/app_side_admission.md): `users.is_approved` (migration `0024`) + login write-through migration + bulk `POST /api/admin/users/approve` + cockpit pending list/toast. S4 (drop `user`-role fallback) pending soak | ✅ Shipped (S1–S3) |
| **B#4 — Cross-user 403 audit log** | open | **Shipped + k3d-verified 2026-06-11**: `security_events` (migration `0025`) written from all 15 `access.py` raise sites via `log_security_event`/`_denied`, plus `_require_admin` (`admin_denied`) + IDE proxy denials; `GET /api/admin/security-events`; retention sweeper; 16 tests | ✅ Shipped |
| **D#1 — Per-user API rate limiting** | pending | Only a per-*job* message rate limit (`main.py:4872`); no per-user API middleware | ❌ Not started |
| **D#2 — Pod ResourceQuota/LimitRange** | pending | Zero `ResourceQuota`/`LimitRange` in the chart | ❌ Not started |
| **E — Self-deletion + data export** | pending | Absent (only `/api/me/active-jobs`) | ❌ Not started |

### Two facts worth internalizing

- **The tenant-network story is finished.** Egress hardening (the wildcard
  `ipBlock … except` on TCP 22/80/443) and the per-tenant tier model
  (`internet-only` default, `home-allowed` operator tier) are live, with a
  fail-closed fallback-deny catching any label/DB drift. That's a whole
  M1.D line item already checkable. See [`workspace_network_isolation.md`](features/workspace_network_isolation.md).
- **The cloud blocker is a seam-fill.** `build_rclone_mount_spec` already
  threads a `CloudMountSubject` through, and `_explicit_user_home_credentials`
  resolves per-user creds — today from env vars (correct for a single
  operator, useless for strangers). Wiring Keycloak-minted OAuth tokens into
  that existing seam is the work, which is why the ~1–2 week estimate is
  credible rather than optimistic.

---

## The strategic fork

The destination changes which gaps actually block you. This is the decision
to make before committing engineering time.

### Fork A — Single-tenant pilot (fastest to first revenue)

One customer, one install, vetted users. The shared service account is
*correct* here. Egress is hardened, API isolation is real, the per-user
cloud blocker does not apply.

- **Remaining work:** prove one supported Helm install + the demo runbook
  end-to-end; turn each failure into a punch-list item.
- **Not security plumbing** — it's reliability and packaging.
- Matches the [2026-06-09 roadmap](strategy/2026-06-09-roadmap-priorities.md)'s
  explicit bet (it parks public multi-tenant SaaS in the parking lot).

### Fork B — Open multi-tenant SaaS (strangers on your infra)

Mindset shift: users will probe, abuse, and accidentally hammer the system.

- **Hard gate:** C (per-user cloud OAuth). Until then every cloud-file read is
  one path-scoping bug from cross-tenant exposure.
- **Plus:** abuse layer (D#1 rate limiting, D#2 pod quotas), account
  lifecycle (E), and billing/metering ([`saas_billing_and_metering.md`](features/saas_billing_and_metering.md)).
- **Realistic timeline:** ~4–6 weeks of hardening before responsibly opening
  signups, plus the billing build.

---

## The plan

Sequenced so the early work serves **both** forks — nothing here is wasted if
the fork decision flips later.

### Tier 0 — do regardless of fork (~1–1.5 days, pure defensive wins)

> Status 2026-06-11: **Tier 0 complete.** #1 ✅ shipped + tested (S1–S3; S4
> after soak). #2 ✅ shipped + k3d-verified
> ([security_event_log.md](features/security_event_log.md)). #3 ✅ done
> (tracker trued up).

1. **App-side admission** (supersedes the original "add `user` to the
   `default-roles-srw` composite" fix — direction changed 2026-06-10, see
   [`app_side_admission.md`](features/app_side_admission.md)). Registration
   already works; the gap is the *admission step*. Move it from Keycloak role
   mappings into a `users.is_approved` DB flag + bulk-approve in the Cockpit
   admin Users page. **Highest leverage item in the whole plan** — until
   admission is sane you *cannot easily validate the shipped M1.A isolation
   surface with real second users*. Unblocks testing for both forks.
   - Verify: register a fresh user on k3d, approve via Cockpit, confirm a
     non-admin user sees only their own jobs.
   - The `verifyEmail` / `email.enabled` dead-end on fresh installs stays an
     orthogonal chart-defaults fix; [[project-keycloak-self-registration-broken]]
     has role IDs + workarounds; deeper follow-on is
     [`cockpit_owned_auth_ui.md`](features/cockpit_owned_auth_ui.md).
2. **Cross-user 403 audit log.** Emit a structured event at the `access.py`
   raise sites. ~3h, one localized change, gives probe-detection signal.
3. **Truth-up [`multi_tenancy.md`](multi_tenancy.md).** Mark D#3 shipped;
   reclassify C as "seam exists, needs OAuth backing." Stops re-derivation.

### Tier 1 — if Fork A (pilot)

Audit the one supported Helm install end-to-end against the demo runbook in
[`2026-06-09-roadmap-priorities.md`](strategy/2026-06-09-roadmap-priorities.md)
§"Define And Prove One Pilot Demo Path." Each failure becomes a `Now` bug or a
deliberately accepted caveat. This is reliability/packaging, not new security.

### Tier 2 — if Fork B (SaaS), in this order

1. **Per-user cloud OAuth** — ✅ **mount path shipped 2026-06-10** (Keycloak
   token-exchange impersonation through the `CloudMountSubject` seam, exactly
   as prescribed here; validated k3d + dev). **Remaining residual:** move the
   personal WebDAV *datasource* (`main.py:17863`) off the shared service
   account onto the same impersonation model (or retire it in favor of the
   mount path), and decide whether BYO Nextcloud's explicit-credential v1
   stance is acceptable for SaaS.
2. **Per-user API rate-limiting middleware** (Redis-backed, per-user not
   per-IP so paid-tier hooks attach later).
3. **Pod `ResourceQuota` + `LimitRange`** in the chart; concurrent-jobs-per-user
   cap at dispatch.
4. **Account lifecycle** — `DELETE /api/me` (cascade matrix is the hard part)
   + `POST /api/me/export`.
5. **Billing + usage metering** ([`saas_billing_and_metering.md`](features/saas_billing_and_metering.md),
   [`observability_and_quotas.md`](features/observability_and_quotas.md)).

---

## Recommendation

~~Do **Tier 0 today** independent of the fork.~~ **Tier 0 is complete as of
2026-06-11** — admission (2026-06-10), the 403 audit log (2026-06-11), and the
tracker truth-up are all shipped. Nothing on this roadmap precedes the fork
decision anymore.

Then make the fork decision deliberately. For near-term cash, Fork A (pilot)
is faster and the strategic roadmap already votes for it; Fork B (open SaaS)
is the right destination — and its hardening arc shrank materially on
2026-06-10 when the per-user mount path shipped: the remaining Fork B work is
the WebDAV-datasource residual, rate limiting, pod quotas, account lifecycle,
and billing.

---

## Readiness gates (carried from [`multi_tenancy.md`](multi_tenancy.md), status corrected)

**Fork A — pilot ready:**

- [x] API data isolation (M1.A)
- [x] Workspace egress hardening + tiering (M1.D#3)
- [x] Admission (app-side, shipped 2026-06-10; S4 fallback-drop after soak)
- [x] Cross-user 403 audit log (shipped + k3d-verified 2026-06-11)
- [ ] One Helm install + demo runbook proven end-to-end ← **the only Fork A gate left**

**Fork B — open-signup ready (all of Fork A, plus):**

- [x] Cloud per-user — workspace mount path (token-exchange impersonation, 2026-06-10)
- [ ] Cloud per-user — WebDAV datasource residual off the shared service account
- [ ] Per-user rate limiting (M1.D#1)
- [ ] Pod quotas (M1.D#2)
- [ ] Self-deletion + data export (M1.E)
- [ ] Billing + usage metering
