---
tags:
  - issue
  - jobs
  - critic
  - workspace-lifecycle
  - lifecycle-reaper
  - agent-resilience
---

# Failed/cancelled critic leaves the parent job stuck in `reviewing` (and its workspace pod alive)

**Filed:** 2026-06-12, found during a zombie-workspace sweep on dev.

> **2026-06-22 update:** a second incident (`d9d11992` / critic `772a4cc1`)
> exposed the *upstream* cause — the critic shares the parent's workspace
> container and the lifecycle reaper pulls it mid-review — plus two agent-side
> resilience bugs that turned a recoverable blip into a dead, wedged job. See
> **"2026-06-22 — upstream root cause + agent-side resilience gaps"** below.
> The original section stays as the record of the *downstream* verdict-handler
> gap. (Line numbers in the original section have drifted; current symbols are
> named in the new section.)

## Symptom

Jobs sit in `status='reviewing'` indefinitely, and because `reviewing`
is non-terminal, their workspace pods are never torn down. On dev:

| Job | Config | Stuck since | Critic subjob | Workspace pod |
|---|---|---|---|---|
| `6ffd0c16` | bughunter | 05-21 | (cron-tick test artifact) | already gone |
| `4486f28a` | scholar ("Research 03") | 06-04 | `8a3fc7d1` **cancelled** 06-03 | `workspace-4486f28a`, **9 days old** |
| `abc15bd5` | developer | 06-12 00:31 | `826e5bfe` **failed** 06-12 02:03 | `workspace-abc15bd5` |

## Root cause

`_handle_critic_verdict` in `orchestrator/main.py` (~line 7608) only
un-sticks the target when the critic **completed**:

- critic completed *with* verdict → approve / return-with-feedback path
- critic completed *without* verdict → implicit approval (the existing
  "doesn't get stuck in reviewing" safeguard)
- critic **failed or cancelled** → `return` — the parent stays in
  `reviewing` forever, no retry, no fallback, no notification

Nothing else ever transitions a `reviewing` job. (It *is* still
manually actionable — the approve endpoint accepts
`pending_review`/`reviewing` — but nothing surfaces that the review
pipeline died.)

## Effects

- Job appears perpetually "in review" in the cockpit.
- Workspace pod runs forever (the lifecycle honors non-terminal jobs),
  eating a pool slot + node resources — the 9-day-old pod above.

## Proposed fix

On critic terminal failure/cancellation, in `_handle_critic_verdict`
(or the completion service):

1. Flip the target job back to `pending_review` (human review takes
   over where automated review died) and emit a notification, or
   respawn the critic with a bounded retry count before falling back.
2. Optionally: idle-suspend workspaces of jobs that have been in
   `reviewing`/`pending_review` beyond a threshold — review only needs
   the Gitea branch, not a live pod.

---

# 2026-06-22 — upstream root cause + agent-side resilience gaps

**Found:** 2026-06-22 on dev (ns `superhuman-remote-worker`), investigating a
job sitting in `reviewing`. Symbols/line numbers below are current as of this
date (`develop`); the original section's numbers predate later drift.

## Incident

| Job | Role | Status | Notes |
|---|---|---|---|
| `d9d11992-9eef-4df4-8a07-be8d9c19cd61` | scholar ("Research 04: verify-before-done") | **`reviewing`** | froze `job_complete` 18:29:07Z, confidence 90 %, all deliverables present |
| `772a4cc1-86e8-4487-a7fd-506a9c039df4` | critic (verification subjob) | **`paused`** | 28 audit steps; workspace lost mid-run, then orphaned |

The deliverables were fine. The critic died on pure infra, and — crucially —
ended up **`paused` (orphaned)**, *not* failed/cancelled, so it never reached
the verdict handler the original section describes. The parent is stuck anyway.

### Timeline (all 2026-06-22 UTC)

- **17:55:11** — parent dispatched, `injected workspace container config …
  host=10.42.2.63:30022` → parent runs on a **sandbox** container (`10.42.x.x`
  is the pod CIDR; this is a container, not a VM).
- **18:29:07** — parent froze for review (`job_complete`).
- **18:29:15–18** — `_trigger_verification_on_complete` spawns critic
  `772a4cc1`; dispatched with `injected workspace container config …
  host=10.42.2.63:30022` — the **same container as the parent** (shared by
  design, so the critic can read the parent's `output/`).
- **18:30:10** — `Workspace container deleted: workspace-d9d11992-9ee` +
  `Lifecycle tick kind=workspace {'reaped': 1}`. The reaper tore down the
  shared container ~52 s after the parent left `processing` — **while the
  critic was actively using it**.
- **18:30→18:51** — every critic workspace call now fails/hangs (audit steps
  19–28). Agent keeps heartbeating but does no real work.
- **~18:54 → 18:55** — agent marked offline → critic orphaned → re-dispatched;
  recovery sets `context.vm.requested=true, recovering=true` and
  **auto-provisions a fresh VM** (the only real VM in this whole story).
- **19:01:12** — recovery VM never became reachable (`snapshot_attempts:5`);
  lifecycle reaper `force-deleted dirty unreachable instance kind=vm`. Critic
  settles `paused`. Parent stuck `reviewing`.

DB ground truth (`jobs` table): critic `config_override.workspace` is **null**
(no VM in its config — it `$extends: defaults` → `backend: sandbox`);
`context.workspace_container = {status, pod_ip:10.42.2.63}` (inherited from
parent); `context.vm` was written later by recovery.

## Three stacked bugs

### Bug 1 (root) — the reaper kills a workspace a live subjob is sharing
- `_trigger_verification_on_complete` (`orchestrator/main.py:9185-9195`) copies
  the parent's `context.workspace_container` (or `vm`) into the critic so it
  shares the parent's pod.
- Container ownership is **single-job** — teardown releases
  `WorkspaceOwner.job(parent_id)` (`main.py:3201-3215`), and the lifecycle
  reconciler reap (see `main.py:753`) reaps the parent's container once the
  parent leaves `processing`. **No check for a live child job** referencing the
  same container.
- The `/complete` handler itself skips teardown unless status is
  `completed`/`failed` (`main.py:9627`) — the parent went to `reviewing`, so
  this was purely the reconciler reaper.

### Bug 2 (cheapest, highest-leverage) — workspace-loss watchdog defeated by a class-name string match
- There **is** a safety net: `graph.py:3547-3559` re-raises
  `WorkspaceUnavailableError` (→ clean freeze / recovery) **iff** a tool result
  contains the substring `"WorkspaceUnavailableError"`.
- But the filesystem tools catch the exception and return
  `f"Error writing file: {str(e)}"` (`src/tools/workspace/filesystem.py:296-298`
  and the sibling `except Exception` blocks). `str(WorkspaceUnavailableError(…))`
  is the *message* (`"Failed to connect to VM 10.42.2.63:30022 after 5
  attempts: …"`), which **does not contain the class name** → the watchdog's
  substring check never matches → the net never fires.
- The backend raises the right exception (`src/core/backends/remote.py:222,313`)
  and `agent.py:888-908` has a clean `workspace_unavailable` freeze path — both
  require the exception to *propagate*, which the swallow-to-string prevents.
- Audit proof: step 24 = `Tool [ok] write_file: Error writing file: Failed to
  connect to VM …` — marked **`[ok]`**, class name gone.

### Bug 3 — `git_status` (SSH exec path) hangs instead of failing fast
- File ops fail fast: `RemoteBackend(connect_timeout=30, max_retries=5)`
  (`remote.py:109-110`) → the observed "after 5 attempts" errors.
- Git/shell go through `exec_command` / `_exec` (`remote.py:185,294`); on
  connection loss `_exec` raises (`:313`) and a wrapper returns
  `"SSH connection lost during command execution: …"` (`:897-898`). But audit
  step 28 (`git_status`) **never returned at all** — it hung in
  connect/reconnect, wedging the agent ~21 min (heartbeats 18:30→18:51). This
  is the operator-visible "`git_status()` still executing".
- Side note: that `"SSH connection lost during command execution"` string also
  lacks the `WorkspaceUnavailableError` token, so it would bypass Bug 2's net too.

### Downstream (original section) — and it's broader than "failed/cancelled"
Even after the critic dies, nothing un-sticks the parent. Here the critic is
**`paused` (orphaned)** — it never reaches `_handle_critic_verdict_on_complete`
(`main.py:8940`) at all, so the original proposed fix (keyed on critic
*terminal* failure/cancel) wouldn't catch this case either. Argues for a
**`reviewing`-timeout watchdog** independent of how the critic ended.

## Expanded fix (priority order)

1. **Bug 2 — stop flattening `WorkspaceUnavailableError`** in the workspace
   tools (let it propagate), or match the watchdog on the failure *signature*
   rather than the class-name substring. Cheapest change; on its own it would
   have turned this whole incident into a clean agent freeze
   (`freeze_type=workspace_unavailable`) + orchestrator re-seed.
2. **Bug 1 — don't reap a workspace with a live verification subjob:** skip the
   reap (or transfer ownership to the critic) while a child job referencing the
   container is `created`/`processing`/`paused`; **or** give the critic its own
   workspace seeded from the parent's snapshot so lifetimes decouple. Overlaps
   with [`unify_scholar_critic_subjob_provisioning.md`](unify_scholar_critic_subjob_provisioning.md)
   (the critic-spawn path is the place to do it).
3. **Bug 3 — apply the file-op fail-fast caps** (`connect_timeout`/`max_retries`)
   to the SSH **exec** path so git/shell tools can't wedge the agent on a dead
   host.
4. **Downstream — `reviewing` watchdog:** timeout-based flip to `pending_review`
   + notification, covering `paused`/orphaned critics, not just failed/cancelled.
   Also: workspace **recovery should re-provision the same tier**, not escalate a
   *sandbox* job to a VM — the VM jump here landed on dev's flaky VM tier and
   turned a transient container loss into a dead end.
