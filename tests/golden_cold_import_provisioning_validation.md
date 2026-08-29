# Validation — golden cold-import provisioning (`waiting_golden` path)

**Type:** hybrid — unit-covered (pytest, green) + manual live-smoke runbook (the
trigger requires a golden-image cold import on the real VM cluster, which k3d
cannot produce — no KubeVirt/CDI).
**Status (2026-07-12): unit PASSED · deployed both sides · live smoke PENDING.**
The fix (commit `5f8b6047`) has been running on the dev stack since 2026-07-10
(orchestrator `sha-2a71df3`, vm-controller `sha-f1f32eb`) and two golden cold
imports have occurred since — but **no VM create has coincided with an import
window yet**, so the new path has never fired outside unit tests
(0 × `deferring VM create` in controller logs; 8 VM creates, all warm-golden).

**What it validates:** a VM job dispatched while the golden DataVolume is still
importing (any `agent-vm-base` bump → ~15–30 min cold import) **waits** on a
dedicated golden budget instead of burning its 3 provision attempts and failing
— the failure that cost RSI-loop iteration 22 (2026-07-09). Plus the two
secondary hardenings from the same incident: idempotent VM-create 409, and
instant orphan force-delete for unroutable tailnet VMs.

**Design + incident forensics:**
`knowledge-history/done/golden_image_cold_import_fails_inflight_vm_jobs.md` (§Chosen fix).
Code: `vm/controller/controller.py` (`_golden_state_nowait`, `_do_create`,
idempotent 409) · `orchestrator/services/dispatch_guards.py`
(`VM_GOLDEN_POLL`/`VM_PARK_GOLDEN`) · `orchestrator/main.py` (dispatcher
branches) · `orchestrator/services/vm_provisioner.py` (`create_vm(fresh=False)`)
· `orchestrator/services/nats_bridge.py` (telemetry passthrough) ·
`orchestrator/services/lifecycle/vm_manager.py` (`attempts_exhausted`).

---

## What IS verified

### Unit coverage (all green, part of the 5 affected suites — 446 tests)

| Behavior | Test |
|---|---|
| Golden Succeeded → clone source, no wait | `test_vm_controller.py::TestGoldenStateNowait::test_succeeded_returns_name_no_waiting`, `TestDoCreateWaitingGolden::test_succeeded_golden_creates_vm_with_clone_source` |
| Golden importing → `waiting_golden`, **never sleeps**, no VM, no Headscale key | `TestGoldenStateNowait::test_importing_returns_waiting_without_sleeping`, `TestDoCreateWaitingGolden::test_importing_golden_defers_vm_create` |
| Golden absent → DV created once (409 = racer lock), then waits | `TestGoldenStateNowait::test_absent_creates_dv_and_returns_waiting`, `::test_absent_create_409_racer_still_waits` |
| CDI infra error → legacy registry fallback (golden off ≠ VM outage) | `TestGoldenStateNowait::test_absent_create_error_falls_back_to_registry`, `TestDoCreateWaitingGolden::test_golden_infra_error_falls_back_to_registry_create` |
| Failed golden DV → deleted + recreated → waits | `TestGoldenStateNowait::test_failed_golden_recreated_then_waits` |
| Plain VM-create 409 AlreadyExists → **idempotent success** (was: `failed` → parked 2 loop jobs) | `TestHandleCreate::test_create_vm_409_already_exists_is_idempotent_success` |
| "is being deleted" 409 retry loop unchanged | `TestHandleCreate::test_create_vm_conflict_retry_succeeds`, `::test_create_vm_conflict_exhausted_retries` |
| `waiting_golden` → POLL within golden budget; **outlives the 600s boot budget without RECYCLE**; **ignores attempt exhaustion** | `test_dispatch_guards.py::TestVmGoldenWaitDecision` (5 tests) |
| `waiting_golden` past `VM_GOLDEN_WAIT_TIMEOUT_S` → `VM_PARK_GOLDEN` | `TestVmGoldenWaitDecision::test_waiting_golden_parks_past_golden_budget` |
| Poll re-issue merges ONLY rolling `provisioned_at` (no ctx reset, no `provisioning` overwrite) | `test_vm_provisioner.py::TestGoldenPollCreate` (3 tests), `test_nats_bridge.py::TestRequestVmCreate::test_golden_poll_skips_provisioning_status` |
| Fresh provision clears `golden_wait_started_at` | `TestFreshProvisionReset::test_fresh_provision_ctx_shape`, `TestGoldenPollCreate::test_fresh_default_resets_and_sets_provisioning` |
| Golden telemetry (`golden`/`golden_phase`/`golden_progress`) → `context.vm` | `test_nats_bridge.py::TestOnVmLifecycleStatus::test_waiting_golden_passes_import_telemetry`, `::test_non_golden_status_has_no_golden_keys` |
| Unroutable tailnet VM → `attempts_exhausted` instantly true (reaper force-deletes first tick, not after ~5 min); escape hatch + counter semantics preserved | `test_lifecycle_vm_manager.py::TestAttemptsExhausted` (5 tests) |

### Live evidence (dev stack, 2026-07-10 → 2026-07-12)

- **Rollout:** both images contain `5f8b6047`; no version-skew issue observed.
- **Pre-warm on a real image bump** (`sha-01bf2c9`, 2026-07-10 13:43): controller
  started the golden import in the background task; the (intentionally kept)
  blocking `_ensure_golden` pre-warm logged its non-fatal >900s warning —
  as designed, the warning no longer implies job damage.
- **Golden GC**: deleted 2 stale goldens (`e98330dd0d47`, `58a046a8c6f3`)
  without touching current/in-use ones.
- **Warm-path regression:** 8 VM creates since deploy, all against `Succeeded`
  goldens, all normal — the fast path is unaffected.

---

## What is NOT yet verified (live)

None of these has fired in production yet. Expected signatures listed so the
next occurrence can be recognized in logs.

| # | Path | Expected live signature |
|---|---|---|
| 1 | **`waiting_golden` defer** — VM create arrives during a cold import | controller: `golden agent-vm-golden-<hash> not ready for job <id> (<progress>) — deferring VM create`; orchestrator: `VM lifecycle status for job <id>: waiting_golden` |
| 2 | **`VM_GOLDEN_POLL` loop** — dispatcher polls without burning attempts | orchestrator every ~30s: `Dispatcher: job <id> waiting on golden image agent-vm-golden-<hash> (<progress>) — polling`; `context.vm.provision_attempts` stays at 1; **no** `VM stuck in … recycling` lines during the window |
| 3 | **Create-after-golden** — VM create issues ≤1 tick after DV `Succeeded`, boots, dispatches | controller: `VM created: agent-vm-<id>` within ~30s of `Succeeded`; job → `processing`; zero jobs failed by the import window |
| 4 | **`VM_PARK_GOLDEN`** — import wedged past 2700s | orchestrator: `Dispatcher: job <id> golden wait exhausted — failing job (golden image import did not complete within 2700s …)`; job `failed` with that error_message. *Deliberately not forced live* — would need env surgery on the Fleet-managed deploy; unit coverage is the authority here |
| 5 | **Idempotent 409** — duplicate/racing create | controller: `VM agent-vm-<id> already exists (job <id>) — idempotent create` then status `created` (NOT `failed`). Should be rare now that creates don't block; its firing is benign by design |
| 6 | **F4 instant orphan force-delete** — dirty unreachable tailnet VM reaped | orchestrator: `Lifecycle reaper force-deleted dirty unreachable instance kind=vm …` within **one** reconciler tick (~60s) of the VM becoming orphaned — not after ~5 min of `reap_attempts` |

---

## Manual live-smoke runbook (forces path 1–3 on the dev stack)

Forces a cold import **without** waiting for the next `agent-vm-base` bump, by
deleting the current golden DV (it is only a cache — CDI re-imports it).
Cost: one ~15–30 min import window on the dev VM cluster.

### Cluster facts

| Thing | Value |
|---|---|
| Orchestrator | your app context/namespace, `deploy/srw-orchestrator` |
| VM controller | your VM-cluster context, ns `agent-vms`, `deploy/srw-vm-vm-controller` |
| **Do NOT touch** | any production namespace on either cluster |
| Golden naming | `agent-vm-golden-<sha256(image)[:12]>`, annotation `srw.io/vm-image-ref` holds the image |
| Budgets | boot `VM_PROVISION_TIMEOUT_S=600` × 3 attempts; golden `VM_GOLDEN_WAIT_TIMEOUT_S=2700` (code default, no helm override) |

### Preconditions

1. No VM currently provisioning/cloning (an in-flight clone reads the golden
   PVC — deleting it mid-clone breaks that VM):
   ```bash
   kubectl --context=vm -n agent-vms get vmi        # ideally empty / all Running+Ready
   kubectl --context=vm -n agent-vms get dv | grep -v golden   # no rootdisk mid-clone
   ```
2. No loop iteration mid-provision on the dev orchestrator (or accept that its
   jobs will — correctly — wait out the import; that is the fix working, but
   it delays the loop by the import time).

### Steps

1. Identify the current golden (match `srw.io/vm-image-ref` to the controller's
   `DEFAULT_VM_IMAGE`):
   ```bash
   kubectl --context=vm -n agent-vms exec deploy/srw-vm-vm-controller -- sh -c 'echo $DEFAULT_VM_IMAGE'
   kubectl --context=vm -n agent-vms get dv -l srw.io/golden-image \
     -o custom-columns='NAME:.metadata.name,IMAGE:.metadata.annotations.srw\.io/vm-image-ref,PHASE:.status.phase'
   ```
2. Delete it (PVC cascades):
   ```bash
   kubectl --context=vm -n agent-vms delete dv <agent-vm-golden-XXXX>
   ```
3. Create a VM-backed job (cockpit → New Job with `workspace.backend=vm`, or
   let the loop dispatch one).
4. Watch both sides:
   ```bash
   kubectl --context=vm -n agent-vms logs deploy/srw-vm-vm-controller -f | grep -Ei 'golden|deferring|created|idempotent'
   kubectl --context=<app-cluster> -n <app-namespace> logs deploy/srw-orchestrator -f \
     | grep -Ei 'waiting on golden|golden wait exhausted|waiting_golden|auto-provisioned|VM stuck|recycling'
   kubectl --context=vm -n agent-vms get dv -w | grep golden
   ```
   Job context (via MCP `get_job`/`query_table jobs`, or the cockpit job page):
   `context.vm.status`, `context.vm.golden_progress`,
   `context.vm.provision_attempts`, `context.vm.golden_wait_started_at`.

### Pass criteria

- [ ] Controller answers the create with `deferring VM create` — **no VM object,
      no Headscale key** minted during the window (signature 1).
- [ ] Orchestrator logs the `waiting on golden image … — polling` line ~every
      30s; `provision_attempts` stays at 1; **zero** `recycling` lines
      (signature 2).
- [ ] Within ~30s of the DV flipping `Succeeded`, the VM is created (clone
      source), boots, registers, and the job reaches `processing`
      (signature 3).
- [ ] No job failed with `provisioning exhausted` or a 409 during the window.
- [ ] Fast-path sanity after: a second VM job created *after* the golden is
      warm boots in the normal ~5 min with no golden log lines.

### Cleanup

None — the re-imported golden IS the warm cache again; the smoke job completes
or can be cancelled normally.

### On success

Update `knowledge-history/done/golden_image_cold_import_fails_inflight_vm_jobs.md` (drop the
live-verification caveat) and this doc's **Status** header; move the checked
rows of §What-is-NOT-yet-verified into §Live evidence with the log excerpts.
