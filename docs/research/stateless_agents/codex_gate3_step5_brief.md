# Codex brief — Gate 3 step 5: background finalization for stateless workers

**Date: 2026-08-13 (follow-on run). Branch: `develop`, directly. Commit
locally per milestone. DO NOT PUSH — step 4 is itself still unpushed pending
the live review probe, and the push decision stays with the review.**

## 0. Ground rules (deltas on the step-4 brief; everything else binds)

- Design authority order: `docs/features/stateless_agents.md` §5.4.5 —
  especially the (2) retry matrix, the (7) rollout's step 5 line and its
  revert rules ("admission-off ≠ executor-off", the stateless queued-age
  alarm), and the step-4 ops note at §"Gate 3 step-4 operations" — then the
  M6-deferral note at the tail of `implementation_log.md` (your own recon:
  a fresh stateless 202 while the reordered job is still `processing` is
  treated by worker cleanup as permission to destroy its tmux shell, while
  the command may still resolve to review/pause/waiting/retry). That note is
  the reason this brief exists.
- **Migrations start at 0145** (0141–0144 are taken and k3d-applied;
  fix-forward only). Snapshot regenerated in the same commit; squawk pinned;
  `.squawk.toml` path exceptions only with rationale.
- **Environment tonight**: the homelab box serving the k3d default chat
  model (`gemma-4-moe`) and the dev cluster may be OFFLINE. Do not depend on
  either. For k3d soak jobs, pin the model per job via
  `config_override: {"llm": {"model": "openrouter/openai/gpt-oss-120b"}}`
  (catalog row exists and is enabled) or `MiniMax-M3` — both are cloud
  routes. If cloud LLM is also unreachable, do the real-PG and unit
  milestones and leave the soak milestone cleanly not-started.
- The worktree carries the user's untracked `HomeLab/`,
  `release_transition_checklist.md`, and `docs/features/officer_backlog_pools.md`
  — never stage or modify any of them.
- Tilt is UP; the usual rules (no `tilt trigger srw`, no checkout, verify
  image bytes before trusting a smoke).
- One live probe job may still be running from the review
  (`ee33e63f-…`, repo `job-ee33e63f`, plus its workspace pod) — leave it and
  its rows alone.

## 1. What you are building

Step 5 of §5.4.5 (7): stateless-lane **worker** terminal reports return
**202 at accept and finalize in the background**; pinned stays inline.
Session units are untouched (they never call `/complete`). The step is
blocked by the worker-handoff contract you identified, so that fix comes
first and gates the rest.

**M1 — the worker shell/lifecycle handoff contract.** The worker-side
cleanup (S2 tmux ownership machinery + the driver's terminal path) must key
shell destruction and workspace release on the **finalized outcome**, never
on the accept. Concretely: after a 202, the unit/shell enters a
"finalization-pending" hold; the driver learns the outcome via the (2) retry
matrix (`done` ⇒ stored outcome + `Idempotent-Replayed`) or the lease-renewal
RETURNING channel — pick the mechanism from the doc, don't invent a third —
and only a genuinely terminal outcome releases the shell. Review / pause /
waiting / retry outcomes must leave the tmux session intact and reattachable
(that is the entire point: a human-stop must land on a live shell). Tests:
every non-terminal resolution preserves the shell; terminal releases exactly
once; a crashed driver between 202 and outcome neither leaks the hold
forever nor destroys early (bound it with the command's own
lease/deadline vocabulary).

**M2 — 202 accept + background drain for stateless workers.** Flag the
behavior inside the existing `COMPLETION_COMMANDS_ENABLED` +
worker-lane gates (a stateless worker report with commands on ⇒ 202 +
enqueue to the background finalizer; the finalizer already exists — this is
routing, not a new engine). Respect the step-3 live-fuse rule: accept stays
short; the drain owns everything after. Pinned behavior byte-identical.

**M3 — the step-5 nets.** The stateless queued-age alarm (scale-to-zero
leaves queued units with no claimant — alarm in the monitoring path, not the
finalizer loop, alongside the existing zero-leader and oldest-command-age
alarms). The revert rule made real: admission-off ≠ executor-off — document
and test that disabling worker admission keeps the claim loop draining
in-flight units to terminal.

**M4 — soak (k3d, cloud model, only if reachable).** The step-5 rows from
§5.4.5's acceptance list, all against a stateless worker job:
1. Terminal report → 202 → background finalization completes; tmux shell
   survives until the outcome and is destroyed exactly once after it.
2. A `blocking_message`/review stop: 202, finalization resolves to a
   human-facing status, the shell is ALIVE and reattachable afterwards.
3. Kill the agent between the 202 and the queue release → the unit is not
   requeued and no successor claims it.
4. Kill the orchestrator mid-terminal-completion on the stateless lane →
   consume-or-benign-re-report; never park.
Record wall-clock and row evidence per row in the implementation log.

**M5 (stretch) — doc fold.** Fold the M6 recon + this step's design into
§5.4.5 as the step-5 design note (the deferral rationale currently lives
only in the log), and update §9.1's status section.

## 2. Out of scope

Step 6 (worker admission flip) and anything touching its default-off state;
the session lane; VM tier; the Cerebras/params_json model-catalog work (the
user parked it); the unpushed step-4 code beyond what M1/M2 genuinely
require; the dev cluster in any form.

## 3. Report

Per-milestone table (status, commit, evidence), the M1 contract stated in
one paragraph (what holds the shell, what releases it, what bounds the
hold), soak table if run, deviations with reasoning, and the morning
hand-check nomination.
