# Orchestrator M0 — Active-Passive Failover Hardening Implementation Plan

**✅ Status: COMPLETE (2026-06-24).** All tasks implemented and committed on `develop` (unpushed); chart + mechanics verified locally on k3d (`startupProbe` carried a ~5-min cold start with 0 restarts, `preStop` 18s drain, PDB `ALLOWED DISRUPTIONS: 1`). Step checkboxes below are ticked for history. **Outstanding:** the live multi-node + real-traffic chaos test — deferred to a quiet overnight window after M0 reaches dev — tracked in `docs/tests/orchestrator_m0_failover_verification.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a single orchestrator pod's death (eviction, OOM, node drain, image roll) a fast, bounded, predictable failover — graceful connection drain, slow-migration-safe startup, and a drain-aware PodDisruptionBudget — without enabling multiple replicas.

**Architecture:** Pure Helm-chart change. Add a `preStop` drain hook + `terminationGracePeriodSeconds`, a `startupProbe` that decouples migration time from the liveness budget, tightened liveness/readiness, and a new orchestrator `PodDisruptionBudget` mirroring the existing agent one. Plus an operations runbook for the operator-run chaos test. No application code.

**Tech Stack:** Helm 3 (chart `helm/`, release-name convention `srw`, chart name `superhuman-remote-worker`), Kubernetes `apps/v1` Deployment + `policy/v1` PodDisruptionBudget. Validation via `helm lint` and `helm template --show-only`.

**Spec:** `docs/superpowers/specs/2026-06-24-orchestrator-m0-active-passive-design.md`
**Parent milestone:** `docs/features/orchestrator_ha_scaling.md` — M0 / Phase 0.

## Global Constraints

- **Config-only. No application code** — Helm templates, `values.yaml`, and docs only.
- **Stays `replicas: 1`.** Nothing in M0 changes the replica count (that is M1).
- **Drain `15` / grace `60` seconds** (exact defaults, tunable via values).
- **`orchestrator.pdb.minAvailable: 0`** — mandatory at `replicas: 1`; `1` would block `kubectl drain`.
- **No `values.schema.json` change** — the schema is intentionally permissive (`additionalProperties` allowed; it does not define `orchestrator`). Confirmed by reading it; do not add entries.
- **No `values.example.yaml` change** — it is a minimal customer overlay (external services + sizing only); the HA defaults in `values.yaml` are production-appropriate.
- **Base image is `python:3.11-slim`** — has `sh`+`sleep`, so the `preStop exec` works.
- **Probe endpoint is `GET /api/health` on port `8085`** (unchanged; same endpoint for startup/liveness/readiness — DB-aware health is M4, out of scope).
- **Validation command** (renders cleanly today): `helm template srw helm/ -f helm/ci/test-values.yaml`. Lint: `helm lint helm/ -f helm/ci/test-values.yaml`.
- **Commit trailer:** every commit ends with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Commit to `develop`; **do not push.**

---

### Task 1: Graceful shutdown + probe hardening (orchestrator Deployment)

**Files:**
- Modify: `helm/values.yaml` (orchestrator block — after `agentBindTimeoutSeconds: 300`, currently line 115)
- Modify: `helm/templates/orchestrator/deployment.yaml` (pod spec ~line 21; container probe block lines 1036-1047)

**Interfaces:**
- Produces values keys consumed by the template: `orchestrator.preStopDrainSeconds` (int), `orchestrator.terminationGracePeriodSeconds` (int).

- [x] **Step 1: Write the failing test (baseline render assertion)**

Run:
```bash
helm template srw helm/ -f helm/ci/test-values.yaml \
  --show-only templates/orchestrator/deployment.yaml \
  | grep -E "terminationGracePeriodSeconds|preStop|startupProbe|failureThreshold" || echo "ABSENT"
```
Expected: `ABSENT` (none of the new fields exist yet).

- [x] **Step 2: Add the drain values to `helm/values.yaml`**

Find (line 115):
```yaml
  agentBindTimeoutSeconds: 300
```
Replace with:
```yaml
  agentBindTimeoutSeconds: 300
  # -- Graceful shutdown. On SIGTERM the pod is removed from Service endpoints;
  # the preStop sleep holds the container alive while that removal propagates
  # (kube-proxy/LB) so in-flight requests drain instead of being cut. Long-lived
  # SSE streams drop regardless and are recovered by thread_events replay.
  preStopDrainSeconds: 15
  # -- Must exceed preStopDrainSeconds + lifespan shutdown (background loops,
  # NATS drain, DB pool close).
  terminationGracePeriodSeconds: 60
```

- [x] **Step 3: Add pod-level `terminationGracePeriodSeconds` to the Deployment**

In `helm/templates/orchestrator/deployment.yaml`, find:
```yaml
      serviceAccountName: {{ include "srw.fullname" . }}-orchestrator
```
Replace with:
```yaml
      serviceAccountName: {{ include "srw.fullname" . }}-orchestrator
      terminationGracePeriodSeconds: {{ .Values.orchestrator.terminationGracePeriodSeconds }}
```

- [x] **Step 4: Add the `preStop` hook + `startupProbe` and tune liveness/readiness**

In `helm/templates/orchestrator/deployment.yaml`, find (lines 1036-1047):
```yaml
          livenessProbe:
            httpGet:
              path: /api/health
              port: 8085
            initialDelaySeconds: 30
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /api/health
              port: 8085
            initialDelaySeconds: 10
            periodSeconds: 5
```
Replace with:
```yaml
          lifecycle:
            preStop:
              exec:
                command: ["sh", "-c", "sleep {{ .Values.orchestrator.preStopDrainSeconds }}"]
          startupProbe:
            httpGet:
              path: /api/health
              port: 8085
            periodSeconds: 5
            failureThreshold: 30
            timeoutSeconds: 3
          livenessProbe:
            httpGet:
              path: /api/health
              port: 8085
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /api/health
              port: 8085
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 2
```
(The `startupProbe` gates liveness/readiness until the app is up, so their `initialDelaySeconds` are removed.)

- [x] **Step 5: Run the render assertion + lint to verify it passes**

Run:
```bash
helm lint helm/ -f helm/ci/test-values.yaml
helm template srw helm/ -f helm/ci/test-values.yaml \
  --show-only templates/orchestrator/deployment.yaml \
  | grep -E "terminationGracePeriodSeconds|preStop|sleep 15|startupProbe|failureThreshold: 30|failureThreshold: 3|failureThreshold: 2"
```
Expected: lint reports `0 chart(s) failed`; grep prints lines including `terminationGracePeriodSeconds: 60`, the `preStop`/`sleep 15` exec, `startupProbe`, and the three `failureThreshold` values.

- [x] **Step 6: Verify Track-2-readiness render (replicas=2) still validates**

Run:
```bash
helm template srw helm/ -f helm/ci/test-values.yaml \
  --set orchestrator.replicas=2 --set orchestrator.terminationGracePeriodSeconds=45 \
  --show-only templates/orchestrator/deployment.yaml | grep -E "replicas: 2|terminationGracePeriodSeconds: 45"
```
Expected: prints `replicas: 2` and `terminationGracePeriodSeconds: 45` (values plumb through cleanly).

- [x] **Step 7: Commit**

```bash
git add helm/values.yaml helm/templates/orchestrator/deployment.yaml
git commit -m "feat(helm): orchestrator graceful drain + startupProbe + probe tuning (M0)" \
  -m "preStop drain hook (sleep \$preStopDrainSeconds) + terminationGracePeriodSeconds so in-flight requests drain on SIGTERM; startupProbe (failureThreshold:30) decouples slow migrations from the liveness budget (fixes crash-loop-on-slow-migration); tighter liveness/readiness. Config-only, replicas:1 unchanged." \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Orchestrator PodDisruptionBudget

**Files:**
- Modify: `helm/values.yaml` (orchestrator block — after `terminationGracePeriodSeconds: 60` added in Task 1)
- Create: `helm/templates/orchestrator/pdb.yaml`

**Interfaces:**
- Consumes: nothing from Task 1's template (independent), but appends to the same `values.yaml` orchestrator block — run after Task 1.
- Produces values keys: `orchestrator.pdb.enabled` (bool), `orchestrator.pdb.minAvailable` (int).

- [x] **Step 1: Write the failing test (no orchestrator PDB renders yet)**

Run:
```bash
helm template srw helm/ -f helm/ci/test-values.yaml | grep "orchestrator-pdb" || echo "ABSENT"
```
Expected: `ABSENT`.

- [x] **Step 2: Add the PDB values to `helm/values.yaml`**

Find (added in Task 1):
```yaml
  terminationGracePeriodSeconds: 60
```
Replace with:
```yaml
  terminationGracePeriodSeconds: 60
  # -- Pod Disruption Budget. At replicas:1, minAvailable MUST be 0 — 1 would
  # block all voluntary evictions (kubectl drain), the 2026-05-16 incident mode.
  # Flip minAvailable to 1 when replicas>=2 (M1).
  pdb:
    enabled: true
    minAvailable: 0
```

- [x] **Step 3: Create the PDB template**

Create `helm/templates/orchestrator/pdb.yaml` (mirrors `helm/templates/agent/pdb.yaml`):
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

- [x] **Step 4: Run the render assertions to verify it passes**

Run:
```bash
helm lint helm/ -f helm/ci/test-values.yaml
helm template srw helm/ -f helm/ci/test-values.yaml \
  --show-only templates/orchestrator/pdb.yaml
```
Expected: lint `0 chart(s) failed`; the second command renders a `PodDisruptionBudget` named `srw-superhuman-remote-worker-orchestrator-pdb` with `minAvailable: 0`.

- [x] **Step 5: Verify the enable/disable toggle**

Run:
```bash
echo "enabled (expect 1):"; helm template srw helm/ -f helm/ci/test-values.yaml | grep -c "orchestrator-pdb"
echo "disabled (expect 0):"; helm template srw helm/ -f helm/ci/test-values.yaml --set orchestrator.pdb.enabled=false | grep -c "orchestrator-pdb"
echo "replicas2/minAvailable1 (expect 'minAvailable: 1'):"; helm template srw helm/ -f helm/ci/test-values.yaml --set orchestrator.pdb.minAvailable=1 --show-only templates/orchestrator/pdb.yaml | grep "minAvailable:"
```
Expected: `1`, then `0`, then `minAvailable: 1`.

- [x] **Step 6: Commit**

```bash
git add helm/values.yaml helm/templates/orchestrator/pdb.yaml
git commit -m "feat(helm): orchestrator PodDisruptionBudget (M0)" \
  -m "Mirror the agent PDB for the orchestrator; gated on orchestrator.pdb.enabled, minAvailable:0 (mandatory at replicas:1 so kubectl drain is not blocked). Flips to 1 for Track 2 (M1)." \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Failover runbook + chaos-test procedure

**Files:**
- Create: `docs/operations/orchestrator_failover.md`

This is documentation (the chaos test is operator-run on a live cluster — it is not executed from this workspace). The runbook uses copy-paste `kubectl` blocks, matching the runbook style in `docs/features/high_availability_setup.md`.

- [x] **Step 1: Create the runbook**

Create `docs/operations/orchestrator_failover.md`:
```markdown
# Orchestrator Failover — Behavior & Chaos Test

> Companion to `docs/features/orchestrator_ha_scaling.md` (Milestone M0). The
> orchestrator runs `replicas: 1`. This documents what happens when that pod
> dies and how to verify the failover hardening (preStop drain, startupProbe,
> PodDisruptionBudget) actually works.

## Expected behavior on pod death

| Event | Mechanism | User-visible effect |
|---|---|---|
| Pod evicted / OOM / image roll | Kubernetes reschedules; the new pod's `startupProbe` waits out migrations (up to ~150s) before liveness can act. | REST 5xx/timeout for the rolling-restart window (single-digit seconds with a warm node; longer on a cold image pull). |
| Graceful termination (drain, rollout) | `preStop` sleeps `preStopDrainSeconds` (15s) after the pod leaves Service endpoints, so in-flight REST drains. `terminationGracePeriodSeconds` (60s) bounds total shutdown. | In-flight requests finish; no truncation. |
| In-flight jobs | Agents heartbeat-timeout (3 min); `recover_orphaned_jobs` flips them to `paused`; the new orchestrator re-dispatches. | Jobs resume automatically; no loss. |
| Persistent sessions | The agent pod keeps running and keeps writing `thread_events`; cockpit reconnects and replays from `Last-Event-ID`. | Session reattaches after the bounce. |
| Open sudo prompts | Reply subject is persisted (`sudo_approval_requests.nats_reply_subject`); decision is delivered on resolve. | Prompt survives; may need a cockpit refresh. |
| Node drain | `kubectl drain` is allowed because `orchestrator.pdb.minAvailable: 0`. | Pod reschedules onto another node. |

## Chaos test (run on dev, not prod)

Destructive. Run against the dev cluster with a warm agent pool. Measure
observable REST downtime and confirm clean recovery.

```bash
CTX=--context=dev          # your dev kube-context
NS=superhuman-remote-worker

# 0. Baseline: orchestrator healthy, one replica.
kubectl $CTX -n $NS get pods -l app.kubernetes.io/component=orchestrator -o wide

# 1. In one terminal, poll the API to measure downtime.
API=https://api.dev.example.com/api/health
while true; do
  printf '%s ' "$(date +%T)"
  curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" "$API" || echo "FAIL"
  sleep 1
done

# 2. Set up the three in-flight conditions:
#    (a) a job mid-dispatch, (b) an open sudo prompt, (c) a session mid-turn.
#    (Drive these from cockpit / the MCP as appropriate for your setup.)

# 3. In a second terminal, delete the orchestrator pod under load.
kubectl $CTX -n $NS delete pod -l app.kubernetes.io/component=orchestrator --wait=false

# 4. Watch recovery.
kubectl $CTX -n $NS get pods -l app.kubernetes.io/component=orchestrator -w
```

### Pass criteria

- Polling loop returns to `200` within the rolling-restart window (record the count of non-200 seconds).
- The new pod becomes Ready after its `startupProbe` passes (no crash-loop, even if migrations run).
- The mid-dispatch job ends up `paused` then re-dispatched — exactly once.
- The persistent session reattaches after refresh; no duplicated assistant turn.
- The sudo prompt resolves (after refresh if needed); the agent is not wedged.

### Drain test (verifies the PDB)

```bash
NODE=$(kubectl $CTX -n $NS get pod -l app.kubernetes.io/component=orchestrator -o jsonpath='{.items[0].spec.nodeName}')
kubectl $CTX cordon "$NODE"
kubectl $CTX drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=10m
# Expected: drain proceeds (PDB minAvailable:0 does not block it); orchestrator
# reschedules onto another node. Contrast: minAvailable:1 at replicas:1 would
# hang the drain — that is why M0 ships minAvailable:0.
kubectl $CTX uncordon "$NODE"
```

## Notes

- Sub-second failover requires `replicas: 2`, which is **not safe until M1**
  (leader election) — dispatch double-assign + IMAP double-poll. Do not raise
  `orchestrator.replicas` before M1. See `docs/features/orchestrator_ha_scaling.md`.
```

- [x] **Step 2: Verify the doc renders and bash blocks are well-formed**

Run:
```bash
test -f docs/operations/orchestrator_failover.md && echo "EXISTS"
grep -cE "^## (Expected behavior|Chaos test|Notes)" docs/operations/orchestrator_failover.md
# Extract fenced bash and syntax-check it:
awk '/^```bash$/{f=1;next}/^```$/{f=0}f' docs/operations/orchestrator_failover.md | bash -n && echo "BASH OK"
```
Expected: `EXISTS`; `3`; `BASH OK`.

- [x] **Step 3: Commit**

```bash
git add docs/operations/orchestrator_failover.md
git commit -m "docs(ops): orchestrator failover behavior + chaos-test runbook (M0)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Update the design doc M0 / Phase 0 status

**Files:**
- Modify: `docs/features/orchestrator_ha_scaling.md` (Refresh reality-check matrix M0 row; Phase 0 checklist)

- [x] **Step 1: Update the reality-check matrix M0 row**

Find:
```markdown
| **Track 1** active-passive hardening (probes / `preStop` / grace / PDB) | not started | **NOT STARTED** | No `preStop`, no `terminationGracePeriodSeconds`, no orchestrator PDB anywhere in `helm/`. Probes present but untuned. |
```
Replace with:
```markdown
| **Track 1** active-passive hardening (probes / `preStop` / grace / PDB) | not started | **SHIPPED (chart); chaos test operator-pending** | `preStop` drain + `terminationGracePeriodSeconds` + `startupProbe` + tuned probes + orchestrator PDB landed in `helm/` (M0). Behavioral chaos test is operator-run on dev — see `docs/operations/orchestrator_failover.md`. |
```

- [x] **Step 2: Update the Phase 0 checklist**

Find:
```markdown
### Phase 0 — Track 1: Active-passive failover hardening — NOT STARTED
- [ ] Tighten readiness probe + add `preStop` hook + `terminationGracePeriodSeconds` in `helm/templates/orchestrator/deployment.yaml`.
- [ ] Add `helm/templates/orchestrator/pdb.yaml` (mirror `agent/pdb.yaml`). Document expected failover latency.
- [ ] Move module-level singletons behind `lifespan` startup for clean SIGTERM.
- [ ] Chaos test: delete the pod under load; measure user-visible downtime.
- [ ] Document failover behavior in `docs/operations/orchestrator_failover.md`.
```
Replace with:
```markdown
### Phase 0 — Track 1: Active-passive failover hardening — SHIPPED (chart); chaos test operator-pending
- [x] Tighten readiness probe + add `preStop` hook + `terminationGracePeriodSeconds` + `startupProbe` in `helm/templates/orchestrator/deployment.yaml`. (2026-06-24)
- [x] Add `helm/templates/orchestrator/pdb.yaml` (mirror `agent/pdb.yaml`), `minAvailable: 0`. (2026-06-24)
- [ ] Move module-level singletons behind `lifespan` startup for clean SIGTERM. **Deferred** — cosmetic; tracked as a follow-up.
- [ ] Chaos test: delete the pod under load; measure user-visible downtime. **Operator-run on dev** — runbook ready.
- [x] Document failover behavior in `docs/operations/orchestrator_failover.md`. (2026-06-24)
```

- [x] **Step 3: Verify**

Run:
```bash
grep -c "SHIPPED (chart); chaos test operator-pending" docs/features/orchestrator_ha_scaling.md
grep -c "\- \[x\]" docs/features/orchestrator_ha_scaling.md
```
Expected: `2` (matrix row + Phase 0 heading); at least `3` checked boxes.

- [x] **Step 4: Commit**

```bash
git add docs/features/orchestrator_ha_scaling.md
git commit -m "docs(ha): mark M0 chart hardening shipped; chaos test operator-pending" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- preStop + grace → Task 1. startupProbe + probe tuning → Task 1. PDB → Task 2. values keys → Tasks 1-2. Runbook + chaos test → Task 3. Doc status update → Task 4.
- Spec DoD "schema updated" / "values.example.yaml" → resolved during planning as **no change needed** (permissive schema; minimal example file) and recorded in Global Constraints. No orphaned spec requirement.

**Placeholder scan:** No TBD/TODO; every code/edit step shows exact content; every test step has an exact command + expected output. The `<node-with-orchestrator>` / `dev` context tokens in the runbook are operator-supplied runtime values (a runbook is inherently parameterized), not plan placeholders.

**Type/name consistency:** Values keys `orchestrator.preStopDrainSeconds`, `orchestrator.terminationGracePeriodSeconds`, `orchestrator.pdb.enabled`, `orchestrator.pdb.minAvailable` are referenced identically in `values.yaml` and the templates. PDB name `srw-...-orchestrator-pdb` and the `srw.componentLabels`/`srw.componentSelectorLabels` helpers match the agent PDB template. Probe endpoint `/api/health:8085` consistent across all three probes.
