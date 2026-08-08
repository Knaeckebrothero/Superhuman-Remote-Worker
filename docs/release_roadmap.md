# Release Roadmap — Alpha → Beta → SaaS

**Status:** Active — living checklist. Check boxes here; execution detail lives in the linked docs.
**Created:** 2026-08-07
**Owner:** release

## Premise

The repo has been **public under FSL-1.1-ALv2 since 2026-06-23**. Making code
public is not a release — a release is the moment we ask people to use it.
This doc defines what "asking people" is gated on, in three milestones:

- **Alpha** — quiet, recruited self-hosters. Gate: the first-run experience.
- **Beta** — the one loud public launch. Gate: data integrity + upgrade path proven by alpha.
- **SaaS** — paid hosting. **Runs in parallel from today**, not after beta; its gates are ops and business maturity, and the beta launch is its demand generator.

Milestones close on their **exit gates**, not on dates or internal quality
feel. The feature-freeze holds throughout: everything below is hardening,
docs, packaging, or business plumbing — not new product surface.

## Current state (2026-08-07)

- Repo public, FSL-1.1-ALv2, positioned as **Fair Source** (never bare "open source" — site copy already aligned).
- Tags up to `v0.0.23` are deploy cuts: **zero GitHub Releases, no changelog, no release notes.** To a visitor the repo looks unmaintained.
- `develop` == `origin/develop` (all work pushed). Working tree carries only in-flight metering/pricing doc drafts.
- Two pilot installs + the dev cluster. `prod-private` is pinned `v0.0.23` and down pending the MongoDB restore.
- Install path: [`deployment_readiness.md`](deployment_readiness.md) Phase 1 (chart CI gates) **complete**; Phases 2–6 open. Helm is the single supported path.
- `SECURITY.md` exists (vuln reporting) but claims "no versioned releases; `main` is supported" — becomes false at the first alpha cut — and has no deployment threat model yet.

---

## Milestone 1 — Alpha (quiet, recruited)

**What it is:** hand-recruited self-hosters running the container tier
internally. No announcement, no HN. These users tolerate rough software;
they do not tolerate a broken install or lost data.

**Exit gate (milestone closed when):**
≥10 external installs (target 30); several reached a working session **and**
job from the docs alone, unassisted; the issue → fix → release loop has run;
at least one outsider upgraded across two alpha releases without damage.

**Target:** ~2 weeks prep → recruiting starts ~2026-08-21.

### A1 — Install path (alpha-critical slice of [`deployment_readiness.md`](deployment_readiness.md))

- [x] Phase 1 — chart correctness gates in CI (render matrix, kubeconform, schema)
- [ ] Phase 2 — install test on throwaway infrastructure
- [ ] Phase 3 — documentation for the single path (Helm, k3s mini-PC → multi-node)
- [ ] `values.example.yaml` matches the real secret contract (known drift: `POSTGRES_USER`, `VECTOR_*`, `KC_CLIENT_SECRET`, …)
- [ ] Full first-run smoke on hardware that isn't ours: clone → install → login → one session + one job, using only the docs

### A2 — Release mechanics

- [ ] Cut the **first tagged GitHub Release with real notes**, labeled alpha (`v0.1.0-alpha.1`, or stay on `v0.0.x` — the notes matter, the number doesn't)
- [ ] Start `CHANGELOG.md`; every cut from now on gets notes
- [ ] Per-release **upgrade notes** (breaking values changes, migration callouts)
- [ ] Fix `SECURITY.md` deployment-model section (it predates versioned releases)

### A3 — Honest scope

- [ ] Threat-model section in `SECURITY.md`: intended deployment = **single-tenant, trusted operators, private network/VPN**; multi-tenant authorization hardening in progress
- [ ] Known-issues section in README, fed from `docs/issues/`
- [ ] VM tier labeled **experimental**; supported alpha path is the container tier (see [`issues/vm_reliability_assessment.md`](issues/vm_reliability_assessment.md))
- [ ] Fair Source wording pass over README, site, release notes. The hook: *"free to self-host; each release becomes Apache-2.0 open source two years after publication."*

### A4 — Community plumbing

- [ ] Issue templates (`.github/ISSUE_TEMPLATE/`: bug, install failure) — none exist today
- [ ] Enable GitHub Discussions (or a small Discord) as the landing spot
- [ ] **Hosted-offering waitlist link** in README + site — start collecting SaaS demand now
- [ ] Recruiting plan + outreach: r/selfhosted, homelab Discords, k8s/self-hosting communities, the pilots' networks. Personally onboard the first ~10.

### A5 — Field-critical fixes (what a fresh install hits first)

- [ ] Postgres checkpoint-blob growth: confirm keep-N retention is in the shipped images; document disk sizing (has recurred internally)
- [ ] Orchestrator image ships all agent deps (neo4j/pgvector import gap → silent vector-store no-op)
- [ ] Triage [`issues/BACKLOG.md`](issues/BACKLOG.md) for anything else first-run-lethal

---

## Milestone 2 — Beta (the public launch)

**What it is:** the loud one — Show HN, blog post, the works. We get exactly
one of these; it is not spent on alpha-quality first-run experience.

**Entry gate (launch when):** alpha exit gate met; every install-path bug
alpha surfaced is fixed; **no known data-loss bug in the supported path**;
authorization is deny-by-default *or* the documented threat model honestly
covers the gap; upgrade path proven.

**Target:** ~1 month after alpha starts (mid-September) — reality-gated, not date-gated.

### B1 — Data integrity (ranks above security for self-hoster trust)

- [ ] [`issues/session_restore_drops_repo_checkouts.md`](issues/session_restore_drops_repo_checkouts.md) — pod recycle must not lose work
- [ ] Session "shared folder" created + shared but never mounted (orphan rclone)
- [ ] Workspace snapshot capture/restore holes ([`issues/snapshot_capture_ssh_failure.md`](issues/snapshot_capture_ssh_failure.md), [`issues/snapshot_restore_dead_for_jobs.md`](issues/snapshot_restore_dead_for_jobs.md)) — fix, or explicitly cut them from supported scope
- [ ] Disk-growth ops guidance validated in the field during alpha

### B2 — Security hardening

- [ ] Grants enforcement slice 2: **deny-by-default** (grandfathering plan exists) — enforced, not just documented
- [ ] Expert-write gate holes closed
- [ ] Hardening pass over shipped chart defaults against [`security_checklist.md`](security_checklist.md)

### B3 — Reliability

- [ ] Session-zombie agents drain on deploy/version upgrade (today they survive deploys until manual pod delete)
- [ ] Workspace reaper lifecycle landed (leaked pods + DB orphans)
- [ ] Delegation vs reaper race (parent reaped while critic runs)

### B4 — Install path completion ([`deployment_readiness.md`](deployment_readiness.md) Phases 4–6)

- [ ] Phase 4 — remove legacy deployment paths (compose ×3, raw manifests, kustomize)
- [ ] Phase 5 — documentation cleanup
- [ ] Phase 6 — release-validation gate exercised for every beta cut

### B5 — Launch assets

- [ ] Demo video/GIFs: a session and a job, end to end
- [ ] README + site polish. Positioning: **self-hosted, sovereign, bring-your-own-model** — against the labs' cloud-tethered workspace products
- [ ] Launch post drafted, license framing rehearsed (Fair Source; the two-year Apache-2.0 timer is the honest "open source" claim)

**Exit gate (milestone closed when):** launch executed; strangers (not
recruits) are installing; the issue queue is sustainable next to other work;
no known data-loss bug in the supported path.

---

## Milestone 3 — SaaS (parallel track)

**What it is:** paid managed hosting. **Not sequenced after beta** — the
business and ops work runs alongside alpha/beta, and the beta launch feeds
the waitlist. Product-wise the repo is the same; what's gated here is the
ability to take money responsibly.

**Exit gate:** a stranger can pay and receive an isolated, metered, working
workspace with no founder in the loop — and we can legally invoice them.

### S1 — Business & legal (DE)

- [ ] Ability to invoice at all: Steuernummer via ELSTER (Einzelunternehmer/Freiberufler) — prerequisite for *pilot* revenue too, not just SaaS
- [ ] Kleinunternehmer vs VAT-waiver decision (input-VAT reclaim on infra spend)
- [ ] ToS, privacy policy (GDPR incl. AVV for B2B), Impressum
- [ ] Payment provider wired (Stripe or EU alternative)
- [ ] Pricing finalized — [`features/cloud_equivalent_usage_pricing.md`](features/cloud_equivalent_usage_pricing.md) (draft in flight)
- Note: if EXIST-Gründerstipendium is still on the table, **do not incorporate before applying** — founding first disqualifies.

### S2 — Metering → billing

- [ ] Infrastructure metering + usage dashboard landed ([`features/infrastructure_resource_metering.md`](features/infrastructure_resource_metering.md), [`features/usage_dashboard.md`](features/usage_dashboard.md) — in flight)
- [ ] Usage-ledger attribution gaps closed (per-user/per-job rollups billing can trust)
- [ ] Per-tenant quotas/limits enforced ([`features/observability_and_quotas.md`](features/observability_and_quotas.md))

### S3 — Multi-tenant hardening (superset of B2)

- [ ] Deny-by-default grants mandatory, no grandfathering
- [ ] Tenant isolation review: namespaces, NetworkPolicies, storage, per-tenant secrets
- [ ] Abuse controls: resource caps, egress policy, runaway-agent cutoffs

### S4 — Ops maturity

- [ ] `prod-private` restored (MongoDB outage) and unpinned from `v0.0.23`
- [ ] Backup/restore + DR **tested by actually restoring**, for hosted customer data
- [ ] Monitoring/alerting + a public status page
- [ ] Hosting decision for paid tenants: homelab-behind-tunnel vs cloud provider — the cost model feeds pricing

### S5 — Product surface

- [ ] Self-serve signup: Keycloak automation + workspace provisioning
- [ ] Hosted onboarding flow + docs
- [ ] Support channel with stated response expectations

---

## Sequencing

```
Aug 2026                 Sep 2026                  Oct 2026 →
|— Alpha prep (~2 wks) —|
          |—— Alpha: recruited installs ——|
                              gates met → |— Beta launch —|— open beta —→
|—————— SaaS track: legal · metering · multi-tenant · ops ——————|→ first paid tenants
```

Alpha → Beta is sequential. The SaaS track starts now and converges when its
own gates pass — likely shortly after beta, but nothing about beta blocks
starting S1 today.

## Explicitly out of scope until after beta

New feature work (freeze holds), OneDrive/Google Drive sync, product
rename/rebrand, multi-region, HA beyond what exists. If it isn't on a gate
above, it waits.

## Related documents

- [`deployment_readiness.md`](deployment_readiness.md) — install-path phases (the A1/B4 detail)
- [`customer_install_guide.md`](customer_install_guide.md) — installer-facing guide
- [`security_checklist.md`](security_checklist.md) — container/agent hardening reference
- [`issues/BACKLOG.md`](issues/BACKLOG.md) + `docs/issues/` — open defects feeding A5/B1–B3
- [`issues/vm_reliability_assessment.md`](issues/vm_reliability_assessment.md) — why the VM tier is experimental
- `features/` metering & pricing docs — the S2 detail
