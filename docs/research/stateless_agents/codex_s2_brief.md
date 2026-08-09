# Codex brief — S2: make the stateless lane work for real workspaces

The stateless session lane is functionally complete **for lite tiers only**
(`virtual` / `none` — no workspace pod). But `config/session_base.yaml` defaults
to `backend: sandbox`, an SSH workspace container. So the default session a user
creates cannot safely use this lane, and the lane has only ever been proven on a
single lite thread.

**Your goal: a session with a real (sandbox) workspace runs correctly on the
stateless lane — shell state survives a cross-pod handoff, cloud sync stays
ordered, and nothing agent-local is silently lost.**

Branch off `feature/stateless-agents`. Do not push. Do not touch `develop`.
Migrations start at **0122** (0115–0121 are taken).

---

## 1. Facts already verified — do not re-derive these

Checked against the code on 2026-08-09. Line numbers will drift; the claims
won't.

* **The lane does not gate on workspace tier.** Nothing in
  `src/api/turn_executor.py` checks the backend; it calls `_attach_session`,
  which for a non-lite thread polls for a workspace pod. So a sandbox thread on
  this lane *attempts* to work today and half-succeeds — which is worse than
  failing.
* **tmux is destroyed at both ends of every claim.** `RemoteBackend._init_shell`
  runs `tmux kill-session … || true` then `tmux new-session`
  (`src/core/backends/remote.py:1137,1141`), and `disconnect()` runs
  `tmux kill-session` (`:519`). So on a sandbox thread every turn wipes the
  shell, and a claim-switch wipes it again.
* **The tmux session name is deterministic**: `agent_{job_id[:12]}` where
  `job_id` is the thread id for sessions (`:278`). Reattach-if-exists is
  therefore viable — the name is stable across pods.
* **`WORKSPACE_PVC_ENABLED` defaults to `False`**
  (`orchestrator/services/agent_provisioner.py:121`) and PVC-backing applies only
  to `purpose == "session"` (`:330`). The manifest comment at `:1455–1461`
  explains the intent: PVC-back `/workspace` for *all* sessions because for lite
  sessions the agent-local copy "is the only copy". The `agent-stateless`
  Deployment uses `emptyDir`.

---

## 2. Read first

1. `docs/features/stateless_agents.md` — **§9.1** for current status (written
   against code), then §5.3.5 (background work, outbox, the two resident
   daemons), §5.3.6 (session semantics that must survive), §6 (prerequisites —
   §6.1 is yours), §9's S2 line for scope and acceptance.
2. `docs/research/stateless_agents/implementation_log.md` — four build sessions.
   The "Traps hit" sections will save you hours.
3. `src/api/turn_executor.py` and `src/core/backends/remote.py`.

---

## 3. The work

### 3.0 Fail closed first (do this before anything else)

Right now nothing stops a sandbox thread being put on this lane, and the failure
is silent shell destruction rather than an error. Gate it: the lane admits only
lite tiers until the rest of this brief lands, and says so clearly when it
refuses. Then remove the gate as the last step, once the acceptance below
passes. This is a small change that makes every later step safe to develop
against.

### 3.1 tmux reattach-if-exists

Make `_init_shell` reattach when the deterministic session already exists
(`tmux has-session`) instead of kill-and-recreate, and make the kill in
`disconnect()` conditional on genuine session end rather than a claim switch or
detach. Rehydrate tab state from `tmux list-windows` so the tool layer's view
matches reality after a handoff.

The alternative the doc allows is declaring shell state batch-scoped — i.e.
explicitly *not* preserved across a handoff. If you conclude reattach cannot be
made reliable, take that route deliberately and write it down; a documented
limitation beats a flaky guarantee. But do not leave the current behaviour,
which promises continuity and silently destroys it.

### 3.2 The §6.1 agent-local state inventory — the hard part

This is discovery, not construction, and it is the reason S2 is not a
mechanical port. **What does a session write under agent-local `/workspace`
that must survive the pod?** Known candidates: uploads staging, memory/KB
artifacts, canvas-adjacent files, file-undo state, the session task manager.
For each one, decide and record: externalize to object store / DB, or declare it
disposable. The design's own words are that PVC-backing was the fix *because*
lite sessions had no other copy — a Deployment cannot mount a per-thread PVC, so
the copy has to move or be declared expendable.

Produce the inventory as a table in the implementation log before you build the
fixes. If the list turns out longer or nastier than the doc implies, say so —
that is a legitimate finding and it resizes S2 rather than blocking it.

### 3.3 Cloud-push sync-generation fence (§5.3.5)

Turn-end push and turn-start pull must stay ordered *across pods*: push(N) must
land before pull(N+1), even when N and N+1 run on different pods. The in-process
guard that exists today cannot see another pod. Needs a durable generation
marker the next claimant respects.

### 3.4 The two resident daemons

rclone token refresh and the overlay ENOTCONN heal keep *workspace* mounts
alive. They cannot become queued work — they are not per-turn. Decide where they
live when no pod owns the session between turns: the workspace pod itself, a
sidecar, an orchestrator sweep, or heal-on-claim. "Heal on next claim" is
acceptable if you prove the mid-idle expiry case actually heals.

### 3.5 Outbox re-homing, hard-interrupt routing, presence

Per §5.3.5 and the S2 line in §9: re-home the post-turn background work
(including the `llm_requests` archive) so it survives release; route hard
interrupt to whichever pod holds the lease (this is also what would let
`/interrupt` stop being a 501); and re-home canvas presence so it doesn't
flicker between turns — or name and accept the flicker.

---

## 4. Acceptance (from §9's S2 line)

- `push(N)` → `pull(N+1)` ordering holds **across a forced pod handoff**.
- tmux state survives a handoff (or is documented as batch-scoped, per §3.1).
- A mid-idle rclone token expiry heals on the next claim.
- p95 approval-to-visible-resume < 3 s.
- Canvas presence has no inter-turn flicker, or the flicker is named and accepted.
- A sandbox-tier session completes a multi-turn conversation across **at least
  two different pods**, with shell state and workspace files intact.
- The §6.1 inventory exists, with a decision recorded per item.

Plus the standing gates: `pytest tests/ -q` at the known baseline (11
environment failures — confirm any new one reproduces on `develop` before
chasing it), `ruff check` and `ruff format --check` clean, and the pinned lane
unchanged (walk the README smoke path).

---

## 5. Traps (each cost real hours)

- **Tilt ships partially-edited images.** `updateStatus: ok` is not evidence.
  Verify with
  `kubectl --context=k3d-srw -n srw exec <pod> -c agent -- grep -c "<a string you just wrote>" /app/<file>`
  on **every** running pod before trusting a measurement.
- **`git checkout` while Tilt is up is a deploy of that branch.** To test whether
  a failure predates you, stash the one file (`git stash push -- <path>`) — note
  a fresh worktree fails 19 helm tests spuriously for lack of gitignored files.
- **admin-cli's `access_token` has no `sub`** and 500s the auth resolver; use the
  `id_token`. It expires in ~15 min and fails as a silent 401.
- **`kill -9 1` inside a container does nothing** (PID-1 signal protection). Use
  `kubectl delete pod --force --grace-period=0`.
- **Turns are ~5 s now**, so fault injection needs a deliberately long generation
  and a kill fired the instant the claim appears. `scripts/stateless-lane-probe.sh
  kill` already encodes this.
- After **any** migration: regenerate with `scripts/schema-snapshot.sh` and stage
  the snapshot in the **same commit**, and bump `APP_CURRENT_MIGRATION_HEAD` in
  `tests/test_infrastructure_metering_migrations.py`. Never edit `schema.sql`.
- Never `git add -A`; never `helm upgrade`/`install` by hand; **never
  `tilt trigger srw` — it uninstalls the release.**

---

## 6. When to stop

Stop only when a premise is load-bearing **and** there is no reasonable
alternative route — "the thing I am standing on does not exist". Otherwise
adapt, build the alternative, and record the deviation in the log. A discovered
dependency is scope to absorb, not a reason to halt.

Keep a running log in `docs/research/stateless_agents/implementation_log.md` as
you go, and mark something DONE only when it is verified. Update
`docs/features/stateless_agents.md` §9.1 to match what you actually landed —
that section is the status of record, and leaving it stale is a defect.

**Out of scope:** the worker lane (S3). It is blocked on the Gate 3 design in
§5.4.5. Do not enqueue a `worker_batch` unit.

---

## 7. Report back with

What you built, what you verified and how, in numbers. The §6.1 inventory. What
you decided differently from the design and why. What is still unverified.
