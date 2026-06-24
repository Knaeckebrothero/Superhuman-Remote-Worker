# Orchestrator M0 — Active-Passive Failover Hardening — Design Spec

**Status:** Approved design (2026-06-24), pending implementation plan.
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
- `helm/values.example.yaml` — mirror the new keys (it is documentation-facing).
- `helm/values.schema.json` — add matching entries under the `orchestrator` properties (`preStopDrainSeconds`: integer, `terminationGracePeriodSeconds`: integer, `pdb`: object of `enabled`: boolean + `minAvailable`: integer). The chart validates against this schema; missing entries fail `helm template` if `additionalProperties:false` is set on that object — verify during implementation.
- `docs/operations/orchestrator_failover.md` — new: documents expected failover behavior and the chaos-test procedure (Phase 0 explicitly calls for this doc).

---

## Validation

**Done here (by the implementer):**
- `helm lint helm/` passes.
- `helm template helm/` renders cleanly with (a) defaults and (b) `--set orchestrator.replicas=2 --set orchestrator.pdb.minAvailable=1` (proves Track-2-readiness and schema validity).
- Rendered orchestrator Deployment contains `terminationGracePeriodSeconds`, container `lifecycle.preStop`, `startupProbe`, and the tuned liveness/readiness; rendered output includes the orchestrator PDB when enabled. A rendered-manifest diff goes in the implementation-plan verification step.

**Behavioral chaos test (operator-run, on dev):** destructive `kubectl delete pod <orchestrator>` under load — (a) a job mid-dispatch, (b) a sudo prompt open, (c) a persistent session mid-turn — measuring observable downtime and confirming clean recovery (job re-dispatched, session reattaches, no duplicate side effects). Delivered as a script + the `orchestrator_failover.md` runbook; **not** run from this workspace (it can't safely drive the remote cluster, and local k3d/tilt runs dev Dockerfiles rather than this chart). Acceptance target: observed REST downtime within the rolling-restart window (single-digit seconds at `replicas:1` with a warm replacement; documented, not asserted in CI).

---

## Definition of done

- [ ] Chart renders + lints (defaults and `replicas=2`), schema updated.
- [ ] preStop + grace + startupProbe + tuned probes present in the rendered Deployment.
- [ ] Orchestrator PDB renders when enabled; absent when disabled.
- [ ] New values documented in `values.yaml` + `values.example.yaml`.
- [ ] `docs/operations/orchestrator_failover.md` written (behavior + chaos-test runbook + script).
- [ ] `orchestrator_ha_scaling.md` Phase 0 checkboxes + M0 status updated to reflect what landed (and what's left: the operator-run chaos test).
- [ ] `helm/ci/` render-test values still render and the chart CI gate is green (new keys are optional with defaults, so no breakage expected — confirm).

---

## Decisions

- **Config-only M0.** The singleton-lifespan refactor (`_knowledge_graph_db`, cloud/IDE HTTP clients behind `lifespan`) is deferred — it's cosmetic ("harmless but ugly in logs" per the parent doc), edits the 23k-line `main.py`, and needs tests. Not worth coupling to this low-risk config slice.
- **Drain 15 / grace 60**, not the parent doc's offhand 30/60 — see §1 rationale. Tunable.
- **`startupProbe` over bumping liveness `initialDelaySeconds`** — the modern K8s answer to slow start; decouples migration time from the liveness budget permanently.
- **PDB `minAvailable: 0` default** — mandatory at `replicas:1`; the PDB exists mainly for Track-2-readiness and intent documentation.

## Out of scope

Singleton-lifespan refactor; DB-aware probes (M4); NATS clustering / Postgres HA ([[high_availability_setup]]); anything that changes `replicas` (M1). No new application code.
