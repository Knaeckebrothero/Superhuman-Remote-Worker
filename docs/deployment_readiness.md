# Deployment Readiness for the Open-Source Release

**Status:** Active — checklist tracking
**Last updated:** 2026-06-15
**Owner:** deployment / release

## Goal

Ship **one** install path for the open-source release: the Helm chart, tested
and documented from a single-node k3s box (a mini PC) up to a large multi-node
cluster. The chart already runs the full stack in under ~10 GiB of RAM, so the
hardware story is real today — what is missing is the *testing*, *documentation*,
and *cleanup* that make it the credible and only supported path.

We are **release-ready when every box below is checked.** This document is the
umbrella; the detailed execution plans live in their own docs and are linked per
phase. Where a phase already has a plan (e.g. the Docker Compose deprecation),
this doc does not duplicate it — it sequences it.

## Why now

The repo carries the full archaeology of its own deployment history: bare-metal
local execution → Docker → Docker Compose (×3 files) → raw Kubernetes manifests
→ Kustomize overlay → Helm chart, plus k3d + Tilt for the dev loop. For an
open-source release this is a liability: every extra path is a second topology to
test, document, and keep from drifting. Helm is the settled answer. Everything
that is not "Helm chart" or "k3d/Tilt dev loop" is now legacy to retire or
internal-ops to fence off.

## Scope

**In scope (the OSS install story):**

- `helm/` — the product chart. The single supported install path.
- `helm/ci/` — values permutations used for render/lint/install tests.
- Root `README.md`, `helm/README.md`, and a new single-node k3s install guide.
- The k3d + Tilt dev loop (stays — it is how contributors develop against the
  chart; documented, not removed).

**Stays, but explicitly fenced as internal ops (not part of the OSS install):**

- `deployment/` — Fleet GitOps overlay for *our* clusters (values, `fleet.yaml`,
  `deploy.sh`). This is how we operate our own dev/prod; it is not how a customer
  installs. Flag it as internal; a later open-core split may move it out of the
  public repo entirely.
- `deployment-vms/` and `helm-vm-cluster/` — VM-cluster Fleet bundles + the
  separate VM chart. Advanced, optional, NATS/KubeVirt territory. Gets
  lint+render coverage only; **not** a release blocker.

**Out of scope / non-goals:**

- Docker Compose as a supported tier — being removed (Phase 4), not maintained.
- Full end-to-end automation in CI (scripted login + job against a stub LLM).
  Highest confidence but most flake; revisit after the release if the install
  test proves insufficient.
- Bare-metal CoreOS / Ignition install automation — separate track, user-driven.

## Current state snapshot

| Area | Today | Target |
|---|---|---|
| Chart lint in CI | `helm lint` ×2 (`test-values.yaml`, `customer-external-values.yaml`) | + render matrix, kubeconform, values schema |
| Install test in CI | none | throwaway k3d job: install + wait + health, incl. upgrade-from-published |
| `values.schema.json` | absent | present; bad values fail at `helm install`, not pod-crash |
| `helm test` hooks | none | health-probe hooks (CI gate **and** user `helm test srw`) |
| Single-node install doc | none (README has eval + k3d-dev only) | dedicated mini-PC k3s guide |
| Docker Compose | 3 root files + `DockerProvisioner` + `agent.py --loop`, docs say "supported" | removed (per existing plan) |
| Strategy docs | `deployment.md` claims "three tiers actively maintained" | reconciled to single-path reality |
| Release validation | ad-hoc | documented manual gate on real k3s hardware |

---

## Phase 1 — Chart correctness gates in CI

**Goal:** Catch template, schema, and values-wiring regressions on every PR that
touches `helm/`, before anything reaches a cluster. Cheap, fast, no cluster.

- [ ] **Render matrix.** `helm template` across the meaningful permutations:
  the three secrets modes (ESO / pre-existing Secret / chart-created), internal
  vs. external for each database, hostname overrides, `vmController` on/off,
  `mcp`/`neo4j`/`opencloud` toggles. Build out `helm/ci/*-values.yaml` as the
  matrix inputs (extends the two files already there).
- [ ] **`kubeconform`** over the rendered output, pinned to the K8s versions we
  claim to support (chart says 1.28+). Validates against real API schemas; needs
  CRD schemas for KubeVirt/ESO/cert-manager where those resources render.
- [ ] **`values.schema.json`** for the chart. Required keys (`global.domain`,
  `license.acceptTerms`, a coherent secrets mode) fail at install time with a
  clear message instead of a downstream crash-loop. Doubles as install-time UX.
- [ ] Wire all three into the existing `lint` job in `develop.yml` / `main.yml`,
  gated on `helm/` changes via the existing `changes` job.
- [ ] *(Stretch)* targeted `helm-unittest` only if a specific template keeps
  regressing — not adopted wholesale.

**Acceptance criteria:**

- A PR that breaks any matrix permutation's render or schema fails CI.
- `helm install` with a missing required value fails fast citing the value.
- No new always-on cluster cost; jobs run only when `helm/` changed.

---

## Phase 2 — Install test on throwaway infrastructure

**Goal:** Prove the chart actually *comes up*, not just that it renders. This is
the layer that would have caught the April fresh-deploy cascade (postgres init
skipped → keycloak role missing → init-container deadlock) — invisible to every
static check. See `docs/issues/helm_fresh_deploy_issues.md` and
`docs/issues/local_e2e_testing.md`.

- [ ] **CI install profile.** A trimmed `helm/ci/install-values.yaml` that fits
  the free 16 GiB public-repo runners (core path only: postgres + vector +
  keycloak + gitea + orchestrator + cockpit; neo4j/opencloud/mongo trimmed or
  minimally sized). Document what it deliberately omits.
- [ ] **k3d install job** in CI: create cluster → `helm install` the profile →
  `kubectl wait` for core Deployments/StatefulSets to be Ready → fail with pod
  logs on timeout. k3d is the existing dev stack, so no new tooling.
- [ ] **Upgrade test.** Install the last *published* OCI chart, then `helm
  upgrade` to the PR's chart — most real-world chart breakage is upgrades, not
  fresh installs.
- [ ] **`helm test` hooks.** Pods annotated `helm.sh/hook: test` that probe
  orchestrator / cockpit / keycloak health endpoints from inside the cluster.
  Run as the CI gate after install; also shipped to users as `helm test srw`.
- [ ] Gate the whole job on `helm/` changes; keep it off the hot path for
  code-only PRs.

**Acceptance criteria:**

- Fresh `helm install` of the CI profile reaches all core pods Ready in CI.
- `helm upgrade` from the previously published chart succeeds in CI.
- `helm test srw` passes against the CI install and is documented for users.
- A reintroduction of the April cascade (or similar dependency deadlock) is
  caught by CI rather than by a human at deploy time.

---

## Phase 3 — Documentation for the single path

**Goal:** A reader who has never seen the repo can get from "blank machine" to
"working SRW" by following one document, and an operator can run, upgrade, and
recover the chart from the chart docs alone.

- [ ] **Restructure root `README.md` helm-first.** Install-path picker becomes:
  (a) one-command evaluation, (b) single-node k3s (mini PC), (c) production /
  bring-your-own. Remove the Docker Compose path (coordinated with Phase 4). Keep
  k3d + Tilt as the **dev-loop** section, clearly labeled "for contributors."
- [ ] **New single-node k3s install guide** (the mini-PC persona): blank OS →
  install k3s → ingress/TLS choice → `helm install` → verify with `helm test`.
  This is the headline OSS story; reconcile/absorb the existing
  `docs/customer_install_guide.md` (prototype guide) rather than leaving two.
- [ ] **Grow `helm/README.md`**: an Upgrade section, a Backup/Restore section
  (lead with the `APP_ENCRYPTION_KEY` "lose it and all stored credentials are
  unrecoverable" warning), and a resource-sizing table (mini-PC floor →
  recommended → large-cluster).
- [ ] Document the supported toggle surface coherently: which optional
  components exist, what each needs, and the minimum viable footprint.

**Acceptance criteria:**

- The single-node guide, followed literally on a clean machine, yields a working
  install (validated by Phase 6).
- README presents exactly one supported install path (+ the dev loop), no
  Compose references.
- `helm/README.md` covers install, upgrade, backup/restore, and sizing.

---

## Phase 4 — Remove legacy deployment paths

**Gated on Phases 1–2 being green** — the safety net goes in before the old
paths come out.

- [ ] **Execute the Docker Compose deprecation** per the existing 5-PR plan in
  `docs/issues/deprecate_docker_compose_stack.md`: delete the three root
  `docker-compose*.yaml`, remove `orchestrator/services/docker_provisioner.py`
  and its ~17 dispatch call sites, drop `agent.py --loop` + `AGENT_LOOP`, ship
  `scripts/build-local-images.sh` as the local-rebuild replacement. (That doc
  holds the per-PR detail and its own acceptance criteria — follow it.)
- [ ] **Retire the pre-Helm Kubernetes artifacts.** `deprecated_deployment-local/`
  (old Kustomize single-cluster overlay) and `deployment/legacy/` (numbered raw
  manifests) are superseded by the chart. Confirm nothing live references them,
  then delete (or move under a clearly-marked `legacy/` if we want git-history
  convenience — deletion preferred for an OSS repo).
- [ ] **Audit `docker/` for orphaned build context.** Distinguish Dockerfiles the
  chart still builds (`Dockerfile.agent`, `.orchestrator`, `.cockpit`, `.mcp`,
  `.workspace`, `agent-vm-base/`) from Compose-only or dead ones (e.g. `.dev`
  variants, `nextcloud/` now that OpenCloud is default, `vpn/` after the VPN
  sidecar removal). Remove what no build path references; keep what the chart
  needs.
- [ ] **Sweep stray root files.** `headscale-bootstrap.sh` and similar one-offs at
  repo root — relocate under `scripts/`/`deployment/` or remove. Each verified
  dead/relocated, not blind-deleted.

**Acceptance criteria:**

- No `docker-compose*.yaml` at repo root; the Compose-plan acceptance criteria
  (its own checklist) all pass.
- No references to `docker_provisioner` / `WORKSPACE_HOSTS` / `AGENT_LOOP` in
  `orchestrator/`, `src/`, `agent.py`.
- `deprecated_deployment-local/` and `deployment/legacy/` removed (or fenced).
- Every remaining file under `docker/` is referenced by a live build; orphans
  gone.
- Full `pytest` + `ruff check` + `ruff format --check` green; chart still
  installs (Phase 2 job).

---

## Phase 5 — Documentation cleanup

**Goal:** No doc contradicts the single-path reality. A reader can't find a page
telling them Compose is "supported" or that there are "three actively maintained
tiers."

- [ ] **`docs/deployment.md`** — currently opens with "three deployment tiers,
  all actively maintained" and a reversed-decision note keeping Compose. Rewrite
  to: Helm chart is the supported install; `deployment/` is internal Fleet ops;
  Compose removed. This becomes the canonical strategy doc.
- [ ] **`docs/docker_compose_mode.md`** (833 lines) — delete, or move under
  `docs/legacy/` with a top banner pointing at the k3s/Helm path.
- [ ] **Mark resolved issue docs historical.** `docs/issues/helm_fresh_deploy_issues.md`,
  `deployment_separation_of_concerns.md`,
  `ci_migration_lint_bypassed_by_deploy.md`, `local_e2e_testing.md` — add a
  resolution/superseded banner where the work is done or folded into Phases 1–2,
  so they read as history, not open work.
- [ ] **Reconcile `deployment_checklist.md` / `deployment_roadmap.md`** — fold
  anything still live into this readiness doc; banner the rest as historical.
- [ ] **Update `CLAUDE.md` / `AGENTS.md`** deployment sections: drop Compose/
  `--loop` mentions; point at Helm + k3d/Tilt as the only narratives.

**Acceptance criteria:**

- `grep -ri "docker compose\|docker-compose" docs/ README.md` returns only
  historical/banner-marked hits.
- No doc claims Compose is supported or lists it as an install option.
- `deployment.md` describes exactly the post-cleanup topology.

---

## Phase 6 — Release validation gate (recurring, manual)

**Goal:** Validate what CI structurally cannot — the published artifact, real
ingress/TLS, behavior on modest hardware, and whether the install docs are
actually followable. Run before each release/RC.

- [ ] **Real-hardware install.** Fresh k3s on the dedicated old PC
  (4-core / 32 GiB), installing the **published** RC chart from the OCI registry
  by following the single-node guide *verbatim* — deviations are doc bugs to fix,
  not steps to improvise around.
- [ ] **Smoke checklist.** Walk the README smoke test (Keycloak login → new
  session → new job → SSO to git/cloud) plus `helm test srw`.
- [ ] **Document the procedure** so it is repeatable by anyone, not tribal
  knowledge.

**Acceptance criteria:**

- The published chart installs clean on the k3s box strictly via the docs.
- Smoke checklist + `helm test` pass.
- Any doc gap found is fixed before the release is cut.

---

## Dependency ordering

```
Phase 1 (static gates) ──┐
                         ├──> Phase 4 (remove legacy)  ──> Phase 6 (release gate)
Phase 2 (install test) ──┘            │
                                      └── Phase 5 (doc cleanup, rides with 4)
Phase 3 (docs) — parallel to 1/2, feeds Phase 6
```

- 1 and 2 land first and in parallel; they are the safety net.
- 3 proceeds in parallel; the single-node guide is needed for Phase 6.
- 4 is gated on 1+2 green. 5 rides along with 4 (code + its docs together).
- 6 is the recurring gate, exercised once 3 produces the guide and 4/5 settle.

## Release-ready definition

The open-source deployment story is ready when:

- Every acceptance box in Phases 1–5 is checked, and
- Phase 6 has been run green at least once against a published RC chart on real
  hardware, and
- The repo presents exactly one supported install path (Helm) with a working
  single-node guide, no Compose, and no doc contradicting that.

## Related documents

- `docs/issues/deprecate_docker_compose_stack.md` — the detailed Phase 4 plan.
- `docs/deployment.md` — strategy doc to become canonical in Phase 5.
- `docs/docker_compose_mode.md` — to retire in Phase 5.
- `docs/issues/helm_fresh_deploy_issues.md` — the failure modes Phase 2 must
  catch.
- `docs/issues/local_e2e_testing.md` — prior proposal for local/CI e2e; Phase 2
  is its retargeting at the chart.
- `docs/customer_install_guide.md` — prototype guide to absorb into the Phase 3
  single-node guide.
- `helm/README.md` — chart docs to extend in Phase 3.
- `docs/features/agent_open_source_split.md` — the broader open-core split this
  readiness work feeds into.
