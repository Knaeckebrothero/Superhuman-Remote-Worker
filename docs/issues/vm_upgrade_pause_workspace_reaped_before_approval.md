---
tags:
  - issue
  - jobs
  - workspace-lifecycle
  - sudo-gate
  - vm-upgrade
  - remote-backend
  - reaper
---

# A job paused for VM-upgrade approval gets its workspace reaped before the operator can approve

**Status:** investigated 2026-07-08 from a live incident on the main cluster
(`superhuman-remote-worker`). Root cause **confirmed in code**. Not yet fixed.
**Severity:** high — the job is unrecoverable and the loss is silent. Any job that
hits the sudo/VM-upgrade gate and isn't approved within the reaper's idle window
(minutes) dies, even though the approval request itself is valid for **24 h**. For
VM-backed / non-suspendable workspaces the agent's work is destroyed (only
phase-boundary git commits survive).
**Component:**
- `orchestrator/services/lifecycle/workspace_manager.py` (`_IDLE_JOB_STATUSES`,
  `is_idle`, `is_reapable`)
- `orchestrator/services/lifecycle/reconciler.py` (idle → snapshot/drain tick)
- `orchestrator/services/completion.py` (`vm_upgrade_required` → `paused`)
- `orchestrator/services/sudo_gate.py` (`insert_vm_upgrade_request`, 24 h TTL)
**Observed on:** main cluster, job `b5b0c0d0-e7b4-403b-a143-dd8d9f98ae51`
("project management software", `default` config, `gemma-4-moe`), sudo request
`98d88547`, workspace `workspace-b5b0c0d0-e7b` / VM `agent-vm-b5b0c0d0-e7b`.

---

## TL;DR

The sudo gate freezes a job as `vm_upgrade_required` and pauses it so a human can
decide whether to upgrade it to a VM. That approval request is deliberately given a
**24 h TTL** — "operator decides in their own time" (`sudo_gate.py:588`). But the
lifecycle reaper classifies **`paused` as an idle, drainable/reapable state**
(`workspace_manager.py:53`), and nothing in the reaper checks for an open sudo/VM
approval. So the reconciler drains the paused workspace on a normal idle tick —
minutes to ~an hour later — **long before the 24 h approval window closes**.

By the time anyone approves (or hits Resume), the workspace is gone. Resume tries to
reconnect to the recorded workspace endpoint, gets `Name or service not known`,
exhausts `WORKSPACE_RECOVERY_MAX_ATTEMPTS`, and flips the job to `failed`
(`workspace_unavailable`). The sudo request is still `PENDING` — it was never
honoured, because there was nothing left to upgrade.

Two independent aggravating defects showed up in the same incident:
1. the requesting user **lacked VM-backend permission**, so even a timely approval
   would have been rejected by the capability check — and nothing surfaced that;
2. Cockpit offered **Resume** (not Approve/Deny) as the action for a sudo-gated
   pause, and Resume against a reaped workspace is what finally consumed the job
   into `failed`.

## Timeline (observed, job `b5b0c0d0`)

| Time (UTC, 2026-07-08) | Event |
|---|---|
| 08:24:01 | Job created (`default`, `gemma-4-moe`). Its scholar research subjob `f8326e32` runs and completes; output exported to OpenCloud (`sessions/job-f8326e32a3ea`). |
| 09:39–09:54 | Agent works phases 4–8. Phase boundaries committed to Gitea repo `job-b5b0c0d0` — 20 commits, last `04b82c19` "[Phase 8 Strategic] Complete" at **09:54:30**. |
| 09:55:58 | Agent runs `sudo docker --version` in its (non-VM) workspace. |
| 09:56:08 | **Sudo gate fires** → freeze `vm_upgrade_required` → job `paused`; `sudo_approval_requests` row `98d88547` inserted (`vm_upgrade`, 24 h TTL). Last audit entry: *"paused while the operator decides whether to upgrade this job to a VM environment."* |
| ~09:56 → ~15:47 | **Zero job activity for ~6 h.** Workspace reaped during this window; `context.vm.status` → `deleted`, DNS for `workspace-b5b0c0d0-e7b…svc.cluster.local` no longer resolves. |
| ~15:47:42 | Operator hits **Resume**. Orchestrator tries to reconnect → `Failed to connect to workspace … [gone]: [Errno -2] Name or service not known`; recovery exhausted after 3 attempts → job `failed` (`freeze_type: workspace_unavailable`, `recovery_attempts: 4`). |

The sudo request `98d88547` remained `PENDING` throughout — it never registered an
approval, and its 24 h TTL had not yet expired, so the system's own view still said
"awaiting operator decision" for a workspace that had been dead for ~5 h.

## Root cause (confirmed in code)

**A TTL mismatch between the approval window and the reaper's idle window, with no
guard linking them.**

### 1. The pause is legitimately long-lived
`completion.py:577` maps the gate freeze to a paused job:
```python
if freeze_type == "vm_upgrade_required":
    return ("paused", None)
```
`sudo_gate.insert_vm_upgrade_request` (`sudo_gate.py:577`) records the decision with
a **24 h** TTL, by design:
```
… "these have no reply subject and use a long TTL (24h — operator decides in their own time)."
INSERT INTO sudo_approval_requests (… request_type, ttl_seconds, expires_at)
VALUES (…, 'vm_upgrade', 86400, NOW() + INTERVAL '86400 seconds')
```

### 2. The reaper treats that same pause as disposable
`workspace_manager.py:53`:
```python
_IDLE_JOB_STATUSES = frozenset(
    {"paused", "pending_review", "reviewing", "waiting_for_reply"}
)
…
_REAPABLE_JOB_STATUSES = _IDLE_JOB_STATUSES | _TERMINAL_JOB_STATUSES
```
`is_idle()` / `is_reapable()` return `True` whenever `job_status ∈` those sets. The
reconciler tick (`reconciler.py:185-210`) then runs `snapshot()` → `drain()` (a
pod delete) on the idle instance. The **only** guards are:
- `has_live_shared_child` — a critic SSHed into this pod (irrelevant here), and
- `bound_row_missing` — the deleted-job orphan age gate (irrelevant here).

**There is no check for an open, unexpired sudo/VM-upgrade request** bound to the
job. A job paused *specifically so a human can decide within 24 h* is drained on the
same schedule as any quiescent paused job — i.e. as soon as the idle/drain window
elapses (minutes to ~1 h, well under 24 h).

### 3. The drain is fatal for this workspace class
The reap is designed to be state-safe for suspend-capable, PVC-backed workspaces
(`snapshot()` before `drain()`, PVC retained because `paused` is non-terminal → later
reattach + checkpoint resume). But this job's workspace was VM-tier / non-reattachable
(there is no persistent VM rootdisk yet — see
[`../features/vm_persistent_rootdisk.md`](../features/vm_persistent_rootdisk.md)),
so the drain destroyed the live filesystem. Only the
phase-boundary git commits in `job-b5b0c0d0` survived. Even where a snapshot *does*
survive, it does not rescue this flow: the operator's approval wants to **upgrade the
original live workspace in place**, and that workspace no longer exists.

### 4. Resume can't recover, and reports the wrong thing
On Resume the recovery arm reconnects to the recorded endpoint, fails NXDOMAIN,
retries to the cap, and fails the job as `workspace_unavailable` — a generic
"workspace vanished mid-run" message that gives the operator no hint that **their own
still-open approval request is the thing that got stranded**. See the sibling
resilience spec [`agent_fast_freeze_on_dead_workspace.md`](agent_fast_freeze_on_dead_workspace.md)
(the freeze *type* is
correct; the problem is that the workspace should never have been reaped while an
approval was pending).

## Secondary defects surfaced by the same incident

- **An approval the requester can't satisfy is still raised to a human.** The job's
  creator was an **admin** but had no VM-backend grant — the grant system was added
  after he became admin and he didn't know he had to self-assign VM in Grants.
  Approving the VM upgrade would have been rejected by the capability check anyway, yet
  the approval email ("Approve a VM upgrade or reject") gave no signal that the request
  was unsatisfiable. → fixed by **directions 4 (auto-deny)** and **5 (admins bypass
  grants)**.
- **Resume is the wrong affordance for a sudo-gated pause, and the job list hides the
  wait.** Cockpit (and the email deep-link) surfaced **Resume**, not Approve/Deny —
  and the STATUS column showed no sign an approval was pending (the screenshotted row
  reads `Created` with a green Resume button). Resume does not approve the request;
  against a live workspace it just re-enters the gate, and against a reaped one it
  destructively consumes the job into `failed`. → fixed by **direction 6**.
- **`PENDING`-forever display.** Because the request TTL is 24 h, `list_sudo_requests`
  keeps showing `PENDING` even after the workspace it depends on is gone — the operator
  cannot tell from the request that approving it is now hopeless. → addressed by
  **directions 1 (don't reap under an open request)** + **3 (expire loudly)**.

## Fix directions

Primary (fixes the class):

1. **Don't reap a workspace that has an open, unexpired sudo/VM-upgrade approval.**
   Add a guard to `is_idle`/`is_reapable` (mirroring `has_live_shared_child`): when the
   bound job has a `pending` row in `sudo_approval_requests` whose `expires_at` is in
   the future, the workspace is **not** drainable. Surface this as an
   `awaiting_approval` metadata flag set alongside `job_status` so the predicates stay
   cheap. This makes the workspace lifetime track the approval TTL instead of the idle
   timeout.
2. **Bound the approval window to what the workspace can survive.** If a 24 h TTL is
   the intended UX, the workspace must be either (a) held for up to 24 h, or (b)
   snapshot+suspended and reliably restorable on approval — including for VM-tier jobs
   (needs the persistent-rootdisk work). Pick one; today it silently does neither.
3. **On expiry, fail loudly and specifically.** When a `vm_upgrade` request expires
   (or its workspace is confirmed gone), transition the job with an explicit
   `vm_upgrade_expired` reason — "the approval window closed; re-run the job" — not the
   generic `workspace_unavailable`.

Secondary — capability model + UX (decided 2026-07-09):

4. **Auto-deny an approval the requester can never satisfy — don't ask a human.** At
   gate time, resolve the requesting user's capabilities. If they do **not** hold the
   capability the agent is asking for (e.g. no VM grant), there is no decision for the
   operator to make: **auto-deny immediately** and let the job continue *without* the
   privilege — the agent receives a denied tool result and adapts (or freezes on its
   own terms) — rather than pausing indefinitely on an unanswerable request. Only raise
   an approval to a human when the user *could* actually grant it. (This also removes
   the "workspace reaped under a request that could never be honoured" case entirely for
   ungrantable users.)
5. **Admins bypass grants (or are seeded with max-level grants).** The root trigger
   here was that the requester was an **admin** but still had no VM grant — the grant
   system was introduced *after* he became admin, and he didn't know he had to open
   Grants and self-assign VM. An admin should never be blocked by a capability they can
   grant themselves. Treat `admin` as holding **all** capabilities (short-circuit the
   grant check), or seed admins with max-level grants by default and **backfill existing
   admins on migration** so no admin is silently missing a grant added later.
6. **Make the job list tell the truth, and gate Resume behind the approval.** A job
   blocked on a sudo/VM-upgrade request must show that in the **STATUS column** (e.g.
   `Waiting · approval`) instead of a bare `Paused`/`Created` (see screenshot — the row
   shows `Created` + a green **Resume** button with no hint an approval is pending). For
   such a job, **replace the Resume action with an "Approve request" button that
   deep-links to the pending request in the notification center**, so the operator
   *cannot* Resume without first approving or denying it. Resume must be unreachable for
   an approval-blocked job; the approval email's deep-link should also land on that
   Approve control, not the job's Resume button.

## Recovery for this incident

Not resumable — the workspace is gone and the failure is a destroyed VM workspace, not
a permission denial, so granting VM permission (already done) does not un-fail it. The
Phase-8 work **is** preserved in Gitea repo `job-b5b0c0d0` (20 commits through
`04b82c19`), and the research subjob output is in OpenCloud
(`sessions/job-f8326e32a3ea`). Re-run as a **fresh VM-backed job**, optionally seeded
from the `job-b5b0c0d0` repo so the prior phases aren't redone. Clean up the orphaned
`PENDING` request `98d88547` (deny or let it expire).

**Operational caveat (2026-07-08):** VM jobs on the main cluster are currently wedged
independently by the orchestrator-vantage SSH-readiness gate (`96c33654`, live via
`sha-69a4da6`) — the orchestrator has no tailnet route so the probe can never pass. A
"re-run on VM" recovery will not actually dispatch until that is resolved. Track
separately (VM SSH-readiness saga).

## Related

- [`deleted_job_orphans_workspace_pod.md`](deleted_job_orphans_workspace_pod.md) — the
  *inverse* failure (deleted-row pods that never get reaped); shares the
  `is_idle`/`is_reapable` predicates edited here.
- [`agent_fast_freeze_on_dead_workspace.md`](agent_fast_freeze_on_dead_workspace.md) —
  make workspace loss survivable/quick-to-freeze; the recovery consumer this incident
  lands in.
- [`loop_job_workspace_lost_wedged_in_recovery.md`](loop_job_workspace_lost_wedged_in_recovery.md)
  — the recovery arm that fails after `WORKSPACE_RECOVERY_MAX_ATTEMPTS`.
- [`reviewing_parent_pod_reaped_under_critic.md`](reviewing_parent_pod_reaped_under_critic.md)
  — origin of the `has_live_shared_child` reap guard; a new "pending-approval" guard
  should follow the same pattern.
- `docs/features/workspace_tier_upgrade.md` — the VM-upgrade design this gate implements.
