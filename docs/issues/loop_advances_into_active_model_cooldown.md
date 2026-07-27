---
tags:
  - issue
  - loops
  - llm
  - cooldown
  - orchestrator
related:
  - "[[llm_cooldown_pause_and_resume]]"
  - "[[llm_fallback_model_routing]]"
  - "[[loop_unified_engine]]"
---

# Loop rotates its next member into a model it just learned is in a multi-day cooldown

**Filed:** 2026-07-24, from a live dev incident on 2026-07-23 (project "Research RAG
technologies", loop `528a4f91`). Line numbers are develop @ 2026-07-24.

**IMPLEMENTED 2026-07-25 (Option A + prerequisite, develop, uncommitted):** structured
`classification/model/reset_at` in the fail-fast error dict (`src/graph.py`
`_cooldown_failfast_error`), persisted to the previously-dormant `jobs.error_details`
JSONB on every failed dict-error completion; `_loop_cooldown_park_until` aggregator in
the barrier winner (ANY cooldown-failed member, max reset, 14d cap
`LOOP_COOLDOWN_PARK_CAP_SECONDS`); `park_until` threaded through rotation + all three
campaign spawn sites into `create_loop_job`; born-parked rows created atomically by
`db.create_job(status=, freeze_data=)` (paused + `llm_unavailable` freeze +
`context.llm_outage` WITHOUT `first_failed_at` — the wake-time ceiling landmine); rides
the existing llm_outage sweeper unchanged. 24 new unit tests across 6 suites (advance
park/threading, born-parked row shape, extractor table, sweeper wake, evaluator wake,
completion passthrough); full local suite green. **Owed: k3d live gate** (blocked
2026-07-25 by a host docker-bridge firewall failure on the dev laptop, not by the
change) — full runbook: `tests/loop_cooldown_born_parked_validation.md`.

## Symptom

When a loop member fails because its model hit a **long quota cooldown** (the
agent-side fail-fast for provider resets beyond the 12h pause budget,
`src/graph.py:2768-2807`), the loop engine treats it like any other member
failure: it rotates and **spawns the next member against the same frozen
model**. That member's first LLM call hits the proxy's cached cooldown 429 and
the job dies within ~2 minutes — a guaranteed-doomed spawn.

Observed timeline (UTC, all evidence pulled live from the dev cluster):

| Time | Event |
|---|---|
| 17:23:30 | Upstream OpenAI 429 `usage_limit_reached`, `resets_in_seconds=585034` (~162.5h); codex proxy marks the credential cooling until Jul 30 11:54:04Z |
| 17:25:00 | Iter 3 SCHOLAR (`055bbc8a`, 84 min of real work, 1798 audit rows) hits the cached 429 → fails fast per design |
| 17:25:02 | Advance hook rotates, spawns iter 4 CRITIC (`cdd48881`) |
| 17:27:09 | Iter 4's first LLM call → same cached cooldown → failed (8 audit rows) |
| 17:35:03 | Loop ends `stop_reason=budget` (`remaining_iterations=0` — iter 4 was the last budgeted turn) |

Blast radius here was capped by coincidence (budget exhausted). With budget
remaining, the engine would have kept spawning doomed members until the
consecutive-failures axis tripped (`max_consecutive_failures`, default 3,
`orchestrator/main.py:12504`) — each one burning an iteration from the loop
budget, provisioning + tearing down a workspace, producing a red job card, and
firing failure notifications. The user read the incident as an SRW bug
("shouldn't it pause and wait?"), which is the real cost: the system *knew* the
model was frozen for a week and walked into it anyway.

## Mechanics — every layer is working as designed; the seam between them is the gap

1. **Agent fail-fast is deliberate.** A cooldown whose provider-stated reset
   exceeds `LLM_OUTAGE_CEILING_SECONDS` (12h) fails fast instead of pausing —
   the June-27 fix for the week-long live-lock
   (`docs/done/loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md`).
   Within-budget cooldowns already pause + auto-resume via the `llm_unavailable`
   outage path ([[llm_cooldown_pause_and_resume]]). Not the bug.
2. **The structured signal is dropped at the seam.** The fail-fast return ships
   only free text: `{"error": {"message": …, "type": "llm_error",
   "recoverable": False}}` (`src/graph.py:2799-2807`). `classification:
   "cooldown"` and `reset_seconds` exist at that point in the code (they're
   written to the audit row, `graph.py:2785-2793`) but are **not** in the
   completion payload. The advance hook therefore sees only
   `failed=True` + error text (`orchestrator/main.py:13044-13045`).
3. **Loop failure tolerance is deliberate and cooldown-blind.** The barrier
   winner aggregates the turn, bumps `consecutive_failures`, checks the three
   stop axes (budget / deadline / failures, `main.py:12494-12506`), and rotates
   (`main.py:13066-13115`). Designed for *ordinary* one-off failures; it has no
   notion of "the next spawn is guaranteed to fail until timestamp T".
4. **The proxy makes the doom cheap but certain.** CLIProxyAPI caches the
   cooldown and answers subsequent requests 429 in 1-4 ms without going
   upstream, until the provider-stated reset.

## Root cause

No path carries "model M is in cooldown until T" from a failed member into the
loop engine's spawn decision. The engine's failure tolerance — correct for
transient/one-off failures — is wrong for an environmental failure with a known
end time.

## Fix options

**Prerequisite for A and B (independently useful):** plumb the classification
into the completion payload — extend the fail-fast return
(`src/graph.py:2799-2807`) with `"classification": "cooldown"`,
`"model"`, and `"reset_at"` (absolute epoch, not relative seconds — the payload
is read minutes later), and persist them on the job row via `/complete`. This
also answers the June doc's open question about surfacing a structured
"Model X cooling down until T" to the Cockpit instead of free text.

### Option A — spawn the next member born-parked (recommended)

Rotate exactly as today, but when the completed turn's failure is a long
cooldown, create the next member **pre-parked** instead of dispatching it:
status `paused` with `context.llm_outage.next_retry_at = reset_at` — the same
shape the within-budget cooldown pause already writes. The existing llm_outage
sweeper then re-dispatches it when the window reopens; the proxy has thawed;
the loop continues with zero new machinery.

- Touches only the spawn call in the rotation path; barrier semantics, budget
  accounting, counters, and `_resume_project_loop` (`main.py:13118-13138`)
  stay untouched.
- Reuses the park/wake/re-anchor semantics already hardened by the
  llm_outage_subjob_resilience work (timer rebase, overdue guard).
- Cockpit already renders paused jobs; the member shows as paused-with-wake-time
  rather than a red failure.
- The cooldown-failed turn itself still counts toward `consecutive_failures`
  (it did fail), but the cascade stops: the next member waits instead of
  insta-failing, so the failures axis can no longer be tripped by one quota
  wall.

### Option B — park the whole loop until reset

On barrier aggregation, when the turn failed on a long cooldown: skip the
rotate, set the loop `paused` with a stored `resume_at` + the pending-rotate
inputs, and let a sweeper call `_resume_project_loop` after reset. More
visible (the loop card itself shows paused-until-T) but more machinery: the
barrier is already claimed at that point, so the wake path must re-enter the
rotate with persisted inputs (or re-derive via the torn-advance heal stamps).
Prefer only if product wants loop-level "waiting for quota" UX.

### Option C — orchestrator-wide model-cooldown registry (follow-up, not this issue)

Record `model → reset_at` centrally whenever any job fails/pauses on a
cooldown; dispatch defers *any* job pinned to a cold model (born-parked, as in
A). Protects non-loop jobs too, but needs HA-safe writes, invalidation
(cooldowns can clear early), and interacts with per-credential pools. File
separately if walls keep hitting non-loop dispatches.

### Option D — fallback model routing (existing DRAFT, orthogonal)

`docs/features/llm_fallback_model_routing.md` would switch to a sibling model
instead of waiting. Explicitly out of scope here: it must stay loud + opt-in
(silent wrong-model was the original June defect), and it layers *above*
pause/park — the park is still needed when all candidates are cold.

## Recommendation

Prerequisite + Option A. Smallest engine delta, reuses the entire existing
outage park/sweep machinery, and converts "two red jobs and a dead loop" into
"one red job, one paused member that self-resumes Jul 30".

## Acceptance criteria

- Given a member that failed fast on a cooldown with `reset_at` beyond the
  pause budget, the loop's next member is created parked with
  `context.llm_outage.next_retry_at ≈ reset_at` and is **not** dispatched
  while the cooldown is active.
- The parked member dispatches automatically after `reset_at` (sweeper path)
  and the loop continues normally.
- A quota wall can no longer trip the `failures` stop axis by itself
  (consecutive doomed spawns are impossible).
- Failed-fast cooldown jobs carry structured `classification`/`reset_at` on
  the job row; Cockpit can show "cooling until T" instead of free text only.
- Non-cooldown member failures keep today's exact rotate/stop behavior.

## Evidence appendix (2026-07-23 incident)

- Upstream body (proxy error log
  `/data/auth/logs/error-v1-responses-2026-07-24T012330-22239be7.log`, dev
  proxy pod `srw-codex-proxy`, ns `superhuman-remote-worker`):
  `{"error":{"type":"usage_limit_reached","message":"The usage limit has been
  reached","plan_type":"pro","resets_at":1785412444,"resets_in_seconds":585034}}`
- Proxy gin log separates the real upstream 429 (984 ms) from cached ones
  (1-4 ms). Pool = single credential (the owner's personal Pro account) — see
  memory `srw_codex_proxy_credential_pools`.
- Independently confirmed outside SRW: owner's Codex CLI shows the identical
  block/reset for `gpt-5.3-codex-spark` ("try again at Jul 30th, 2026 1:54
  PM") while the chatgpt.com weekly meter shows 84% remaining — a separate,
  invisible per-model cap.
- Loop row after the incident: `model=gpt-5.3-codex-spark`,
  `role_sequence=["scholar","critic"]`, `max_iterations=4`,
  `remaining_iterations=0`, `stop_reason=budget`, `status=completed`.
- Jobs: iter 3 `055bbc8a-6c2c-427f-a860-44c7a2276d76` (failed 17:25:08Z),
  iter 4 `cdd48881-21d5-47a8-82bc-39f005fe22ae` (created 17:25:02Z, failed
  17:27:09Z).
