# PVC-reattach recovery churns to fail-loud: recreated workspace pod gets a new ephemeral IP the resuming agent never reaches

**Status:** Filed + diagnosed live on k3d (2026-06-29). **FIXED via Option 1 (stable headless Service) — committed `7fb9e9e2`, k3d mechanism-verified; full real-job auto-resume E2E still pending.** Each PVC-backed job workspace now gets a headless Service named after the pod, so the agent dials a constant DNS (`workspace-<id>.<ns>.svc:30022`) that always resolves to the current pod; both the dispatch and resume paths prefer this stable `host` over the ephemeral `pod_ip` (resume previously injected no container host at all → used the stale persisted IP). Mechanism proof: across a force-delete + recreate the pod IP changed `10.42.0.102→.103` while the DNS stayed connectable (resolved to the new pod, `:30022`, attempt 1) and the PVC sentinel survived. **Remaining acceptance gate:** the full real-job E2E (kill a *running* PVC-backed job's pod mid-run → it auto-resumes from checkpoint instead of failing — §Acceptance criteria), to be run once with G1+G2+Option 1 all deployed.
**Original diagnosis (kept for context):** Discovered by the Phase-2 crash-recovery E2E. The PVC-reattach *data* path worked; the *agent reconnect* did not (stale ephemeral IP), so recovery fail-louded instead of resuming.
**Found:** 2026-06-29. k3d (`k3d-srw`/`srw`), scholar job `d65d93d3-8435-451e-9e84-07057e5de0b0`, `WORKSPACE_PVC_ENABLED=true`, with the G1+G2 recovery changes deployed.
**Severity:** **High for the recovery feature.** With G1+G2, a workspace-lost job no longer wedges forever (good) and its PVC data is preserved (good) — but it **does not auto-resume**: it churns through the bounded recovery attempts and **fails the job**. So crash-recovery is "fail cleanly + keep the data," not "resume." This is the last blocker to real auto-recovery.
**Component:** workspace recreate → re-dispatch IP propagation (`orchestrator/main.py` dispatch pod arm + `workspace_unavailable` handler `:10118`) · `ensure_workspace`/`create_workspace` (new pod = new `pod_ip`) · agent workspace backend addressing (`src/core/backends/remote.py` — dials `pod_ip`, no stable name) · the recovery cap (`WORKSPACE_RECOVERY_MAX_ATTEMPTS`, default 3)
**Related:** `docs/features/workspace_pvc_branch_a_implementation.md` (Phase 2 — this is its blocker) · `loop_job_workspace_lost_wedged_in_recovery.md` (the wedge G1 fixed; this is the next layer) · `agent_workspace_pod_resource_headroom.md` (the OOM that triggers workspace loss in the first place)

---

## Symptom

A PVC-backed worker job whose workspace pod dies mid-run recovers correctly at the data layer — the PVC survives, a new pod reattaches it by name, the working tree is intact — but the **resuming agent can never connect to the new pod**, so each recovery attempt re-reports `workspace_unavailable`, and after the bounded cap (3) the job ends `failed` with `"workspace unavailable; recovery exhausted after 3 attempts"`. The job is lost (cleanly, not wedged), and **G2's non-destructive resume branch is never exercised** (no agent ever SSH-connects to the reattached pod).

## Root cause — the recreated pod's IP changes every cycle; the agent dials a stale one

Workspace pods are addressed by their **ephemeral pod IP** (`workspace_container.pod_ip`, dialed at `…:30022`), not a stable name. The recovery loop is:

1. Workspace pod dies → agent reports `workspace_unavailable`.
2. G1 (fixed) invalidates the container (`status="deleted"`, `pod_ip=None`) + re-dispatches → `ensure_workspace` → `create_workspace` → **new pod, new IP**, `workspace_container.pod_ip` updated.
3. The job re-dispatches and the agent is handed a `pod_ip` — but by the time it dials, the value it got is **stale** (a prior cycle's IP). It can't connect → reports `workspace_unavailable` → back to step 2 with **yet another new IP**.

The IPs leapfrog and the agent is always a cycle behind. Bounded by the cap → `failed`.

### Live evidence (job `d65d93d3`)

- Workspace pod IPs across recovery cycles: **`10.42.0.79` → `.88` → `.93` → `.95`** (each recreate = new IP). PVC stayed `Bound` to the **same PV** `pvc-79610dbe…` throughout (reattach works).
- The **untracked sentinel** `E2E_SENTINEL.txt` (planted pre-kill, never committed/pushed) **survived every recreate** — the working tree is preserved; the old `initialize()` `rm -rf` would have wiped it. So the data path is sound.
- Final `freeze_data`: `{"freeze_type":"workspace_unavailable","detail":"Failed to connect to VM 10.42.0.88:30022 after 5 attempts","recovery_attempts":4}` — the agent died dialing **`.88`** while the live pod was at **`.95`**. Stale IP, definitively.
- Job ended `status="failed"`, `error_message="workspace unavailable; recovery exhausted after 3 attempts: …"`, `vm.requested` **null** (G1 routing correct — never the VM path).

## What already works (so this issue is tightly scoped)

These were all verified in the same E2E and are **not** in question:
- G1 routes pod jobs to PVC reattach, never the VM arm (`vm.requested` null every cycle).
- G1 bounded cap → fail-loud: clean terminal `failed` + `freeze_data`, never wedges.
- PVC survives the pod kill and reattaches by name (same PV across all recreates).
- Working tree (un-pushed sentinel) preserved across recreates.
- G1's stale-container bug (a separate defect found+fixed in the same pass) is gone — the recreate now actually happens.

The single missing piece is **the agent reaching the recreated pod**.

## Fix options

**Option 1 — stable workspace address (recommended, robust).** Give each workspace a stable name the agent dials instead of the ephemeral pod IP, so recreate → same address → the agent reconnects with no IP propagation at all:
- A per-workspace **headless Service** (or the pod's DNS via a governing Service) named deterministically (e.g. `workspace-<id[:12]>`), agent dials `workspace-<id>.<ns>.svc:30022`.
- Pairs naturally with the deterministic pod name + owner-keyed PVC already in place.
- Cost: one Service object per workspace (create/delete alongside the pod), and the agent/orchestrator switch from IP to DNS for the workspace endpoint.

**Option 2 — reliable fresh-IP propagation (lighter, interim).** Keep IP addressing but make every (re)dispatch inject the **current** `pod_ip` and don't let recreate latency burn the cap:
- Serialize recovery so only one recreate is in flight; re-read `pod_ip` from the freshly-`ready` container *immediately before* building the agent's `JobStartRequest`.
- Don't count a "raced a new IP / sshd not yet up" failure the same as a genuine workspace loss against `WORKSPACE_RECOVERY_MAX_ATTEMPTS` (or raise the cap / add a short settle wait so a healthy recreate isn't failed by timing).
- Smaller change, but fiddlier and still IP-fragile; a stopgap before Option 1.

**Also worth checking:** whether `workspace_container.status` is set to `"ready"` before the pod's sshd actually accepts connections (a readiness-vs-sshd gap would compound the race) — gate `ready` on a real `:30022` probe if so.

## Acceptance criteria

- A PVC-backed job whose workspace pod is force-deleted mid-run **resumes**: re-dispatch → recreate → reattach → the agent **connects to the recreated pod**, G2 logs `Reattached workspace detected`, the job continues from its checkpoint with the working tree intact — **without** consuming the recovery cap.
- Re-run Phase-2 E2E step 7 (`workspace_pvc_branch_a_implementation.md`) to green: same kill, but the job ends back in `processing`/`completed`, not `failed`, and the sentinel is present on the resumed workspace.

## Repro

```bash
CTX=k3d-srw NS=srw
# 1. create a PVC-backed worker job (internal key), wait for its workspace pod + a few checkpoints
# 2. plant an untracked sentinel:  kubectl exec workspace-<id[:12]> -- sh -c 'echo X > /home/agent-host/workspace/SENTINEL'
# 3. force-delete the workspace pod: kubectl delete pod workspace-<id[:12]> --grace-period=0 --force
# 4. watch: PVC stays Bound (same PV); new pods appear with NEW IPs; recovery_attempts climbs; job → failed
kubectl --context=$CTX -n $NS exec -i srw-postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  -c "SELECT status, error_message, context->'workspace_container'->>'pod_ip', jsonb_pretty(freeze_data) FROM jobs WHERE id='<job>';"
```
