# Subjobs inherit a by-value snapshot of the parent's workspace_container — a scholar spawned mid-provisioning captures `status:"created"` (no SSH host) and hard-fails at workspace init

**Status:** OPEN — root cause confirmed on live dev 2026-07-10, fix designed, not implemented.
**Motivating incident:** scholar subjob `4de67cda-13e8-47f6-b076-05b87c03bfe7`
(kickoff for parent `4b4b7127-99a8-4e72-be3c-cedf767f5f09`, "Alternative
Software zu MS Projekt"), dev cluster `main` / namespace
`superhuman-remote-worker`, image `sha-194cdf2`. Job `failed` with **0 audit
entries, 0 requests, 0 tokens**, no log file, no frozen data;
`error_message`:

```
workspace.backend='sandbox' but no workspace.remote config was provided.
The orchestrator must inject SSH credentials pointing at a provisioned
workspace container or VM.
```

## TL;DR

`_spawn_scholar_subjob` runs **~3 seconds after the parent job is created**
(`main.py:6674`, inside `create_job`) and copies the parent's
`context.workspace_container` **by value** (`main.py:9518`). At that instant
the parent's workspace pod is still `status:"created"` with **no
`host`/`pod_ip`/`port`** — the ready coordinates are written later, only when
`_wait_for_ready` resolves (`container_provisioner.py:336-344`, ready-wait up
to 120s). That stale snapshot lands in the scholar's **own** job row and is
**never refreshed** when the parent pod becomes ready. Every downstream
decision keys off the scholar's frozen copy:

- `_job_needs_sandbox` (`main.py:3118`) doesn't see `status=="ready"`, so it
  doesn't treat the scholar as inheriting a ready workspace.
- the dispatch-time injector (`main.py:1982-1984`) requires
  `status=="ready" and host` before it writes `workspace.remote` — so it
  **never injects SSH credentials**.
- the agent is dispatched with the default `backend='sandbox'`
  (`src/core/loader.py:1412`) and an empty `workspace.remote`, and raises at
  `init_workspace` (`src/agent.py:1848-1854`).

The parent is **not** broken: the scholar failure is caught and the parent is
"unblocked without research" (`main.py:9660`, sets `context.scholar_failed`),
so the job runs anyway — just **without its research phase**. The cost is
silent quality degradation on **every fresh sandbox-backed loop/research
job**, not a visible crash.

The identical copy-by-value exists in the **verification/critic** path
(`main.py:10304`, inside `_trigger_verification_on_complete`) and for the
**VM** backend in both paths (`main.py:9516`, `10302`). See "Two instances +
VM parity" below.

## Evidence (live, 2026-07-10)

Both job rows point at the **same pod** (`workspace-4b4b7127-99a`), but the
snapshots diverge — the parent's advanced to ready, the scholar's is frozen:

| field | parent `4b4b7127` | scholar `4de67cda` |
|---|---|---|
| `workspace_container.status` | `ready` | `created` |
| `host` | `workspace-4b4b7127-99a.…svc.cluster.local` | **absent** |
| `pod_ip` / `port` | `10.42.3.218` / `30022` | **absent** |
| `pod_name` | `workspace-4b4b7127-99a` | `workspace-4b4b7127-99a` (same) |

`config_override.workspace` and `resolved_config.workspace` on the scholar are
both empty (no backend explicitly set → defaults to `sandbox`).

Orchestrator log, final chapter (agent `223ad76e`, which died the same
second — heartbeat stopped `16:28:04`):

```
16:27:39  POST /api/jobs/4de67cda/ensure-workspace-access 200
16:27:51  POST /api/jobs/4de67cda/ensure-workspace-access 200
16:28:04  POST /api/jobs/4de67cda/resume 200
16:28:04  Job 4de67cda status set to 'failed'
16:28:04  Scholar 4de67cda failed — unblocking parent 4b4b7127 without research   (main.py:9660)
16:28:04  POST /api/jobs/4de67cda/complete 200
```

**Timeline.** created `12:46:12` (3s after parent `12:46:09`) → sat undispatched
~3.7h (its own snapshot never reaches `ready`, so `_job_needs_sandbox` keeps
it looking like it needs a workspace that will never provision on its row,
while the loop agent pool is busy) → finally assigned to agent `223ad76e`
~`16:27` → hard-fail at `init_workspace` `16:28:04`. Elapsed 229 min, all of
it wasted.

## Current behavior (code anchors)

- **Scholar copy-by-value** — `orchestrator/main.py:9508-9518`, inside
  `_spawn_scholar_subjob` (def `:9446`):
  ```python
  parent_ctx = job.get("context") or {}
  ...
  if parent_ctx.get("vm"):
      scholar_context["vm"] = parent_ctx["vm"]                       # :9516
  elif parent_ctx.get("workspace_container"):
      scholar_context["workspace_container"] = parent_ctx["workspace_container"]  # :9518
  ```
  Called from `create_job` at `:6674` → runs at **job-creation** time, before
  the parent pod is ready. `worktree_path` is set to
  `…/workspace/worktrees/{short_id}-{scholar_config}` (`:9592`), i.e. the
  scholar is designed to run **inside the parent's pod** via a git worktree —
  it never needs its own container.
- **Provisioner status lifecycle** — `container_provisioner.py`: pod created →
  `{"status":"created", pod_name, namespace}` (`:319`); after ready-wait
  (`:336`) → `{"status":"ready", "pod_ip":…, "port":30022, "host":…}`
  (`:338-344`); on timeout → `{"status":"creating"}` (`:403`). The `host`/IP
  only ever exist on the row whose provisioner ran the wait — the **parent's**.
- **`_job_needs_sandbox`** — `main.py:3116-3119`: returns `False` (skip
  provisioning, inherit) only when the job's **own**
  `workspace_container.status == "ready"`. The scholar's `"created"` snapshot
  falls through to `:3138` → thinks it needs its own sandbox.
- **Dispatch injector** — `main.py:1982-1990`: reads the job's **own** context
  via `_get_container_context(job)` (`:3141-3149`) and only injects
  `workspace.remote` when `container_ctx.get("status") == "ready" and
  container_host`. No fallback to the parent's live context for a subjob.
- **Agent hard-fail** — `src/agent.py:1848-1854`: `if not
  self.config.workspace.remote: raise RuntimeError(...)`. There is no
  workspace, so nothing runs; hence 0 tokens / 0 audit.
- **Parent recovery** — `_handle_scholar_completion` (`main.py:9612`) sets
  `context.scholar_failed=True` and transitions the parent out of `waiting`
  (`:9660`). Confirmed working: the parent is `processing` normally.

## Two instances + VM parity

1. **Scholar (ACTIVE bug).** Spawns at parent **creation** (`_spawn_scholar_subjob`,
   from `create_job`), i.e. squarely inside the provisioning window → hits the
   race reliably. This is the incident.
2. **Verification / critic (LATENT).** Same copy-by-value at `main.py:10302-10304`
   inside `_trigger_verification_on_complete` (def `:10172`). Spawns at parent
   **completion**, when the parent's container has been `ready` for a long time
   → the snapshot it captures is already `ready`+host, so it works today. It is
   nonetheless the **same fragile pattern** (a resumed/suspended parent whose
   container is mid-re-provision at critic-spawn time would reproduce it) and
   should be fixed with the same mechanism for defense-in-depth.
3. **VM backend parity.** Both paths copy `context["vm"]` by value the same way
   (`:9516`, `:10302`). VM readiness (QEMU boot, SSH-readiness) also takes
   minutes, so a VM-backed loop job's scholar has the identical exposure. The
   fix must cover the `vm` key too, not just `workspace_container`.

## Root cause

A subjob is given a **point-in-time value copy** of a mutable, asynchronously
-populated parent field. Workspace readiness is inherently a later event than
subjob creation, but the inheritance is snapshotted at creation and there is
no write-back path from the parent's row to the subjob's copy. The
dispatch/provisioning logic then trusts the subjob's own stale copy as
authoritative.

## Fix design

**Principle:** a subjob that shares its parent's workspace must resolve that
workspace from the parent's **live** context at **dispatch** time, never from a
creation-time snapshot. Combined with a hold-and-requeue guard so an
unresolved workspace never dispatches into a guaranteed hard-fail.

### Part 1 — resolve inherited workspace at dispatch from the parent (primary)

In the dispatch path (`_dispatch_job_to_agent`, around `main.py:1982`), when the
job's own `workspace_container` is not `ready` **and** the job has a
`parent_job_id` (or `context.scholar_target` / `verification_target`), re-read
the **parent job's current** `context.workspace_container` (and `context.vm`)
and inject `workspace.remote` from *that*. The parent is the authority; the
subjob rides its pod via the pre-set `worktree_path`.

Two viable shapes (pick one):
- **(a) Dispatch-time lookup (preferred, self-healing).** Fetch the parent row
  in the injector and prefer its live container/VM context when the subjob's
  own copy isn't ready. Zero change to creation; also heals the VM path and
  the latent critic case for free.
- **(b) Stop snapshotting; store a reference.** At creation, instead of copying
  the dict, store `scholar_context["inherit_workspace_from"] = parent_job_id`
  (drop the `workspace_container`/`vm` copy). Dispatch resolves the reference.
  Cleaner data model but touches both spawn sites and any reader that expects
  the inline copy.

`_job_needs_sandbox` (`:3118`) must be made **parent-aware** the same way — a
subjob whose parent's live container is `ready` must return `False` (don't
provision a redundant pod), regardless of the subjob's own stale snapshot.

### Part 2 — hold-and-requeue instead of dispatching into a hard-fail (guard)

Today an unresolved sandbox workspace still dispatches, and the agent
hard-fails. Add a guard in dispatch: if a job resolves to `backend='sandbox'`
(or `vm`) with **no** `workspace.remote` after all injection attempts, do
**not** dispatch — leave/return it to `created` (or a `waiting_workspace`
substate) and let the dispatcher retry, with a bounded max-wait before an
honest `failed` carrying a clear reason. This turns a silent 0-token failure
into either eventual success (once the parent is ready) or a diagnosable
timeout. Mirrors the existing lite-workspace reject-up-front pattern
(`main.py:2027-2040`) and the VM readiness handling.

### Part 3 — apply to all three copy sites

The `vm` and `workspace_container` copies at `main.py:9516/9518`
(scholar) and `10302/10304` (critic) must all route through the same
resolve-at-dispatch mechanism so scholar, critic, container-backed and
VM-backed subjobs are all covered.

## Gotchas for the implementer

1. **The subjob shares, doesn't own, the parent's pod.** `worktree_path`
   (`:9592`) puts the scholar in a git worktree under the parent's workspace.
   The fix must **not** provision a second pod for the subjob — resolve the
   parent's host, don't create.
2. **Parent may be `suspended`/re-provisioning.** A parent whose container is
   suspended (`workspace_suspension.py`) or reattaching has a transient
   non-`ready` status. The hold-and-requeue guard (Part 2) is what makes this
   safe — don't dispatch until the parent's live context is `ready`+host.
3. **Race is timing-dependent, so tests must force it.** Reproduce by creating
   a parent whose `workspace_container` is `{"status":"created"}` (no host) at
   the moment the subjob is spawned, then flipping the parent to `ready` and
   asserting the subjob's dispatch injects the parent's host.
4. **Don't mutate the parent's row from the subjob path.** Resolve read-only;
   the parent's provisioner owns its context.
5. **`scholar_failed` semantics stay.** Whatever the fix, keep the
   parent-unblock-on-scholar-failure behavior (`:9660`) — a genuinely
   unrecoverable workspace should still free the parent, not wedge it.

## Verification

Unit (orchestrator tests):
- Subjob with own `workspace_container.status=="created"` + parent row
  `status=="ready"` with host → dispatch injects `workspace.remote` from the
  **parent's** host; `_job_needs_sandbox` returns `False`.
- Same with `context.vm` → VM remote injected from parent.
- No parent readiness → job is held/requeued, **not** dispatched; bounded
  max-wait then `failed` with a clear message (never a 0-token
  `no workspace.remote` hard-fail).
- Critic/verification subjob exercised through the same resolver.

Live (k3d, then homelab):
1. Submit a sandbox-backed job with scholar enabled (a loop/research job).
   Confirm the scholar subjob reaches the parent's pod and produces
   `output/` research (grafted into the parent) instead of failing at init.
2. Inspect both rows: subjob dispatch log shows
   `injected workspace container config … host=<parent host>`; subjob has
   audit entries > 0 and non-zero tokens.
3. VM-backed variant: same, host resolved from the parent VM.
4. Negative: kill/deny the parent's workspace and confirm the subjob is held
   then fails with a diagnosable reason, and the parent still unblocks.

## Related

- Root cause recorded in memory:
  `project_scholar_subjob_stale_workspace_snapshot`.
- Loop research/QA starvation findings:
  `docs/issues/loop_run6_deep_dive_forensics.md`,
  `project_better_resavio_loop_findings` — this bug is one concrete mechanism
  by which the research phase silently vanishes.
- Workspace readiness/reattach adjacency:
  `docs/issues/remote_backend_indefinite_wait_deadlock.md`,
  `docs/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md`.
