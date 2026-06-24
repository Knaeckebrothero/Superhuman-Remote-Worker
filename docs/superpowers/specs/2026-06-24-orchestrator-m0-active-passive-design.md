# Orchestrator M0 — Active-Passive Failover Hardening — Design Spec

**Status:** Implemented & locally verified on k3d (2026-06-24). All chart/doc changes committed on `develop` (unpushed). Live multi-node + real-traffic chaos test deferred — see `docs/tests/orchestrator_m0_failover_verification.md`.
**Parent:** `docs/features/orchestrator_ha_scaling.md` — Milestone **M0** / **Phase 0** (Track 1). This spec is the implementation-of-record for that milestone.
**Goal:** Make a single orchestrator pod's death (eviction, OOM, node drain, image roll) a fast, bounded, predictable failover instead of today's multi-minute, connection-severing blackout — **without** making the orchestrator multi-replica. Stays `replicas: 1`.
**Scope:** Config-only. Helm chart changes plus an operations runbook. **No application code.**

---

## Why

The orchestrator is structurally a stateless web service and the system already recovers from its death without data loss (heartbeat-driven offline detection → orphan auto-pause → re-dispatch; persistent sessions reattach via `thread_events` SSE replay; migrations are concurrency-safe). What's missing is making that failover *quick and verified* rather than incidental:

- Today there is **no `preStop` hook, no `terminationGracePeriodSeconds`** — on SIGTERM, in-flight requests are cut rather than drained.
- The probes are bare (no `timeoutSeconds`/`failureThreshold`/`startupProbe`). A populated-DB migration that exceeds the liveness budget (~60s) can **crash-loop** the pod on startup.
- There is **no orchestrator PodDisruptionBudget** — a `kubectl drain` has no workload-aware guard (this is the shape of the 2026-05-16 node-pull incident).

M0 is the cheapest reliability win (config + testing, no new primitives) and de-risks M1: once drain + PDB + recovery are solid, going multi-replica is mostly a config flip.

---

## Changes

All in `helm/templates/orchestrator/deployment.yaml` unless noted. Confirmed prerequisites: base image is `python:3.11-slim` (has `sh`+`sleep`, so the `preStop exec` works); `helm 3.19` is available for lint/template validation.

### 1. Graceful drain on termination

Pod-level (under `template.spec`, alongside `serviceAccountName`):

```yaml
terminationGracePeriodSeconds: {{ .Values.orchestrator.terminationGracePeriodSeconds }}
```

Container-level (`orchestrator` container, near the probes):

```yaml
lifecycle:
  preStop:
    exec:
      command: ["sh", "-c", "sleep {{ .Values.orchestrator.preStopDrainSeconds }}"]
```

On SIGTERM the pod is already removed from Service endpoints; the `preStop` sleep holds the container alive while that removal propagates (kube-proxy / LB), so in-flight REST finishes. `terminationGracePeriodSeconds` must exceed `preStopDrainSeconds` + lifespan shutdown.

**Defaults: `preStopDrainSeconds: 15`, `terminationGracePeriodSeconds: 60`.** Rationale: 15s is ample for endpoint propagation + short REST calls; long-lived SSE streams drop regardless and are already recovered by `thread_events` replay, so there's no need for the doc's offhand 30s. Both are values-tunable — raise `preStopDrainSeconds` if chaos testing shows truncated requests.

### 2. `startupProbe` for slow migrations

New probe on the container:

```yaml
startupProbe:
  httpGet:
    path: /api/health
    port: 8085
  periodSeconds: 5
  failureThreshold: 30   # ~150s budget for migrations on a populated DB
  timeoutSeconds: 3
```

uvicorn does not accept connections until lifespan startup (migrations included) completes, so during a slow migration the probe gets connection-refused; the generous `failureThreshold` absorbs that window. Liveness/readiness do not run until the startupProbe first succeeds — this removes the crash-loop-on-slow-migration failure mode.

### 3. Tighten liveness / readiness

```yaml
livenessProbe:
  httpGet: { path: /api/health, port: 8085 }
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 3
  # initialDelaySeconds dropped — startupProbe now gates liveness
readinessProbe:
  httpGet: { path: /api/health, port: 8085 }
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 2      # sick pod leaves endpoints in ~10s
  # initialDelaySeconds dropped — startupProbe gates readiness
```

DB-aware health (parent doc Open-Q#4) is explicitly **out** — that's M4.

### 4. Orchestrator PodDisruptionBudget

New file `helm/templates/orchestrator/pdb.yaml`, mirroring `helm/templates/agent/pdb.yaml`:

```yaml
{{- if .Values.orchestrator.pdb.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "srw.fullname" . }}-orchestrator-pdb
  labels:
    {{- include "srw.componentLabels" (dict "context" . "component" "orchestrator") | nindent 4 }}
spec:
  minAvailable: {{ .Values.orchestrator.pdb.minAvailable }}
  selector:
    matchLabels:
      {{- include "srw.componentSelectorLabels" (dict "context" . "component" "orchestrator") | nindent 6 }}
{{- end }}
```

Default `enabled: true`, `minAvailable: 0`. At `replicas: 1`, `minAvailable: 0` is **required** — `minAvailable: 1` on a single replica blocks all voluntary evictions, i.e. blocks `kubectl drain` (the exact 2026-05-16 failure). It is a near-no-op guard today but makes the chart Track-2-ready; M1 flips `minAvailable` to `1` alongside `replicas: 2`.

### 5. Values, schema, runbook

- `helm/values.yaml` — add under `orchestrator:`:
  ```yaml
  preStopDrainSeconds: 15
  terminationGracePeriodSeconds: 60
  pdb:
    enabled: true
    minAvailable: 0   # replicas:1 → must be 0; flip to 1 at replicas>=2 (M1)
  ```
- `helm/values.example.yaml` — **no change made (as built).** It is a deliberately minimal customer overlay (external services + sizing only, no `orchestrator:` block); the HA defaults in `values.yaml` are production-appropriate, so adding HA keys there would contradict its design (YAGNI).
- `helm/values.schema.json` — **no change needed (as built).** Confirmed during implementation: the schema is intentionally permissive (`additionalProperties` allowed; it does not define `orchestrator`), so the new keys validate without schema edits.
- `docs/operations/orchestrator_failover.md` — new: documents expected failover behavior and the chaos-test procedure (Phase 0 explicitly calls for this doc).

---

## Validation

**Chart validation (done):**
- `helm lint helm/ -f helm/ci/test-values.yaml` passes; renders cleanly across all `helm/ci/*` values files, with defaults and with `--set orchestrator.replicas=2 --set orchestrator.pdb.minAvailable=1` (Track-2-readiness).
- Rendered orchestrator Deployment contains `terminationGracePeriodSeconds`, container `lifecycle.preStop`, `startupProbe`, and the tuned liveness/readiness; the orchestrator PDB renders when enabled and is absent when disabled.

**Local k3d mechanics (done, 2026-06-24):** the chart was Helm-deployed on the single-node k3d stack and the mechanics were exercised live — `startupProbe` + init-containers carried the orchestrator through a ~5-min cold start with **0 restarts**; `preStop` graceful termination measured **18s** (15s sleep + shutdown); `srw-orchestrator-pdb` reports **`ALLOWED DISRUPTIONS: 1`**. Full results: `docs/tests/orchestrator_m0_failover_verification.md`.

**Behavioral chaos test on the shared cluster (deferred):** the multi-node node-drain reschedule, in-flight-request drain under real traffic, and full-stack failover (job re-dispatch, session reattach, sudo survival) require the live multi-node cluster and are **deferred to a quiet overnight window**, gated on M0 reaching dev (push `develop` / Fleet sync). Procedure: `docs/operations/orchestrator_failover.md`; tracking: `docs/tests/orchestrator_m0_failover_verification.md`.

---

## Definition of done

- [x] Chart renders + lints (defaults and `replicas=2`); schema needed **no change** (permissive).
- [x] preStop + grace + startupProbe + tuned probes present in the rendered Deployment.
- [x] Orchestrator PDB renders when enabled; absent when disabled.
- [x] New values documented in `values.yaml` (`values.example.yaml` left unchanged — minimal overlay, see §5).
- [x] `docs/operations/orchestrator_failover.md` written (behavior + chaos-test runbook).
- [x] `orchestrator_ha_scaling.md` Phase 0 checkboxes + M0 status updated.
- [x] `helm/ci/` render-test values still render (verified against all four; new keys are optional with defaults).
- [ ] **Live chaos test on the shared cluster — deferred** (overnight, after M0 reaches dev). Tracked in `docs/tests/orchestrator_m0_failover_verification.md`.

---

## Decisions

- **Config-only M0.** The singleton-lifespan refactor (`_knowledge_graph_db`, cloud/IDE HTTP clients behind `lifespan`) is deferred — it's cosmetic ("harmless but ugly in logs" per the parent doc), edits the 23k-line `main.py`, and needs tests. Not worth coupling to this low-risk config slice.
- **Drain 15 / grace 60**, not the parent doc's offhand 30/60 — see §1 rationale. Tunable.
- **`startupProbe` over bumping liveness `initialDelaySeconds`** — the modern K8s answer to slow start; decouples migration time from the liveness budget permanently.
- **PDB `minAvailable: 0` default** — mandatory at `replicas:1`; the PDB exists mainly for Track-2-readiness and intent documentation.
- **No `values.schema.json` / `values.example.yaml` changes** (resolved during implementation) — the schema is permissive (doesn't define `orchestrator`) and the example file is a minimal customer overlay, so the `values.yaml` defaults cover M0. This refines the original §5, which had assumed a schema edit might be needed.

## Out of scope

Singleton-lifespan refactor; DB-aware probes (M4); NATS clustering / Postgres HA ([[high_availability_setup]]); anything that changes `replicas` (M1). No new application code.
