# Job termination as a negotiated transition

**Date:** 2026-07-28
**Status:** **PARKED — idea captured, not scheduled.** Too large for the
current fix cycle; recorded so it is not lost and so the tactical fixes
shipping now can be checked against it for forward-compatibility.
**Incident that motivated it:** `docs/issues/transient_db_error_hard_fails_job_and_destroys_vm.md`
(that doc remains the evidence record and the tactical fix spec).

## Problem

Job termination is a **unilateral database write**. Any of 13+ call sites can
set `status='failed'` on a job while an agent is actively executing it. Three
consequences follow, and all three were observed in one incident on
2026-07-27:

1. **The agent is not told.** It keeps executing — 21 minutes and 45 LLM
   calls, in the observed case — because nothing carries status back to a
   running agent.
2. **Its later truth is rejected.** The `/complete` gate
   (`orchestrator/main.py:14240`) refuses any report on a terminal job before
   inspecting it, so a completed job's `job_complete` freeze and a
   recoverable `workspace_unavailable` report are both discarded with a 400.
3. **Its workspace is collected underneath it.** Terminal status triggers VM
   teardown, which destroyed the only path by which job `c6dd288d` could
   record its own completion. Its five subsequent `job_complete` calls all
   failed against a deleted VM.

The common shape: **the orchestrator and a live agent can disagree about
whether a job is alive, and there is no mechanism to detect or resolve that
disagreement.** First writer wins, silently.

Patching individual call sites does not close this. The next novel infra
error, cancellation race, or reaper interaction finds a new way through,
because the invariant — *only one party may declare a job over, and the other
party is never consulted* — is unchanged.

## The three pillars

### 1. Execution epoch (ownership)

When an agent claims a job it receives an **epoch** — a monotonically
increasing counter or token stored on the job row alongside
`assigned_agent_id`. Every write that would terminate a job must either
present the current epoch or **explicitly preempt** it.

- A write carrying the current epoch is the owner resolving its own job.
- A write without it is a preemption: allowed, but recorded as such
  (`preempted_by`, `preempted_reason`) rather than being indistinguishable
  from a normal completion.
- The epoch advances on every re-claim, so a zombie agent from a previous
  claim cannot resolve a job that has since moved on. This alone would have
  answered the open question in the incident doc — which process posted the
  killing report.

### 2. Bidirectional liveness

The heartbeat already flows agent→orchestrator to renew the lease
(`docs/features/job_execution_lease.md`). Today it is one-directional: the
agent asserts liveness and learns nothing.

Make the **response authoritative**. It carries the job's current status and
epoch. An agent whose epoch is stale, or whose job is no longer `processing`,
stops within one heartbeat interval and reports why. No new call, no new
state, no polling — the channel and its cadence already exist.

### 3. Idempotent terminal resolution

Terminal status becomes a **resolution computed from available evidence**,
not a first-writer-wins race. Evidence: the agent's report, the freeze
artifact in the jobs repo, the infra error, the epoch.

A late but authoritative report — a `job_complete` freeze from the epoch
owner — **re-resolves** the job rather than being rejected. A late report
from a stale epoch is logged and acknowledged, never applied. `cancelled`
stays terminal against everything: explicit human intent is not overridden by
a late machine report.

### Cross-cutting: defer destructive side effects until resolution is final

VM teardown and checkpoint pruning currently fire on the status *write*. Both
are irreversible, and both fired on a write that a later report would
legitimately have re-resolved. Under this design they move behind resolution
being **final** — no owner epoch outstanding, no re-resolve window open.

This is the single highest-value piece. Losing the workspace is what turned a
recoverable incident into permanent data loss.

## What the current defects become

| Tactical defect (incident doc) | Under this design |
|---|---|
| 1 — no transient error class | Still needed as a classification concern, but non-terminal by default: an unresolved job stays owned rather than dying |
| 2 — terminal guard upstream of recovery arm | Dissolves. Resolution inspects evidence before deciding; there is no gate to sit upstream of |
| 3 — agent unaware its job was terminated | Pillar 2, directly |
| 4 — `updated_at` misdates failures | Resolution records its own timestamp and cause; `failed_at` becomes part of the resolution record |
| 6 — terminal prune self-defeating under bloat | Deferred side effects; prune runs once, when resolution is final |
| 8 — `job_complete` swallows workspace death | Independent tool-level bug; unaffected |

## Forward-compatibility of the tactical fixes

The fixes shipping now are **narrow cases of these pillars**, not throwaway
work:

- Defect 2's "re-resolve a `job_complete` freeze, never re-open" is pillar 3
  restricted to one evidence type and one transition.
- Defect 3's status-carrying heartbeat response is pillar 2 in miniature —
  the same channel, without the epoch.
- Defect 1's "keep the VM, pause instead of terminate" is the deferred
  side-effects principle applied to the one cause that triggered it.

Each generalizes rather than being rewritten. The main migration cost is
adding the epoch column and threading it through claim/heartbeat/complete.

## Open questions

1. **Epoch vs. lease token.** `lease_expires_at` already exists and is
   renewed by heartbeat. Is a separate epoch warranted, or should the lease
   carry a generation number? Leaning toward extending the lease — fewer
   concepts, and the renewal path is already correct.
2. **Re-resolve window.** How long may a terminal job accept a late
   authoritative report? Unbounded is simplest and probably fine given the
   epoch check; a bound may be wanted for operator sanity.
3. **Preemption policy.** Which existing call sites are legitimate
   preemptions (operator cancel, grant violation, reaper) versus bugs to be
   converted into resolutions? Needs an audit of all 13+ sites.
4. **Deferred teardown and cost.** Holding VMs until resolution is final
   costs resources. Interaction with the workspace reaper needs working out
   so deferral does not become a leak.

## Why parked

The tactical fixes address the observed losses and are independently
valuable. This redesign touches the claim, heartbeat, dispatch, and
completion paths simultaneously — a large test surface against a live dev
cluster running multi-day jobs, during a feature freeze. It should be picked
up deliberately, not folded into an incident response.

Revisit when: another incident of this shape occurs despite the tactical
fixes, or the lifecycle paths are being reworked for another reason.

## Related

- `docs/issues/transient_db_error_hard_fails_job_and_destroys_vm.md` — the
  incident, the eight tactical defects, and their fixes.
- `docs/features/job_execution_lease.md` — the existing liveness channel
  pillar 2 extends.
- `docs/features/llm_outage_pause_and_backoff_redispatch.md` — the
  pause/backoff/re-dispatch model for non-terminal failure handling.
- `docs/done/coincident_infra_error_overrides_reported_job_outcome.md` —
  earlier hardening of `determine_job_status`, downstream of the gate pillar
  3 replaces.
