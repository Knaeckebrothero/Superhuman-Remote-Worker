# Codex brief — session-lane hardening on the Gate-3 substrate

**Date: 2026-08-13 (follow-on run). Branch: `develop`, directly. Commit
locally per milestone. DO NOT PUSH — step 5 is unpushed pending the live
hand-check; you stack on top of it.**

## 0. Ground rules (deltas on the step-5 brief; everything else binds)

- Starting state: `develop` at `d2573d4f` (step-5 M5) or a descendant.
  Untracked and untouchable: `HomeLab/`, `release_transition_checklist.md`,
  `docs/features/officer_backlog_pools.md`.
- **A live hand-check job is running or parked: `a61d9940-…` on the
  STATELESS lane, plus the older pinned probe `ee33e63f-…` and their
  workspaces/rows. Leave every one of them alone** — the step-5 review
  verdict depends on them.
- **Migrations start at 0145.** Snapshot in the same commit; squawk pinned;
  exceptions only with rationale.
- Cloud models: **`MiniMax-M3` is the only proven-working k3d model** (the
  OpenRouter key is dead — 401; the homelab box behind `gemma-4-moe` and the
  embedding endpoint is up-and-down while the user troubleshoots — treat it
  as unreliable and pin every soak job to MiniMax-M3). Auxiliary-model and
  embedding timeouts against the homelab are environmental noise, not your
  bug.
- Tilt is up; the usual rules.

## 1. M0 — recon first, and let it reshape the milestones

Several "known gaps" below come from status notes that may be stale after
S2 and Gate 3. Before building anything, establish current truth in code
and record it in the implementation log:

- The `interrupt` control verb: what exists end-to-end today on each lane
  (REST? control-WS? the 0119 inbox? executor-side delivery mid-turn? S2
  shipped "interrupt receipts" — receipts of WHAT path?). Where exactly does
  a 501 or dead-end remain?
- Permission rows (tool-approval requests) on the stateless lane: what
  happens to an open request when the claim's lease expires or the thread
  ends? Is there any sweeper today?
- Ended-session wake: what fires (or fails to) when a session a job wants
  to wake has ended?
- End-of-session memory extraction: what is actually lost today at session
  end/handoff (the "0-4-turn final-memory gap" named at S2 close), and
  where does the extraction cursor live now?

Shrink or drop any milestone whose gap turns out to be already closed —
record the finding instead; that is a full-value outcome.

## 2. Milestones (post-recon shapes)

**M1 — final-memory outbox on `completion_effects`.** Close the end-of-
session memory-loss gap using the substrate designed for exactly this:
`completion_effects` with `producer_kind='session_turn'` and a
`turn_execution_id` minted INSIDE the fenced persist transaction (§5.4.5's
DDL comment spells out why the unit_id cannot be the key). The extraction
work stays async/auxiliary; what becomes durable is the OBLIGATION, so a
pod death or handoff after the final turn no longer silently drops the last
turns' memories. Respect the retention/pruning rules the doc sets for
session producers (age-pruned from `created_at`, no state partial index).
Real-PG tests: obligation minted exactly once per turn, fenced-out attempt
mints nothing, drain executes exactly once, prune leaves commands intact.

**M2 — the `interrupt` verb to production quality on the stateless lane**
(and REST parity on pinned if the recon shows it is control-WS-only).
Durable via the 0119 control inbox pattern, fenced to the live claim,
delivered to the executing pod mid-turn (the executor already has a
tool-wait/heartbeat seam — pick the doc-sanctioned channel, don't invent a
side-band), with a journal receipt the cockpit can render. An interrupt
that arrives with no live claim resolves cleanly (queued-turn cancel or
no-op receipt), never 501, never a dangling pending row.

**M3 — permission-row retire on lease expiry + ended-session wake.** Both
are named S1 leftovers. Permission rows: an open approval bound to a claim
that lost its lease must retire (deny-by-default with a receipt, or re-arm
on the successor claim — pick from how the S1 presence/permission design
reads, and say which and why). Ended-session wake: a `wake_on_complete`
pointed at an ended/deleted thread must resolve (deliver-elsewhere or
mark-undeliverable) rather than wedge or silently vanish.

**M4 — k3d soak (MiniMax-pinned).** One stateless session driven through:
mid-turn interrupt honored with receipt; a tool-approval left open across a
forced claim steal → retired per M3's contract; session end → final-memory
obligations drained (rows to prove it); nothing leaked. Plus the M1
crash case: kill the executor between final persist and drain → obligation
survives and executes once on recovery.

**M5 (stretch) — docs.** Fold what shipped into the session-reliability doc
and §9.1; update the S1-leftovers list to current truth.

## 3. Out of scope

Step 6 admission defaults; pushing anything; VM tier; metering lease-
interval attribution; the model catalog (dead OpenRouter key, Cerebras) —
all parked with the user. The dev cluster entirely.

## 4. Report

Per-milestone table with commits and evidence; the M0 recon findings as
their own section (they are deliverables); contracts stated in one
paragraph each for M2 and M3; soak evidence; deviations; morning hand-check
nomination.
