# A pre-job research scholar self-provisions its own workspace, but the inherit-workspace resolver misreads that self-provisioned `context.workspace_container` as an *inherited* one and blocks the scholar for 600s on the parent's never-provisioned workspace


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** Phase 0 `2f71e8b0` + Phase 1 `fef40a4d`, hardened `73fbea49` — flag-gated inheritance + fail-subjob-and-unblock-parent + scholar-identity parent provisioning at HEAD; container-backend scope only, by design.

**Status:** ROOT CAUSE CONFIRMED on live local k3d 2026-07-13 **and observed on
the dev cluster 2026-07-14** (image `sha-c2fbe06`), UNFIXED. Regression
introduced by commit `5a6f5a49` ("Introduce robust workspace inheritance handling
for subjobs", 2026-07-10) — the same change documented in
`subjob_inherits_stale_workspace_container_snapshot.md`. That fix hardened the
*inherit* path but did not distinguish an inheriting subjob from a
self-provisioning one, so it now traps every **root-job pre-research scholar**
whose parent hasn't already provisioned a workspace. This is **not**
k3d-specific: it is deployed cluster code, gated only by a create-time race (see
"Affected scope").

**Motivating incident:** root job **`981b1275-11ae-4646-8ded-082bcecda02f`**
("Calculator", *test's Project*, `runner_kind=user`), local k3d cluster
`k3d-srw` / namespace `srw`. Its pre-job research scholar
**`94e4d0e6-4795-4a33-bf33-23fcd23a43fd`** (`config_name=scholar`,
`runner_kind=lifecycle`) `failed` with:

```
Timed out after 600s waiting for parent job
981b1275-11ae-4646-8ded-082bcecda02f workspace to become ready
(container=None, vm=None).
```

The parent is still stranded in `waiting` with 1 child (the failed scholar) and
never provisioned a workspace of its own.

## TL;DR

For **root jobs**, `_spawn_scholar_subjob` (`orchestrator/main.py:10336`) runs
**synchronously inside `create_job`, before any workspace is provisioned**, and
immediately holds the parent at `status="waiting"` (`main.py:~10426`). A
`waiting` job is not dispatchable (`get_dispatchable_jobs` returns only
`status IN ('created','paused')`, `postgres.py:3385`), so **the parent never
provisions a workspace** while held — it is unblocked (`waiting → created`) only
later, by `_handle_scholar_completion` (`main.py:10503`), when the scholar
finishes.

Because the parent has no workspace at spawn, the scholar inherits nothing
(`parent_ctx.get("vm"/"workspace_container")` are both empty at
`main.py:~10405`), gets `worktree_path=NULL`, and **correctly self-provisions
its own pod** — this is the intended pre-research flow.

The regression: once the scholar's own pod is provisioned, the provisioner
writes it into the scholar's **`context.workspace_container`**. On the next
dispatch tick `_resolve_subjob_inherited_workspace` (`main.py:3465`, added by
`5a6f5a49`) decides "this is an inheriting subjob" **purely from the presence of
`ctx.get("workspace_container")`** (`main.py:~3505`):

```python
own_container = ctx.get("workspace_container") or {}
own_vm = ctx.get("vm") or {}
if not own_container and not own_vm:
    return ("proceed", None)   # <-- only reached BEFORE self-provisioning
```

That signal is **ambiguous** — the same key holds both an inherited *and* a
self-provisioned workspace. So the resolver re-reads the parent's live context
(empty), sees no ready parent workspace, and returns `("wait", …)` every tick
until the `WORKSPACE_INHERIT_MAX_WAIT_S` (default 600s) budget expires
(`main.py:~3589`), then `("fail", …)`. The scholar is failed even though **its
own workspace is `ready`**.

## Evidence (live, 2026-07-13, cluster `k3d-srw`)

### Timeline (orchestrator log, pod `srw-orchestrator-76678b89f9-29hgq`)

| Time (UTC) | Event |
|---|---|
| 07:12:27.99 | `Creating scholar subjob for job 981b1275 (scholar_config=scholar)` — parent set to `waiting` |
| 07:12:29.78 | `Scholar job 94e4d0e6 created for parent 981b1275` |
| 07:12:29.82–.85 | Provisioner: **PVC + Service + container `workspace-94e4d0e6-479` created for job 94e4d0e6** (the scholar's OWN pod) |
| 07:13:28.31 | `Workspace container ready: workspace-94e4d0e6-479 @ 10.42.0.2 (job 94e4d0e6)` |
| 07:13:28.31 | `Dispatcher: subjob 94e4d0e6 waiting for parent 981b1275 workspace to become ready` — **flip happens the same second the scholar's own pod goes ready** |
| 07:13:28 → 07:22:28 | same "waiting for parent workspace" line every 30s |
| 07:22:28.39 | `ERROR … subjob 94e4d0e6 cannot inherit parent workspace — failing: Timed out after 600s …` (age from `created_at`=07:12:28 is exactly 600s) |

The parent `981b1275` produces **zero** dispatch/provision log lines after
creation. `kubectl get pods -n srw | grep 981b1275` → **none**. No parent
workspace pod ever existed.

### DB rows

Parent `981b1275`: `status=waiting`, `assigned_agent_id=NULL`,
`updated_at=07:12:27` (frozen since creation),
`context = {"git_remote_url": …, "kickoff_message": "Please build a simple
calculator app in python."}` — **no `workspace_container`, no `vm`.**

Scholar `94e4d0e6`: `status=failed`, `parent_job_id=981b1275`,
`runner_kind=lifecycle`, `worktree_path=NULL`, and crucially its **own**
context points at its **own** pod:

```json
"workspace_container": {
  "host": "workspace-94e4d0e6-479.srw.svc.cluster.local",
  "port": 30022, "pod_ip": "10.42.0.2", "status": "ready",
  "pod_name": "workspace-94e4d0e6-479", "namespace": "srw",
  "snapshot_attempts": 12
}
```

Contrast with the sibling issue
`subjob_inherits_stale_workspace_container_snapshot.md`, where the scholar and
parent point at the **same** pod (`workspace-4b4b7127-99a`). Here the pod_name
is the **scholar's own** (`workspace-94e4d0e6-479`) — proof it self-provisioned
rather than inherited.

## Root cause

`_resolve_subjob_inherited_workspace` cannot tell a **self-provisioned**
workspace from an **inherited** one, because it keys the decision on the
presence of `context.workspace_container` / `context.vm` — the very keys the
provisioner writes for a workspace the subjob provisioned itself. For a root-job
pre-research scholar (which by design has nothing to inherit and self-provisions),
the classifier flips from `"proceed"` to `"wait"` the moment provisioning
completes, then times out.

A clean discriminator already exists: **`worktree_path` is set only for a true
inheriting subjob** (`main.py:~10486`, set only when
`parent_ctx.get("vm") or parent_ctx.get("workspace_container")`), and is `NULL`
for a self-provisioner. The scholar's `worktree_path` is `NULL` here.

## Secondary bug — failed scholar strands the parent forever

`_handle_scholar_completion` (`main.py:10503`) is designed to unblock the parent
on scholar **failure** too (`is_failure` branch → `merge_job_context({"scholar_failed": True})`
→ `update_job_status(parent, "created")`). But it is only invoked from
`complete_job` (`main.py:13213`) and `cancel_job` (`main.py:7914`). The
dispatch-loop inherit-timeout fails the scholar by calling
`postgres_db.update_job_status(job_id, status="failed", …)` **directly**
(`main.py:~4695`), bypassing `complete_job` — so the unblock never fires and the
parent is left in `waiting` indefinitely (observed: `updated_at` frozen at
07:12:27). Even after the primary bug is fixed, any dispatch-path subjob failure
should route through the parent-unblock handler.

## Affected scope

Any **root job with the scholar/research phase enabled** is at risk, but whether
a given job trips the bug is decided by a **create-time race**. In `create_job`
the parent is written `status='created'` (dispatchable) at `main.py:7485` and is
only flipped to `waiting` later, inside `_spawn_scholar_subjob` (`main.py:~10426`),
with `provision_job_repo` (Gitea) + datasource `await`s in between. Two outcomes:

- **No concurrent dispatch tick fires in that window** (idle/quiet cluster) → the
  parent reaches `waiting` before it ever provisions → the scholar has nothing to
  inherit → **self-provisions → misclassified → 600s timeout** (this bug).
- **A dispatch tick provisions the parent first** (busy cluster) → the scholar
  **inherits** a real parent workspace and dodges this bug — but is then exposed
  to the sibling failure modes (stale-snapshot path in
  `subjob_inherits_stale_workspace_container_snapshot.md`; SSH-connect timeouts).

So it is **load-sensitive, not environment-specific**: it reproduces reliably on
an idle cluster (local k3d, or a dev cluster during a quiet window) and is masked
on a busy one. Backend-agnostic — hits both the container and VM seams.

**Observed incidents:**

| date | env | parent | scholar | note |
|---|---|---|---|---|
| 2026-07-13 | local k3d `k3d-srw` | `981b1275` "Calculator" (`runner_kind=user`) | `94e4d0e6` | full root-cause trace (this doc) |
| 2026-07-14 09:00 UTC | **dev cluster** (`sha-c2fbe06`) | `fdb60a9a` "Design the UI theme…" (project *Better Resavio*) | `36969384`, failed `627s (container=None, vm=None)` | scheduled/cron project kickoff on an otherwise-idle cluster — exactly the race's bug branch |

The dev incident makes the practical impact concrete: **scheduled/project jobs
that kick off during quiet periods reliably strand.** A scan of the 100 most
recent dev failures through 2026-07-12 found zero occurrences (the dev job above
was created 2026-07-14, after that window); note `list_jobs` has no `waiting`
filter, so stranded parents are invisible to it — only their failed scholar
siblings surface.

This is a regression window opened on **2026-07-10** by `5a6f5a49`; the
`waiting`-hold itself predates it (`181d21cf`) and is not the bug. Delegation /
critic subjobs that genuinely inherit a live parent workspace are unaffected
(they legitimately have a parent workspace to wait on).

## Phase 0 — immediate hotfix (stop the bleeding)

**Status: IMPLEMENTED on `develop` (TDD, uncommitted 2026-07-14).** Keeps today's
two-pod design and just disambiguates. Low-risk, small diff, deterministic;
shipped because real project jobs are stranding on the cluster.

1. **Discriminate self-provisioned vs inherited** in
   `_resolve_subjob_inherited_workspace` — gate the inherit path on a dedicated
   `context.inherits_parent_workspace=True` flag rather than on the presence of
   `context.workspace_container` / `context.vm`. A subjob with the flag absent
   returns `("proceed", None)` **before** touching the parent, regardless of
   whether its own workspace snapshot is present. (`worktree_path` is an existing
   near-signal but is only set when Gitea is initialised, so it's a fragile
   discriminator — the explicit flag is used instead.)
   - The flag is stamped **at both spawn sites, only when the parent snapshot is
     actually copied**: `_spawn_scholar_subjob` (`main.py:~10405`) and
     `_trigger_verification_on_complete` (the critic, `main.py:~11192`).
     ⚠️ Correction to the original plan: the critic did **not** set any implicit
     flag — it copied `vm`/`workspace_container` by value exactly like the
     scholar — so it had to be stamped too, or gating on the flag would make the
     critic skip its stale-snapshot overlay (regressing
     `subjob_inherits_stale_workspace_container_snapshot.md`). When the parent
     has no workspace at scholar-spawn (the bug's trigger), nothing is copied, no
     flag is set, and the self-provisioning scholar rides the normal path.
2. **Route dispatch-path subjob failures through the unblock handler** so a
   failed subjob (for any reason) flips its held parent `waiting → created`
   instead of stranding it. New helper `_fail_subjob_and_unblock_parent(job,
   message)` (`main.py`, next to the resolver) marks the subjob failed, syncs
   the in-memory `status`, then calls `_handle_scholar_completion` **and**
   `_handle_delegation_child_completion` (each a no-op for the wrong subjob type)
   — mirroring `complete_job`'s terminal-subjob unblock. The dispatcher `fail`
   branch now calls this helper instead of a bare `update_job_status(..., failed)`.

Both are needed: (1) stops the false failure; (2) is defense-in-depth so no
future subjob-failure path can strand a parent. Phase 0 does **not** change the
resource model (still two sequential pods per job); Phase 1 does.

**Tests (`tests/test_subjob_inherited_workspace.py`, 24 passing):** new
`TestSelfProvisionedDiscrimination` (self-provisioned container/vm, flagless →
`proceed` without consulting the parent) and `TestFailSubjobUnblocksParent`
(failed scholar flips parent → `created`; non-scholar subjob failed without a
spurious unblock). Existing inherit tests were migrated to carry the flag via an
`_inherited(...)` helper, making the inherited-vs-self-provisioned distinction
explicit in the fixtures.

## Phase 1 — proper solution: one parent-owned workspace, shared across the whole job

Phase 0 removes the *symptom*. Phase 1 removes the *ambiguity* by collapsing to a
single workspace per job that every phase rides — treat the field as **the job's
one workspace** that everyone checks before creating one.

### Slice 1 status — IMPLEMENTED + k3d-verified (2026-07-14, `develop`, unpushed)

The **scholar** half of the model is built and live-verified. On an idle cluster
the scholar now provisions the parent's ONE shared pod *under the parent's
identity* instead of a throwaway pod of its own:

- **Spawn** (`_spawn_scholar_subjob`): stamps `context.provisions_parent_workspace
  =<parentId>` on the idle path, gated to container/sandbox backends by
  `_scholar_should_provision_parent_container` (VM/remote and lite fall through).
- **Dispatch seam** (`_job_needs_sandbox` branch): a scholar carrying the marker
  (k8s in-cluster only) runs `_provision_parent_workspace_for_scholar`, which drives
  `ensure_workspace(WorkspaceOwner.job(parentId))`. Because `create_workspace` keys
  the pod name **and** the context write-back on the owner, `workspace-<parentId>`
  is created and its ready status lands on the **parent's** row automatically — no
  copy-back. On ready it *promotes* the scholar (`inherits_parent_workspace` + the
  container + `worktree_path`); the scholar then dispatches via the already-shipped
  inherit path (resolver overlay → `_job_needs_sandbox`=False → worktree injection).
- **k3d evidence** (parent `82aa33f2` / scholar `e7695593`, idle cluster): exactly
  **one** pod `workspace-82aa33f2` (never `workspace-e7695593`); the parent row
  carries that pod as its own `workspace_container`; the scholar was promoted with
  `worktree_path=/home/agent-host/workspace/worktrees/e7695593-scholar` and rode the
  shared pod (agent pod dispatched, `job/start → 202`). The one-workspace-pod
  invariant held for the entire run. Unit coverage:
  `tests/test_scholar_provisions_parent_workspace.py` (12 tests).

**Scope:** container/sandbox backend only. VM/remote and lite parents, and non-k8s
(docker) deployments, keep today's behavior. **No reaper/metering change was
needed** — metering/snapshots already key single-owner on the parent (subjobs never
self-provision), and the scholar→parent handoff is status-safe (parent
`waiting`/`created`/`processing`, none reapable; child-keyed teardown
`workspace-<scholarId>` structurally can't reach `workspace-<parentId>`). The
critic-handoff reaper gap (parent `reviewing`) is the separate, already-filed
`reviewing_parent_pod_reaped_under_critic.md` (Slice 2, not in scope here).

### Principle

One workspace per root job, **owned by the parent** (`workspace-<parentid>`),
used by every phase:

| phase | relationship to the shared workspace | today |
|---|---|---|
| **scholar** (pre-agent research) | **rides it** (SSH + git worktree) | self-provisions its own throwaway pod ← the bug |
| **parent agent** (main work) | runs on it | provisions its own |
| **critic** (post-agent review) | rides it (SSH + git worktree) | already does this |

The scholar becomes **symmetric to the critic** — a phase that rides the
parent's pod, differing only in *when* (before vs after the parent's agent). This
deletes the "inherited vs self-provisioned" distinction entirely: there is only
"the parent's workspace." `_job_needs_sandbox` already returns `False` for any
job that finds a `ready` workspace on its context (`main.py:3401`), and the
dispatch injector already SSHes a subjob into the parent's host at its
`worktree_path` (`main.py:2178`) — so **no second workspace pod is ever created**;
the read-side machinery exists.

### Two pods — don't conflate them

Every running phase is **two** pods, managed by two provisioners:

- **Agent pod** (`agent_provisioner`) — the LLM runtime + tunnel to the
  workspace. **Ephemeral and already correct:** `reap_pods` deletes it
  immediately on completion (`Succeeded/Failed → delete`), and it is only created
  when a phase is dispatched. Agent pods therefore already cycle per phase
  (scholar → parent → critic) and never sit idle — **Phase 1 changes nothing
  here.**
- **Workspace pod** (`container_provisioner`, `workspace-<id>`) — the SSH / code
  workspace. **This is the pod Phase 1 makes persistent and parent-owned.**

So "one persistent pod" means one persistent **workspace** pod, ridden by a
*succession* of short-lived agent pods. The handoff is: the scholar's **agent
pod is deleted** → the workspace pod momentarily has **no agent pod attached** →
the parent's **agent pod is created** and connects to the same workspace pod.
The window to protect from the reaper is exactly that no-agent-pod gap on the
workspace pod — not the (already-correct, always-cycling) agent pods.

### Who provisions the shared workspace, and when

The parent is held in `waiting` at creation and provisions nothing, so the
scholar (dispatched first) has nothing to ride. Two ways to fix that:

- **(B) Parent provisions eagerly, then defers its agent.** Provision
  `workspace-<parentid>` *before* the research wait, keep the parent's agent
  unstarted, let the scholar ride it, start the parent agent only after research.
  Cost: needs a **"provision workspace without dispatching the agent"** path —
  provisioning is currently entangled with agent dispatch.

- **(Hybrid A — recommended) The scholar provisions the shared workspace *under
  the parent's identity* as part of its normal dispatch.** No new provision-only
  path — the dispatcher already provisions on demand. The scholar's dispatch,
  finding the parent's workspace empty, provisions **`workspace-<parentid>`**,
  records it on the **parent's** context, does the git setup (below), and rides
  it. The parent later inherits its own now-ready workspace. Race-free because
  the parent stays held in `waiting` until the scholar completes, so exactly one
  actor ever provisions.

**Ownership is the crux — the pod must be keyed to the parent, not the scholar.**
Teardown is keyed to the *owner*: `_archive_and_cleanup_workspace(owner_id)`
fires at that owner's completion/cancel (`main.py:4007`, `7623`, `7762`). A pod
owned by the scholar (`workspace-<scholarid>`, as today) is torn down the instant
the scholar completes — the parent would inherit nothing. Keyed to the **parent**,
scholar completion tears down *nothing* it owns, and the pod survives naturally
into the parent and critic phases. This is what makes the hybrid safe.

### Git / setup model (done by whoever provisions first — the scholar; not agent work)

Workspace init is orchestrator/git work, not agent work, so the first phase to
run performs it:

1. clone/init the parent's Gitea repo; ensure **`main`** carries the parent job's
   **initial content** (kickoff message / document);
2. branch the scholar's worktree off `main`
   (`worktree_path=/home/agent-host/workspace/worktrees/<id>-scholar`);
3. scholar writes findings to `research/`, commits on its branch;
4. on scholar completion, **merge `research/` → `main` locally on the shared pod**
   — the current cross-repo graft becomes a local merge, no Gitea round-trip;
5. the parent inherits the pod, works on `main` with research already present,
   and starts its agent.

Note: the shared workspace's **backend = the parent's backend**. A VM-backed
parent means research runs on the VM (the scholar conforms to the parent's
config); there is no separate light research container.

### Reaper / lifecycle (the missing guard — also fixes the critic bug)

This concerns the **workspace pod** teardown, not the agent-pod reaper (which is
already liveness-based — heartbeat/phase/tunnel). Confirmed against the code: the
workspace pod is released by `_archive_and_cleanup_workspace(owner_id)`, keyed to
the **owner job's lifecycle**, with **no "is an agent pod attached" guard** (no
in-use guards exist in `main.py`). That is exactly the existing
**critic-reaps-parent** hazard (`critic_failure_leaves_parent_job_stuck_reviewing.md`):
the workspace pod is torn down on the owner's *status* while a subjob's agent pod
is still attached to it. The fix is shared with that bug:

1. **Tear the workspace pod down by agent-pod liveness, not owner status.** Never
   release a workspace pod that has a **live agent pod attached — scholar, parent
   or critic.** One rule serves both this feature and the critic-reaps-parent bug.
2. **Protect the handoff gap.** There is a window where the workspace pod has *no*
   agent pod attached — the scholar's agent pod is deleted, then the parent's
   agent pod is created and reconnects. Guard it with a short grace period and/or
   an explicit **`reserved-for:<parentid>` lease** so the reaper can't grab the
   workspace pod in that gap; clear the lease when the next agent pod attaches (or
   at final teardown).
3. **One persistent workspace pod across scholar → parent → critic.** Do **not**
   release/reprovision the *workspace* pod between phases — that reintroduces a
   second workspace pod and defeats the model. (Agent pods, by contrast, are
   *meant* to cycle per phase; only the workspace pod persists.)

### What Phase 1 dissolves

- the **misclassification** — gone by construction (no self-provisioned scholar
  workspace to misread);
- the **stranded parent on scholar failure** — gone: the parent already owns a
  ready workspace, so a failed scholar just means "start the parent agent now"
  (the Phase 0 unblock becomes a non-event);
- the **critic-reaps-parent** bug — folded into the reaper hardening.

### Open questions for detailed design

- **Atomic provision-under-parent** — the scholar's dispatch provisions
  `workspace-<parentid>` and records it on the parent atomically, so if the
  `waiting` hold is ever relaxed there's still no check-then-create double
  provision (DB compare-and-set on `parent.context.workspace_container`).
- **Handoff-protection mechanism** — grace-period vs. explicit lease; TTL; who
  clears it; crash-safety (scholar dies mid-handoff).
- **Backend conformance** — is running research on a (possibly heavy / VM) parent
  workspace acceptable in every tier, or is there a case that needs an isolated /
  cheap research env (which would argue for keeping two pods)?
- **Metering & snapshots** — `workspace_metering` and S3 snapshot/suspend key off
  the owner; confirm single parent-ownership across phases doesn't double-count
  or mis-attribute the research phase.
- **Lite / virtual tiers** — jobs with `backend ∈ LITE_BACKENDS` have no pod at
  all; the scholar handoff must no-op there.

## Repro

Create any root job with scholar enabled on a cluster where the parent has no
pre-existing workspace (the normal case). The scholar self-provisions, then fails
~600s later with `container=None, vm=None`, and the parent stays in `waiting`.

## Key references

- `orchestrator/main.py:3465` — `_resolve_subjob_inherited_workspace` (classifier + 600s wait)
- `orchestrator/main.py:3462` — `_INHERIT_WORKSPACE_MAX_WAIT_S` (`WORKSPACE_INHERIT_MAX_WAIT_S`, default 600)
- `orchestrator/main.py:~4677` — dispatcher call site + `fail` branch (`~4695`)
- `orchestrator/main.py:10336` — `_spawn_scholar_subjob` (root-job pre-research spawn; `waiting` hold; inherit-copy; `worktree_path`)
- `orchestrator/main.py:10503` — `_handle_scholar_completion` (parent unblock, incl. `is_failure`)
- `orchestrator/main.py:3401` — `_job_needs_sandbox` (returns `False` when a `ready` parent workspace is on context → **no second pod**; the shared-pod read-side)
- `orchestrator/main.py:2178` — dispatch injector (`remote["workspace_path"] = worktree_path`; SSH host = parent's pod — proves inherit = shared pod, not clone)
- `orchestrator/main.py:4007` — `_archive_and_cleanup_workspace(owner_id)` (teardown keyed to owner-job lifecycle; **no agent-liveness guard** — the reaper gap Phase 1 closes)
- `orchestrator/database/postgres.py:3385` — `get_dispatchable_jobs` (`status IN ('created','paused')`)
- Sibling: `docs/issues/subjob_inherits_stale_workspace_container_snapshot.md`
- Sibling pattern (failed subjob strands parent / reaper reaps pod under live subjob): `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md`
