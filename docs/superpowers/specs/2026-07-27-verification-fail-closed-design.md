# Fail-closed verification: a durable findings ledger on the target job

**Date:** 2026-07-27
**Status:** Design approved, not implemented.
**Supersedes the fix plan in:** `docs/issues/verification_round_reset_spawns_blind_critic.md`
(that doc remains the incident analysis and evidence record).

## Problem

The verification gate approves work when its own state goes missing. Three
stacked defects, established by a five-agent audit:

1. **Durability.** The verdict lives in a module-level dict in the agent
   process (`src/tools/evaluation/evaluation_tools.py:28`), cleared before the
   freeze is emitted (`src/core/phase.py:798`). Every process boundary loses
   it, and critics cross process boundaries by design.
2. **Semantics.** A missing verdict is *defined* as approval, in eight
   distinct paths. CWE-636.
3. **Continuity.** Rounds chain on `status = 'waiting'`
   (`orchestrator/main.py:12350`), so any status flip restarts review at round
   0 with a critic that has no knowledge of the open issues — and the brief
   that would carry them is rendered and discarded (`main.py:12400`, `:12406`).

Live consequence (job `52949749`): returned twice at severity high, then
approved on a byte-identical deliverable by a fresh critic that never saw the
findings.

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Critic lifecycle | **Fresh critic every round** | Once findings are durable the reviewer need not be. Deletes the `waiting` dependency outright rather than guarding it; matches the original spec (`verification_phase.md:169-176`); leaves one code path instead of two. |
| Closure authority | **Evidence-only** | A finding closes only on a quote from the *new* deliverable. It cannot be closed by re-judging. Mirrors GitHub: another reviewer's approval never clears a blocking review. |
| Verdict | **Computed, never asserted** | Derived from the open findings; the model's claim is recorded for audit but does not decide. |
| Round cap | **Escalate to human** | Never auto-accept work the critic rejected. Matches CodePipeline / GitHub Actions (timeout ⇒ fail, never approve). |

Throughout this document, **"escalate"** means: hand the job to a human
without approving it — `pending_review` for an ordinary job, `completed` with
the findings in `error_message` for a project-loop job (see
[Constraint interactions](#project-loop-jobs-must-not-escalate-to-pending_review)).
It never means approval.
| Storage | **Append-only JSONB array on the target** | Zero migrations; reuses a proven atomic-append primitive. Documented 2-release path to a table exists if analytics ever demand it. |
| Round budget owner | **The target job** | Cannot reset when a critic dies. |
| Finding IDs | **Server-assigned** | The critic proposes claims and dispositions IDs but never owns the namespace, so it cannot renumber or silently drop one. |

## Non-goals

- **Frozen rubrics** (TICK-style checklist fixed at round 1). The research
  case is strong — criteria drift is the named cause of the round-3 approval —
  but evidence-only closure plus a computed verdict already makes that
  incident impossible. Frozen rubrics additionally prevent a critic inventing
  *new* criteria in later rounds (moving goalposts), which is a real but
  different failure already bounded by the round cap. Deferred.
- **The drain-overwrite bug**
  (`docs/issues/drain_freeze_overwrites_critic_verdict.md`). This design
  *neutralises* it for verdicts — the verdict is durable before a freeze
  exists — but it still destroys worker `job_complete` freezes. Fixed
  separately.
- **The `_final_phase_data` durability defect** for ordinary jobs
  (`docs/issues/job_finalization_decisions_held_only_in_process_memory.md`).
  Same class, different consumer.

## Architecture

**The invariant: the ledger on the target job is the single source of truth.
Job status and `freeze_data` are projections of it.** Nothing load-bearing
keys off a status value again.

### Data model

`jobs.context.verification_rounds` — an append-only array on the **target**:

```json
{ "round": 2,
  "critic_job_id": "…",
  "head_commit": "a8117788",
  "verdict": "returned",
  "asserted_verdict": "approved",
  "opened":       [ {"id":"F4","severity":"high","claim":"…","evidence":"…"} ],
  "dispositions": [ {"id":"F1","disposition":"RESOLVED","quote":"…"},
                    {"id":"F2","disposition":"DISPUTED","reason":"…"} ],
  "ts": "…" }
```

**Findings are never mutated; the open set is a fold over rounds.** A finding
is open unless a later round marked it `RESOLVED`. `DISPUTED` records
disagreement without closing it. This keeps the array purely append-only (no
lost-update surface), yields a complete audit trail, and folding 3-5 rounds is
trivial.

**Dispositions** are `RESOLVED` (requires `quote` from the new deliverable),
`STILL_OPEN`, or `DISPUTED` (requires `reason`; does not close).

**Disposition is required for blocking findings only.** Non-blocking findings
are recorded and rendered to the target as advisory notes, but are not tracked
as gate state and need no disposition — otherwise low-severity nits would
accumulate across rounds and have to be re-answered forever. They are surfaced
alongside the verdict on approval.

The severity taxonomy is closed and ordered: `low` < `medium` < `high`,
matching the existing tool signature (`severity: str = "medium"`).

### Verdict computation

```
open_after = fold(all rounds including this one)
blocking   = [f for f in open_after if f.severity >= BLOCKING_SEVERITY]
verdict    = "returned" if blocking else "approved"
```

`BLOCKING_SEVERITY` is a module constant set to `high` for this work — named
rather than inlined so it can become a `verification.blocking_severity` config
key alongside the existing `verification.max_rounds` if a caller ever needs a
stricter gate. Adding that knob is **not** in scope here.

Severities below the threshold are recorded and shown to the target but do not
gate — this preserves the existing "be proportionate; the bar is meets
requirements" instruction and stops nits looping forever.

Divergence is checked in both directions:

- asserted `returned` with **zero** findings → **rejected at the tool
  boundary**, no round recorded (this is the `issues: "[]"` bug, fixed at the
  right layer);
- asserted `approved` with blocking findings open → computed verdict wins.
  **This single rule is what makes the round-3 approval impossible.**

### Progress detection

The job repo's HEAD commit at freeze — already durable and tamper-evident,
since the freeze commits and pushes. If a new round's `head_commit` equals the
previous round's while blocking findings are open, the target produced
nothing: **no critic is spawned** and the job escalates.

A failed push would also freeze the SHA, so this can false-positive. That is
accepted deliberately: a false positive costs one human glance, while assuming
progress costs another wasted round. It fails toward the human, like
everything else here.

## Components

### New

**`append_verification_round()`** — `orchestrator/database/postgres.py`.
Clone of `append_queued_reply` (`:1859-1897`): single-statement
`jsonb_set + ||`, lost-update-immune under READ COMMITTED with two orchestrator
replicas. Guarded by a containment check on `critic_job_id` so a duplicate
`/complete` is a no-op. Payload wrapped in `jsonb_build_array` — a bare array
splices instead of appending.

**`POST /api/jobs/{target_id}/verification/rounds`** — internal-key endpoint.
Body: `critic_job_id`, `asserted_verdict`, `opened[]`, `dispositions[]`.
Server-side: validate dispositions against the current open set, assign IDs
continuing from the max, compute the verdict, append, return the computed
verdict and assigned IDs. 409 with a specific reason on validation failure.

### Changed

**Verdict tools** (`src/tools/evaluation/evaluation_tools.py`) call that
endpoint **before returning**, and store the *server-returned* verdict in
`_verdict_data`, which becomes a cache rather than the source of truth. Any
failure returns an error to the model and records no verdict. This
deliberately inverts the house best-effort convention (`src/tools/context.py:510`,
`if client is None: return None`) — for a verdict, a missing orchestrator
client must fail loudly.

`approve_job` additionally rejects the call outright if blocking findings
remain un-`RESOLVED`, listing them.

**`_trigger_verification_on_complete`** (`orchestrator/main.py`) loses the
`status='waiting'` query. It reads the ledger, sets `round = len(rounds)`, and:

- `head_commit` unchanged + blocking open → escalate, no critic;
- `round >= max_rounds` + blocking open → escalate, no critic;
- otherwise spawn a **fresh** critic with the rendered brief placed in
  `context["instructions"]` — the dead-template fix.

**`_handle_critic_verdict_on_complete`** gains the `verification_target`
discriminator and a terminal-status gate (mirroring the scholar handler at
`main.py:11677`), and reads the computed verdict from the ledger. A critic that
ended with no round record for its round resolves to `unknown` → escalate.

**`format_verification_instructions`** gains a `{prior_findings}` parameter
**with a default**, because a missing key raises `KeyError`, which returns
`None`, which aborts critic spawn entirely at `main.py:12406`.

**`_finalize_with_verdict`** (`src/core/phase.py:684-697`) — a returned verdict
now freezes the critic as `completed`; critics no longer park in `waiting`.

**`unstick_reviewing_parents`** (`postgres.py:4462`) becomes ledger-aware — see
Constraint interactions below.

### Deleted

The `waiting` lookup; both implicit-approval branches (`phase.py:803-812`,
`main.py:12228-12236`); the round-cap auto-accept (`main.py:12266-12281`);
reliance on `increment_job_verification_round`. `job_complete` and
`mark_complete` are removed from the critic's toolset via an explicit `core`
list in the `config_override` — now belt-and-braces rather than load-bearing,
since self-closing produces no round record and therefore escalates.

## Flow

```
target completes → status=reviewing
  └─ read ledger from TARGET
     ├─ head_commit unchanged + blocking open → escalate, no critic
     ├─ round >= max_rounds + blocking open   → escalate, no critic
     └─ spawn fresh critic; instructions = template + open findings by ID
          └─ critic dispositions each open ID, proposes new findings
             └─ POST rounds ──▶ validate → assign IDs → COMPUTE → append
                            ◀── computed verdict
             └─ tool returns; critic freezes as completed
  └─ verdict handler reads LEDGER
     ├─ returned  → target paused + feedback (findings rendered with IDs)
     ├─ approved  → target to autonomy status
     └─ no record → unknown → escalate
```

The target consumes feedback as prose: `restore_from_feedback`
(`src/graph.py:3979`) force-compacts its context, writes `feedback.md`, and
injects a `HumanMessage`. Findings are therefore **rendered with their IDs**
into that text so the next round can be matched back to them. The target is
otherwise unchanged and needs no new tools.

## Constraint interactions

Checked against the constraints recorded in the design vault. Two required
additional changes:

### Project-loop jobs must not escalate to `pending_review`

`orchestrator/services/completion.py` carves loop jobs out of the human-review
gate deliberately: the loop advance hook fires only on terminal statuses, so a
`pending_review` loop job wedges the whole loop forever.

**Therefore escalation is status-aware:**

- ordinary job → `pending_review` with findings attached;
- **loop job → `completed`**, with the unresolved findings written to
  `error_message` so the retro and the next cycle's critic see them. Weak but
  honest, and it matches the existing precedent for a loop job that stops
  without declaring completion.

Note this is *not* a violation of "the target's final status follows the
original job's autonomy" (`verification_phase.md:161-176`) — that constraint
governs the *approved* path. An escalation is explicitly not an approval.
Full-autonomy non-loop jobs **will** now park for a human where they
previously auto-completed; that is the intended behaviour change.

### The unstick watchdog must become ledger-aware

`unstick_reviewing_parents` currently rescues a stuck `reviewing` target only
when **every** critic child is `failed`/`cancelled` (`postgres.py:4500-4512`),
excluding `completed` ones so it cannot race the verdict handler.

Under this design every round leaves a `completed` critic behind, so after
round 1 that condition can never be met again — a target whose round-2 critic
dies would sit in `reviewing` forever. (The hole exists today on the approve
path; this design would make it the norm.)

**New condition:** fire when the target is `reviewing` past the grace period
and **no non-terminal critic child exists**. Then consult the ledger — if a
round record exists for the current round, apply its computed verdict
idempotently (CAS on `status='reviewing'`); otherwise escalate. The ledger
resolves the race that the `completed` exclusion was protecting against, so the
exclusion is replaced by a better mechanism rather than simply dropped.

### Constraints preserved unchanged

No agent-graph changes (`src/graph.py` and `src/core/state.py` untouched);
verdict tools stay strategic-phase-only; `evaluation` injected via
`config_override` only; the critic factory keeps its `autonomy: "full"`
hardcode with safety from `runner_kind='lifecycle'`; `runner_kind` stays
unforgeable; loops are not lifecycle runners; critic output is never merged
into the parent deliverable; a `paused` critic is still treated as live (the
sweeper extension below adds **`waiting` only**, never `paused`); critics keep
`priority=10`; resumes still NULL `freeze_data`; lite backends still get no
critic; `context.verification_target` remains the canonical discriminator.

## Error handling

| Failure | Behaviour |
|---|---|
| Orchestrator unreachable / 5xx at tool time | Tool returns an error; no verdict. Model retries. Persistent failure → no round record → `unknown` → escalate. |
| `orchestrator_client is None` | Fail loudly. |
| Validation failure | 409 naming the specific problem (missing disposition, `RESOLVED` without quote, unknown ID, returned-with-no-findings). |
| Duplicate append | Containment guard makes it a no-op; endpoint returns the existing verdict. Idempotent. |
| Two replicas racing | Single-statement append is lost-update-immune. |
| Critic dies mid-review | Ends `failed`, no record → ledger-aware watchdog fires. |
| `context` read back as a JSON string | Both read sites route through **one** coercion helper — not a tenth ad-hoc parser. See `docs/issues/jsonb_isinstance_guard_without_parse_silent_dead_paths.md`. |
| `max_rounds: 0` (cockpit "Unlimited") | No cap escalation; no-progress escalation still applies. |

**Observability.** Storing both `asserted_verdict` and the computed `verdict`
makes their divergence rate a free, direct measure of critic quality — how
often a critic tries to approve over its own open findings. Log a WARNING on
every divergence. This is currently unobservable.

## Testing

Coverage on this path is currently **zero**:
`_trigger_verification_on_complete` has no direct test,
`TestRoundLimitEnforcement` (`tests/test_critic_loop.py:363-538`) is skipped
with a note that the logic moved to the orchestrator where it was never
re-tested, and `TestVerificationTriggerGuards`
(`tests/test_complete_job_endpoint.py:296-354`) is tautological — it re-asserts
the predicates instead of invoking the guarded function and would pass if the
guards were deleted. Both are repaired as part of this work.

Written TDD-first:

**Unit.** Verdict computation (fold across rounds; blocking threshold; both
divergence directions). Disposition validation (`RESOLVED` without quote;
unknown ID; missing disposition for an open ID; `DISPUTED` keeps open). ID
assignment continuing from max.

**Real Postgres (testcontainers, mirroring `tests/test_atomic_job_context.py`).**
Concurrent appends both land; duplicate append is a no-op; append when
`context` is NULL or the key is missing; round number equals array length.

**Orchestrator flow.** Multi-round continuity — round 1 returns, round 2 spawns
a *fresh* critic whose instructions contain F1 by ID, round 3 cannot approve
while F1 is open. Fail-closed: critic completes with no record → target
`pending_review`, never `completed`. Cap reached with blocking findings →
`pending_review` with findings attached, critic not marked `failed`. Loop job
escalation → `completed` with findings in `error_message`, never
`pending_review`.

**Regression test for the incident, written first.** Identical `head_commit`
plus an open blocking finding escalates and never approves.

## Rollout

In-flight jobs have an empty ledger and simply start at round 0 — worst case,
one extra review round. No data migration.

The computed verdict is mirrored into `freeze_data` during rollout so anything
still reading it keeps working while the ledger becomes authoritative.

Critics currently parked in `waiting` become orphans, since nothing looks for
them any more. The stale-verification sweeper is extended from
`IN ('created','paused')` to include **`waiting`** — which also closes
`docs/issues/stale_critic_waiting_status_escapes_reaper.md` as a side effect.
`paused` critics remain excluded: orphan recovery legitimately parks critics
there and "paused too long ⇒ dead" was evaluated and rejected in
`docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md:105-116`.

## Related

- `docs/issues/verification_round_reset_spawns_blind_critic.md` — incident
  analysis, evidence, and the eight-path fail-open inventory.
- `docs/issues/drain_freeze_overwrites_critic_verdict.md`
- `docs/issues/job_finalization_decisions_held_only_in_process_memory.md`
- `docs/issues/approving_a_critic_wedges_target_in_reviewing.md`
- `docs/issues/jsonb_isinstance_guard_without_parse_silent_dead_paths.md`
- `docs/features/verification_phase.md` — the founding spec.
- `docs/done/critic_subjobs.md` — Open Issue #2 (blind vs. informed critic),
  resolved here: inject findings, never verdicts.
