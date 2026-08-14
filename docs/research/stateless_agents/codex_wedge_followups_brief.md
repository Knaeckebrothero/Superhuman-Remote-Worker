# Codex brief — wedge follow-ups: driver contract + rescue route

**Date: 2026-08-14. Branch: `develop`, directly. Commit locally per
milestone. DO NOT PUSH — morning review decides. This run closes the two
open halves of `step5_handcheck_finding.md` and is the last substantive
build before worker admission opens on dev (step 6).**

## 0. Ground rules (deltas; the step-5/hardening briefs' rules all bind)

- Starting state: `develop` at `000919cb` (session-hardening fold) or a
  descendant; clean worktree. Untracked and untouchable: `HomeLab/`,
  `release_transition_checklist.md`, `docs/features/officer_backlog_pools.md`.
- Read FIRST: `docs/research/stateless_agents/step5_handcheck_finding.md`
  (the finding this run closes), then §5.4.5 decision (6) (route-don't-
  filter, the ownership invariant), then the step-5 M1 shell-hold contract
  in the implementation log.
- **The preserved specimen is the verification fixture and its consumption
  is SEQUENCED**: job `a61d9940-…`, command `2b028d0c-…`, its held
  workspace pod and terminal queue row on k3d. Do NOT touch it until M3;
  M3's rescue route (or its verified equivalent) is what finally moves it.
- Migrations: likely none needed; if one is, app starts at **0156**, vector
  at **0020**. Snapshot same-commit; squawk pinned.
- Models: pin soak jobs to `MiniMax-M3` (OpenRouter key dead; homelab
  gemma up-and-down). The k3d batch floor is 60 s (values-local) — that is
  your repro lever for the rotation suspect.
- Tilt assumed up — verify; usual rules.

## 1. Milestones

**M1 — find and guard the driver's continue-report source.** The stateless
worker emitted `/complete` with `should_stop=false` (job `a61d9940`,
~35 min in). Prime suspect: the batch wall-clock rotation boundary routing
through the terminal path instead of the queue release. Find the exact
emitting path in `src/agent.py` / `src/api/turn_executor.py`, prove it with
a failing test, and guard at the source: per the scope correction, a
rotation releases through the queue and a recoverable-error stop releases
with backoff — a driver with nothing terminal to say must never call
`/complete`. The accept-side 422 guard stays as defense-in-depth, not as
the primary control.

**M2 — the driver-on-422 contract.** Today a `CompletionNonTerminalReport`
422 leaves the driver in undefined territory (most likely holding the
shell forever, since the M1 hold engages around the report). Define and
implement the contract: on this 422 the driver must NOT enter or remain in
the finalization hold — treat it as "the report never happened": release
the unit through the queue with backoff (the sanctioned continue path),
keep the shell per ordinary rotation semantics, and log loudly (this is a
driver bug signal, not a normal flow). State the contract in one paragraph
in the implementation log. Tests: 422 → queue release + no hold + shell
admission restored; the hold engages only after a 2xx accept.

**M3 — the rescue route + invariant census.** Build the missing net for
the wedged shape: a stateless-lane job in a non-terminal status whose
queue row is terminal or absent, with no unfinished completion command,
is owned by nobody. Route it per decision (6)'s vocabulary — the safe
default is park+alert (operator worklist) unless you can prove re-enqueue
is safe for the shape (the wedged job's turn already answered; re-running
it re-executes work). Extend the M5 ownership-invariant census so this
shape fails CI red-before/green-after. Then, and only then, run the route
against the preserved specimen: the job must finally leave `processing`
coherently, its held workspace must be released exactly once, and the
specimen's rows/pod cleaned after evidence capture.

**M4 — soak (k3d, MiniMax).**
1. A stateless worker job whose task genuinely crosses the 60 s batch
   floor: rotation releases via the queue — zero `/complete` calls, zero
   422s — a successor claims, and the job finishes with exactly ONE
   terminal report.
2. The forced-422 path (synthetic continue-report under a valid lease, via
   test harness or a temporarily instrumented driver): driver releases with
   backoff, shell survives per rotation semantics, loud log line present.
3. The specimen convergence from M3, with row evidence.
Record wall-clock and row evidence per case in the implementation log.

**M5 (stretch, recon-only) — claimant-quiescence receipt.** You have twice
hit "public End fail-closed because bare Kubernetes 404 is not
process-zero proof" (the e4bb35f8 leftover, and this run's own pod-death
recon). Write the design note for a general durable quiescence receipt —
what evidence suffices (k8s + CRI + process/cgroup absence? lease age?),
where it lives, who writes it — as a section in the implementation log.
No build.

## 2. Out of scope

Step 6 itself (the dev flag flip is the user's move, after this run);
session-lane changes; VM tier; the model catalog; pushing.

## 3. Report

Per-milestone table with commits and evidence; the M1 root cause in two
sentences; the M2 contract paragraph; the M3 route decision and why;
soak table; the specimen's final disposition; deviations; morning
hand-check nomination.
