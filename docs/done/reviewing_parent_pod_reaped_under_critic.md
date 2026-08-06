---
tags:
  - issue
  - jobs
  - critic
  - workspace-lifecycle
  - lifecycle-reaper
  - agent-resilience
  - project-loop
---

# `reviewing`/`pending_review` parents get their workspace pod reaped out from under a live critic

**Status:** FIXED at HEAD — landed in `656b31ec`, verified by audit
2026-08-06 (batch fix session). Fix option 1 (the precise guard) was taken,
in BOTH managers: `is_reapable`/`is_idle` in
`orchestrator/services/lifecycle/workspace_manager.py` AND `vm_manager.py`
return False when `inst.metadata["has_live_shared_child"]` is set;
`list_instances` stamps that flag via `_live_shared_child_exists`, which
matches a non-terminal child job whose
`context.workspace_container.pod_name` equals this pod (pod_name, not
pod_ip — stable across restores; delegation children get their own pods so
the guard stays narrow). Fail-safe: DB error → assume a child exists → do
not reap. `reviewing` deliberately REMAINS in the idle/reapable status set;
the guard handles the dependency precisely (the rewritten comment block in
workspace_manager.py explains why that is now safe). Tests:
`tests/test_lifecycle_workspace_manager.py`
(`test_reviewing_pod_with_shared_child_flagged_not_reapable` + control +
query-skip) and VM twins in `tests/test_lifecycle_vm_manager.py` (incl. the
fail-safe branch).
**Originally:** Filed + diagnosed 2026-07-04, on the **main cluster** (ns `superhuman-remote-worker`), investigating a batch of failed research-loop jobs. **Not fixed.** This is a **recurrence of the known "Bug 1" in
[`critic_failure_leaves_parent_job_stuck_reviewing.md`](critic_failure_leaves_parent_job_stuck_reviewing.md)** (§2026-06-22, "the reaper kills a workspace a live subjob is sharing"), now with two new wrinkles: (a) `"reviewing"` was *added* to the reapable status set, widening the trigger; (b) the failure signature is now a headless-Service `NXDOMAIN` (`Name or service not known`) rather than June's raw-pod-IP `No route to host`.

**Found:** 2026-07-04, project loop "Run 8" (Research 1–4). Deployed orchestrator image `sha-fe10ea6` (deploy `e453016c`).

**Severity:** **High for verification-enabled jobs.** Every scholar whose critic loses the race fails its review; the parent is then stuck `reviewing` (downstream bug below). Intermittent (race), so a *fraction* of a batch fails each run — here 3 of 4.

**Component:**
- reapable status set — `orchestrator/services/lifecycle/workspace_manager.py:34-49` (`_IDLE_JOB_STATUSES` now contains `reviewing` + `pending_review`; `_REAPABLE_JOB_STATUSES = _IDLE ∪ _TERMINAL`)
- reapability predicate — `workspace_manager.py:205` (`is_reapable`) — checks **only** the parent's `job_status`; **no guard for a live child/critic job**
- reap decision — `orchestrator/services/lifecycle/reconciler.py:254-297` (`_reap`): dirty + reachable → `snapshot` then `delete(grace=0)`
- pod delete (Service survives) — `workspace_manager.py:441-481` (`delete`): deletes the pod; reclaims the stable headless Service **only** when `_is_terminal` + PVC-backed → for a non-terminal `reviewing`/emptyDir pod the Service lingers with **zero endpoints** → `NXDOMAIN`
- critic→parent pod sharing — `_trigger_verification_on_complete` copies the parent's `context.workspace_container` into the critic (June doc cites `orchestrator/main.py:9185-9195`; line refs have since drifted) so the critic SSHes into the parent's workspace
- misleading error string — `src/core/backends/remote.py:223` (hardcodes "VM" for the container path too)

**Related:** [`critic_failure_leaves_parent_job_stuck_reviewing.md`](critic_failure_leaves_parent_job_stuck_reviewing.md) (canonical — Bug 1/2/3 + the stuck-`reviewing` downstream) · [`ide_settings_sweeper_probes_stale_workspace_endpoints.md`](ide_settings_sweeper_probes_stale_workspace_endpoints.md) (stable headless Service context) · [`unify_scholar_critic_subjob_provisioning.md`](unify_scholar_critic_subjob_provisioning.md) (the critic-spawn path — where the decouple fix lives) · `srw_vm_dispatcher_reconciler_churn`

---

## Symptom

A "Verify deliverables of job X (scholar)" **critic** job fails; **every** file/shell/kb tool call for the entire run returns:

```
Error reading file: Failed to connect to VM
workspace-<parent-job-id>.superhuman-remote-worker.svc.cluster.local:30022
after 5 attempts: [Errno -2] Name or service not known
```

The word **"VM" is a red herring** — no VM is involved (see Issue 2). Port 30022 + a `workspace-<id>` Service DNS name is the **container/`sandbox`** attach path.

## Incident data (2026-07-04, UTC from the app DB; UI column is local +2h)

| Parent (scholar) | Parent status | Critic subjob | Critic result | Notes |
|---|---|---|---|---|
| `68372d40` Research 1 | **reviewing** | `e7e6971f` | **failed** | gpt-5.5; ~39 min, every tool `NXDOMAIN`, then failed |
| `e8408426` Research 3 | **reviewing** | `0a14bbf8` | **failed** | same signature |
| `5c8f6931` Research 4 | **reviewing** | `200d8fb4` | **failed** | MiniMax-M3; killed mid-`run_command` (last audit entry is a tool *call* with no result) |
| `e3ab683a` Research 2 | **pending_review** | `05e9fecc` | **completed** ✅ | **control** — same code path, won the race |
| — | — | `9fb6d213` Research 4 **V2** | **failed** | separate symptom — unseeded workspace, see Issue 4 |

The audit proves the critic targets the **parent's** pod: critic `e7e6971f`'s Service is `workspace-**68372d40**…`, critic `200d8fb4`'s is `workspace-**5c8f6931**…` — i.e. the *parent's* job id, not the critic's own.

## Root cause

1. A scholar freezes for review → `determine_job_status` sets it to **`reviewing`** (critic-enabled twin of `pending_review`).
2. A critic child job is dispatched to verify the deliverables and **SSHes into the parent's live workspace pod** (shared by design, so it can read the parent's `output/`).
3. The lifecycle reconciler ticks. Because `reviewing`/`pending_review` ∈ `_REAPABLE_JOB_STATUSES`, `is_reapable(parent)` is **True** the moment it froze. `is_reapable` inspects only `job_status` — **there is no check for a live child/critic job** referencing the pod.
4. `_reap` runs *dirty + reachable → snapshot → `delete(grace=0)`*. `delete` removes the **pod**; the non-terminal/emptyDir branch leaves the **headless Service** with no endpoints.
5. The critic's next SSH call resolves `workspace-<parent>…` → **no endpoints → `NXDOMAIN`** → `Name or service not known`. Every subsequent tool fails; the critic burns the rest of its run and dies.

The code comment at `workspace_manager.py:34-37` asserts the critic *"reviews out-of-band in its own git workspace, so the parent pod is just as idle/suspendable."* **That assumption is false** — the critic uses the parent's live pod.

**Why intermittent (race):** the parent is reapable *immediately* on freeze, so the outcome depends on whether the reaper tick lands before the critic finishes reading. Research 2's critic (`pending_review`) won the race and completed; the three `reviewing` parents lost. `pending_review` is equally at risk — it just won this time.

**Regression aggravator:** commit `c228ab91` **added `"reviewing"`** to `_IDLE_JOB_STATUSES` (it was only `paused`/`pending_review`/`waiting_for_reply` in `d0547ab2`). `c228ab91` is an ancestor of the deployed `sha-fe10ea6`. Combined with the June switch from *keep-alive-on-snapshot-failure* to active *snapshot-then-delete*, parent pods are now torn down promptly instead of lingering — so a critic that used to (accidentally) survive on a lingering pod now reliably loses the pod.

---

## All issues encountered (this investigation)

### Issue 1 — reaper reaps the parent pod under a live critic *(primary; recurrence of June Bug 1)*
As above. Fix candidates in §Fix.

### Issue 2 — misleading `"VM"` label in the RemoteBackend connect error
`src/core/backends/remote.py:223` hardcodes `f"Failed to connect to VM {host}:{port} …"`, but `RemoteBackend` is the single SSH/SFTP backend for **both** sandbox pods **and** VMs (its own class docstring says so). The container path (`backend: sandbox`, `pod_port or 30022` — `src/api/persistent_app.py:4743-4748`) is distinct from the VM path (`backend: vm`, `vm_ssh_host:22` — `:4720-4728`); nothing here provisioned a VM. **This cost real triage time** ("why did regular scholar jobs get a VM?"). Fix: say "workspace" (or include the backend/tier) instead of "VM". *(Also feeds June's Bug 2: the message lacks the `WorkspaceUnavailableError` token the agent-side watchdog matches on.)*

### Issue 3 — parents left stuck in `reviewing` after the critic dies *(downstream; = June's original finding)*
Research 1/3/4 remain `reviewing` in the UI because their critic failed and nothing un-sticks a `reviewing` job whose critic ended non-`completed`. This re-confirms the downstream gap in `critic_failure_leaves_parent_job_stuck_reviewing.md`. Argues (again) for a **`reviewing`-timeout watchdog** independent of how the critic ended.

### Issue 4 — fresh scholar re-run got an *unseeded* workspace *(secondary; lower confidence, likely separate)*
Research 4 **V2** (`9fb6d213`, a scholar) failed with a **different** signature: its own pod was *reachable* (no `NXDOMAIN`) but **empty** — `task_brief.md` not found, `skills/todo-guide/SKILL.md` not found. The agent wrote itself "workspace-blockers" kb notes and spun ~21 min before failing. Hypothesis (unproven): a force-deleted pod recreated blank by the session/dispatch reconcile safety-net (which ensures a pod *exists* but does not re-seed), or a seeding/auto-redispatch race. Needs its own repro before fixing — do **not** fold into Issue 1's fix blindly.

### Issue 5 — agent doesn't fast-freeze on a dead workspace *(resilience; = June Bug 2/3, still live)*
Critic `e7e6971f` made ~21 tool calls over ~39 min, **all** failing with the same `NXDOMAIN`, instead of freezing cleanly on `workspace_unavailable`. Corroborates that June's Bug 2 (workspace tools flatten `WorkspaceUnavailableError` to a string, so the substring watchdog never fires) and Bug 3 (SSH-exec path can hang) are **still unfixed**. **Fix designed (Tier B):** [`agent_fast_freeze_on_dead_workspace.md`](agent_fast_freeze_on_dead_workspace.md) — type-based propagation to un-flatten (Lever A) + de-nest/classify the connect-retry storm to fail fast on `NXDOMAIN` (Lever B) + the Issue 2 rename.

---

## Fix options (priority order)

1. **Guard the reap: a parent with a non-terminal child (critic) job is not reapable.** In `is_reapable`/`is_idle` (or upstream in `list_instances` metadata), treat a workspace as *needed* while any job referencing it (`parent_job_id = bound` or the shared-container owner) is `created`/`processing`/`paused`. Directly encodes the real dependency. *(This is June's proposed Bug-1 fix #2, still not implemented.)*
2. **Decouple lifetimes: give the critic its own workspace seeded from the parent's snapshot** (at the critic-spawn path — see `unify_scholar_critic_subjob_provisioning.md`). Makes the `workspace_manager.py:34-37` comment actually true; removes the race entirely. Larger change, best long-term.
3. **Cheap stop-gap:** drop `reviewing` (and `pending_review`) from `_IDLE_JOB_STATUSES`/`_REAPABLE_JOB_STATUSES` so review-state parents keep their pod. Reverts the resource optimization but stops the bleeding today; low risk.
4. **Agent-side (Issue 5):** stop flattening `WorkspaceUnavailableError` in `src/tools/workspace/filesystem.py` (let it propagate → clean `workspace_unavailable` freeze), and apply the file-op `connect_timeout`/`max_retries` caps to the SSH **exec** path so git/shell can't wedge. **→ Designed in [`agent_fast_freeze_on_dead_workspace.md`](agent_fast_freeze_on_dead_workspace.md)** (Tier B: type-based propagation via ToolNode + de-nested, cause-classified connect retries; also folds in #6).
5. **Downstream (Issue 3):** `reviewing`/`pending_review` timeout watchdog → flip to `pending_review` (human review) + notify, covering failed/cancelled *and* paused/orphaned critics.
6. **Cosmetic (Issue 2):** fix the `remote.py:223` "VM" string.
