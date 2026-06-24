# Orchestrator M0 (Active-Passive Failover) — Verification Record

**Feature:** `docs/features/orchestrator_ha_scaling.md` — Milestone M0 / Phase 0.
**Spec / plan:** `docs/superpowers/specs/2026-06-24-orchestrator-m0-active-passive-design.md`, `docs/superpowers/plans/2026-06-24-orchestrator-m0-active-passive.md`.
**Procedure (how to run the chaos test):** `docs/operations/orchestrator_failover.md`.

**Status (2026-06-24): Locally verified on k3d. Live chaos test on the shared cluster is PENDING — run during a quiet window (overnight, with no one else mid-test on the cluster).**

## Verified locally on k3d (2026-06-24)

Single-node k3d cluster `srw`, Helm-deployed via Tilt (`fullnameOverride: srw`).

| Mechanic | Result |
|---|---|
| Chart deploys via Helm (real values) | **PASS** — release installs clean; `helm lint` + `helm template` green across all `helm/ci/*` values files. |
| `startupProbe` prevents slow-start crash-loop | **PASS** — orchestrator reached Ready through a ~5-min full-stack cold start with **0 restarts** (init-containers gate on the DBs, then the startupProbe gates the orchestrator container). This is the exact failure mode M0 fixes. |
| `preStop` graceful drain | **PASS** — pod termination took **18s** (15s `preStop` sleep + ~3s app shutdown). A near-instant termination would indicate the hook didn't run. |
| Tuned liveness / readiness | **PASS** — orchestrator boots to Ready; ~15s recovery on a warm pod delete. |
| PodDisruptionBudget permits drain | **PASS** — `srw-orchestrator-pdb` reports `minAvailable: 0`, **`ALLOWED DISRUPTIONS: 1`** (a voluntary eviction / drain is permitted, not blocked). |

Incidental finding (not a chart defect): Tilt did not auto-apply the brand-new `helm/templates/orchestrator/pdb.yaml` until a full re-render — the chart renders the PDB correctly under the real values. A Tilt dev-loop quirk only.

## Still owed — the live (dev) chaos test

M0 is pure Kubernetes pod-spec mechanics, and these aspects can only be exercised on the multi-node, live-traffic cluster (not single-node k3d):

1. **Multi-node node-drain reschedule** — single-node k3d can evict but has nowhere to reschedule. The full path — `kubectl drain` proceeds (PDB does not block it) → orchestrator reschedules onto another node → passes `startupProbe` → Ready — needs ≥2 nodes.
2. **In-flight-request drain under real traffic** — confirm the `preStop` window actually lets concurrent REST requests *finish* (locally we only proved the window exists, via the 18s termination). Needs realistic load.
3. **Full-stack failover recovery** — with live agents/sessions: a mid-dispatch job ends up `paused` then re-dispatched exactly once; a persistent session reattaches via `thread_events` replay; an open sudo prompt survives.
4. **Realistic downtime numbers** in the prod-like environment.

### When & prerequisites

- **When:** overnight / a low-usage window, coordinated so no one is mid-test on the cluster. The test deletes the orchestrator pod and drains a node — brief disruption to anyone using dev.
- **Prerequisite:** M0 must be deployed to the target cluster first. As of this record the M0 commits are **local-only on `develop` (unpushed)** — deploy via a `develop` push (Fleet sync) or a manual `helm upgrade` on the `main` context.
- **Procedure & pass criteria:** follow `docs/operations/orchestrator_failover.md` (the chaos test + the drain test).

## Scope guard

M0 keeps `replicas: 1` (active-passive: survive a pod's own death cleanly). Running multiple replicas (`replicas: 2`) is **not** part of M0 and is unsafe until M1 (leader election) lands — do **not** raise `orchestrator.replicas` as part of this test.
