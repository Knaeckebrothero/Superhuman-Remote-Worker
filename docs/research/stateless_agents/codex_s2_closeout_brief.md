# Codex brief — S2 close-out: open sandbox admission

**Goal: a default (sandbox-tier) session runs correctly on the stateless lane,
and the tier gate is removed — its own docstring says it stays "until S2
acceptance passes". This session is that acceptance, plus the fixes it still
requires.**

Branch `feature/stateless-agents`, work directly on it. Do not push. Do not
touch `develop`. Migrations: **0133–0139** (your range from the driver session,
still entirely unused; head is 0132).

**One hard constraint on documentation: do NOT edit
`docs/features/stateless_agents.md` §5.4.5 today** — it is being rewritten
against the adversarial review in a parallel session. Your status updates go to
**§9.1** and `docs/research/stateless_agents/implementation_log.md` as usual.
Do not build anything from §5.4.5's DDL — it is known-defective pending the
rewrite (`docs/research/stateless_agents/gate3_adversarial_review.md` is the
review of record if you need context; nothing in this brief depends on it).

---

## 1. Where S2 actually stands (from your own two reports, verified)

Built and k3d-proven: the tmux ownership handoff (lease-token fenced,
flock-serialized), the cloud-push generation fence, the resident mount
controllers (rclone bearer refresh, overlay adoption, dead-lower recovery),
DB-backed presence and canvas awareness, and the exact-turn interrupt inbox
(migrations 0127–0129).

**Still open — your own "still unverified" lists, consolidated. This brief is
those items:**

1. **Live interrupt proof**: a main-process RAM unwind on a live turn; LISTEN
   latency; a forced cross-pod interrupt handoff; a pinned interrupt smoke.
2. **Workspace/runtime-incarnation authority**: shell ownership must bind to
   the authoritative workspace backing and runtime incarnation (a restored/
   replaced workspace pod must not satisfy a stale ownership record).
3. **Terminal-retirement acknowledgement**: lifecycle retirement failures must
   be reconciled — a session that ends must durably retire its shell record,
   and a failed retirement must not strand the next session.
4. **The cooperative-marker decision**: the tmux/rclone/overlay markers and
   flocks live in the workload user's home — cooperative, not a security
   boundary. Decide explicitly: move the correctness marker outside the
   workload user's writable authority, or **accept and document** the
   cooperative boundary for sandbox v1. Accepting is legitimate (the threat is
   the agent itself, which is the harness-containment track's problem, not
   S2's) — but the decision and its rationale must be recorded in the
   implementation log, not left implicit.
5. **The §6.1 RAM/path fixes** (your inventory's own decisions, none
   implemented):
   - `SessionTaskManager._tasks`/`_next_id` → externalize to Postgres keyed by
     thread (your inventory's decision; a workspace JSON cannot support
     backend `none`).
   - File-undo checkpoints → per-tier: sandbox/VM restore through the proven
     Git/turn-ledger path; virtual via object-store versioning or **fail
     closed and declare undo unsupported for that tier** — a documented
     limitation beats a silent lie.
   - Memory-extraction interval cursor → persist per thread (or derive from
     durable memory metadata) so a new claimant does not repeat extraction.
   - Cloud citation anchors → durable per-thread/path metadata.
   - Read-before-write stamps and sync caches → declare disposable per claim
     and measure the cold-reread cost once, as your inventory proposed.
6. **The three path-bypass bugs** (live on the pinned lane today, verified in
   code): `webdav_read`/`webdav_write` calling local `os.makedirs`/path ops on
   backend paths (`src/tools/webdav/tools.py:150,211`); research downloads
   inferring remoteness from `backend.host is not None`
   (`src/tools/research/workflow.py:254`, `papers.py:74`) — on the virtual
   tier an object key becomes an agent-local path under read-only `/app`;
   citation cloud-snapshot path ambiguity (`src/tools/citation/sources.py`).
   Fix through the backend (stage in /tmp → `backend.write_file`; never infer
   path semantics from `host`).
7. **Gate removal, last.** Only after every acceptance item below is green.
   Removal means `_require_stateless_lite_workspace` and its call sites admit
   sandbox; VM tiers stay refused (no mesh sidecar on the stateless
   Deployment) — narrow the gate, don't delete it.

## 2. Acceptance

- **The original S2 headline, never yet met**: a sandbox-tier session completes
  a multi-turn conversation across **at least two different pods** — shell
  state (tabs, cwd, env), workspace files, session tasks, and undo (per its
  tier decision) all intact. Numbers per phase.
- A live hard interrupt: mid-generation on pod A; and once across a forced
  handoff. Pinned interrupt smoke unchanged.
- A restored/replaced workspace pod does not satisfy a stale shell-ownership
  record (incarnation authority, item 2).
- Session end retires the shell record durably; a deliberately failed
  retirement is reconciled, not stranded (item 3).
- The path-bypass fixes proven on the virtual tier (the research download that
  used to land in `/app` reaches the backend).
- The cooperative-marker decision recorded with rationale (item 4).
- Standing gates: full suite at the 11-failure baseline, `ruff check` +
  `ruff format --check`, helm lint, schema snapshot + head pin in the same
  commit as any migration, pinned smoke path.
- **Commit your work when done.** Last session left ~6.3k lines uncommitted;
  it was committed for you after review, but commit-at-milestones is the
  contract.

## 3. Traps (standing list, all have drawn blood)

- Tilt ships partially-edited images — `kubectl exec <pod> -- grep` a string
  you just wrote on EVERY pod before trusting a run.
- `git checkout` while Tilt is up deploys that branch; `kill -9 1` does
  nothing — `kubectl delete pod --force --grace-period=0`.
- Blocking advisory locks deadlock against `CREATE INDEX CONCURRENTLY`;
  session try-locks in `.notx` work; the hardened migration runner handles
  INVALID-index retry recovery — don't bypass it.
- admin-cli: use the `id_token`; it dies in ~15 min as a silent 401.
- Never `git add -A`; never manual `helm upgrade`; **never `tilt trigger srw`**.
- A fresh git worktree fails 19 helm tests spuriously — don't baseline there.

## 4. Stop rule

Stop only when a premise is load-bearing AND there is no reasonable
alternative. A discovered dependency is scope to absorb — record it in the
implementation log and continue. If an item resizes (the incarnation-authority
work is the likeliest), say so in the report rather than silently descoping.

## 5. Report back with

What you built and verified with numbers; the cooperative-marker decision and
rationale; each §6.1 item's final disposition (fixed / declared-disposable /
descoped-with-reason); whether the gate is removed or what still blocks it;
what remains unverified.
