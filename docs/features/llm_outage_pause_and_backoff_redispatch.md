# LLM-outage resilience: pause + backoff re-dispatch (don't fail the job)

Status: **IMPLEMENTED + k3d E2E VERIFIED — unit-tested + lint-clean, uncommitted on `develop`**
Date: 2026-07-01
**Updated 2026-07-15 by `[[llm_cooldown_pause_and_resume]]`** (two coupled changes,
both live in code): (1) `cooldown` is no longer a blanket fail-fast Non-goal — a
cooldown whose provider-stated reset fits the pause budget is now promoted INTO this
Tier-2 pause path; only an over-budget/multi-day wall still fails fast. (2) the duration
ceiling / pause budget dropped **24h → 12h** (`LLM_OUTAGE_CEILING_SECONDS` 86400 →
43200). The historical "24h" figures in the prose below refer to the original value;
the live default is 12h. See that doc for the anchored-reset change to
`evaluate_llm_outage` (also live here).
Scope: worker/loop jobs (the LangGraph worker in `src/graph.py`). Persistent
interactive sessions are out of scope for v1 (§Non-goals).
Research provenance: six-agent sweep (4 codebase + 2 web) on 2026-07-01; key
citations inline. Design decisions traceable to that sweep are tagged `[R]`.

## Problem

When the LLM endpoint is unreachable (provider crash, gateway OOM, 5xx, sustained
429), a worker job is **canceled — usually within ~1–2 minutes** — and lands in the
terminal `failed` state, which is **never re-dispatched**. That killed an overnight
self-improvement-loop run when the API crashed at ~3am; by the time the endpoint
recovered the job had been dead for the rest of the night.

Verified current mechanics:
1. **Inner retry** in the `execute` node (`src/graph.py:2353`, `ToolRetryManager`,
   `max_retries=3` from `config/defaults.yaml:196`) → 4 attempts, `1+2+4s` backoff.
   429s override the delay to `max(backoff, Retry-After or 90s)`.
2. **Outer circuit breaker** (`src/graph.py:263`, `_LLM_ERROR_STREAK_CAP=5`) trips
   after 5 consecutive no-progress `execute` invocations and returns an `error`.
3. **`determine_job_status`** (`orchestrator/services/completion.py:274`) does
   `if error: return ("failed", …)` — checked **before** freeze_data, ignoring the
   `recoverable` flag. Any `error` ⇒ `failed`.
4. `failed` jobs are excluded from dispatch (`get_dispatchable_jobs`,
   `postgres.py:2650`, `WHERE status IN ('created','paused')`).

Timing regimes (why 30 min of outage always loses): connection-refused → `transient`
→ plain backoff → dead in **~1–2 min**; sustained 429 → 90s/retry → dead in **~20 min**.
Either way, terminal, and the checkpointed state is never resumed.

## Goals

- A transient LLM outage **pauses** the job (non-terminal) instead of failing it.
- The job **resumes from its checkpoint** when the endpoint recovers.
- Retry cadence is **exponential, capped at 60 min, Full-Jittered** — neither hammering
  a recovering endpoint nor waiting pointlessly long.
- A **24-hour duration ceiling** (plus an attempts backstop): after 24h of continuous
  outage the job fails **loudly, with an operator alert** — a broken config can't park
  a loop iteration forever.
- **Resources freed** during the wait; the design **survives pod restart/OOM/deploy**.

## Non-goals (v1)

- **Permanent** (401/403/404/400-invalid_request) and **billing/quota-exhaustion**
  (OpenAI `insufficient_quota`) errors keep **failing fast** — pausing on a bad key or a
  spend cap helps nobody. **`cooldown` was v1-Non-goal but is now conditional** (updated
  2026-07-15, `[[llm_cooldown_pause_and_resume]]`): a `model_cooldown` whose reset fits
  the 12h budget pauses via this path; an over-budget/multi-day wall still fails fast.
  Only *transient* unavailability (and now short cooldowns) pause (see §Error taxonomy). `[R]`
- **Persistent interactive sessions** (`src/persistent_graph.py`) keep current behavior.
- No cockpit change beyond surfacing the pause reason (deferred, §Deferred).

## Hard preconditions `[R]`

These are not optional — the safety argument depends on them.

1. **`CHECKPOINTER_BACKEND=postgres`.** The "resume from checkpoint, no replay,
   no duplicated side-effects" guarantee holds **only** on the shared Postgres
   checkpointer. On the pod-local `sqlite` backend, parking frees the agent →
   re-dispatch lands on a *different* pod → cold-start, which
   `docs/done/cross_pod_resume_cold_starts_checkpoint_not_replicated.md:162-167`
   warns "can duplicate non-idempotent early side effects (file creation, external
   calls, git commits)." Postgres is already the Helm chart default (same doc, :265);
   gate the feature on it and no-op (fall back to today's fail-fast) on sqlite.
2. **Single-layer retries.** Retries multiply across layers — LiteLLM gateway ×
   provider SDK (`max_retries`) × the worker loop → up to 64×/243× amplification
   (Google SRE; AWS Builders' Library). **Implemented:** the *main worker LLM*'s SDK
   retries are the multiplier that matters (it runs inside the graph retry loop), and
   its `max_retries` comes from `config.llm.max_retries` (`config/defaults.yaml`) — now
   **`0`**, so the graph's `retry_manager` (`limits.llm_inproc_retries`, Tier 1) is the
   single retry layer. (The aux/citation clients at `src/agent.py:492,554` stay at `1`
   — they run *outside* the worker loop, so they aren't the amplifier; the earlier plan
   to zero those was a mis-target.) Gateway `num_retries=0` remains a deployment-side
   follow-up (LiteLLM proxy config), noted in §Deferred. `[R]`

## Design — two tiers, split at the re-dispatch cold-start cost (~30–60s)

"Retry a few times, *then* wait" is already two tiers. The boundary is principled:
below the cost of teardown + re-provision + resume (~30–60s) it's cheaper to stay
in-process; above it, pause and free everything. Freeing the worker during a long
wait is the unanimous durable-execution pattern (Temporal "does not tie up the
process"; Azure Durable Functions literally "Don't use Thread.Sleep()"). `[R]`

### Error taxonomy — decide the class *before* choosing tier `[R]`

`_classify_llm_error` (`src/graph.py:317`) already returns `permanent` / `cooldown` /
`rate_limit` / `transient` / `auth_unavailable`. Two refinements:

- **Add a `quota_exhausted` fail-fast class** for OpenAI `insufficient_quota` (and
  Google `RESOURCE_EXHAUSTED`) — these are 429s today, so the current code retries
  them, but no wait fixes a spend cap. Route on the typed `error.code`/`error.type`,
  not the bare 429. Treat like `permanent` (fail fast, actionable "check billing" msg).
- **Anthropic `529 overloaded_error`** (servers overloaded across *all* users — the
  canonical shared-outage/thundering-herd case) already falls in the 5xx→`transient`
  bucket, which is correct: it flows to Tier 2 and gets the long Full-Jittered backoff.

Retriable → Tier 1 then Tier 2: connection/timeout/408, 409, `rate_limit` (429
per-minute), 5xx, `529`, `auth_unavailable`. Fail-fast (never enters the retry
window): `permanent`, `quota_exhausted`, and an **over-budget `cooldown`**.
(Updated 2026-07-15: a within-budget `cooldown` now enters Tier 2 directly from the
execute node — `[[llm_cooldown_pause_and_resume]]` — rather than failing fast.)

### Tier 1 — in-process fast retries (blips)

Keep the existing inner loop, lightly tuned: **5 retries**, `1→2→4→8→16s` (cap 30s),
**honor `Retry-After` / `anthropic-ratelimit-*-reset` over computed backoff** (the
existing `_extract_rate_limit_delay` already does this — validated `[R]`). Covers a
~60s hiccup with zero teardown, context stays hot.

### Tier 2 — pause → scheduled re-dispatch (outages)

**A. Freeze construction (agent).** When Tier 1 is exhausted on a retriable class,
the `execute` node **freezes immediately at the exhaustion point** (`src/graph.py:2385`,
inside the LLM-invoke `except`, *before* the `tools` node and *before* the
`_llm_error_streak += 1` circuit-breaker block). It returns **state-based freeze,
no file**:

```jsonc
return {
  "freeze_data": {
    "freeze_type": "llm_unavailable",
    "classification": "rate_limit",   // transient | rate_limit | auth_unavailable
    "error_summary": "<truncated last error>",
    "model": "<phase model>",
    "retry_after_seconds": 90          // OPTIONAL: server-directed delay, if the error carried one
  },
  "should_stop": true,
  "iteration": iteration + 1
}
```

**Crucially no `error` key** — else `determine_job_status` short-circuits to `failed`
(`completion.py:274`). Mirror the `memory_unavailable` shape (`src/agent.py:746`,
which writes no `job_frozen.json`); the modern signal is `should_stop`+`freeze_data`
in state, not the legacy file (`src/graph.py:3108`). `[R]`

*Why freeze immediately and not at the next phase boundary:* the freeze point is
already side-effect-clean (the LLM call failed → no `tools` node ran → nothing to
duplicate on resume). `version_upgrade` defers to a boundary only because a drain
intent arrives at an arbitrary, possibly-dirty moment; our trigger is clean by
construction, and deferring is impossible anyway — you can't reach the next boundary
*because* the LLM is down. `[R]`

**B. `/complete` handler (orchestrator).** New branch mirroring the
`memory_unavailable` branch at `orchestrator/main.py:10346-10368`. On
`freeze_type=="llm_unavailable"`:

1. Read `context.llm_outage = {attempt, first_failed_at, last_failed_at}` (parse
   context-as-str-or-dict exactly like `completion.py:351-357`).
2. **Auto-reset:** if `now - last_failed_at > RESET_WINDOW` (30 min) the job ran fine
   in between → `attempt=0`, `first_failed_at=now`. (`memory_retry_count` is never
   reset today — this reset logic is net-new. `[R]`)
3. **Ceiling checks** (either bound trips a loud fail):
   - `now - first_failed_at >= 24h` (primary, duration-based — the Temporal
     `scheduleToCloseTimeout` model `[R]`), **or**
   - `attempt >= MAX_ATTEMPTS` (backstop = 60; defends against pathological fast
     re-fail loops that Full Jitter's short early draws could rack up `[R]`).
   → mark **`failed`** with an actionable message *and* emit an operator alert
   (dead-letter-and-alert, not silent give-up `[R]`; reuse the
   `_NOTIFIABLE_FREEZE_TYPES` notification path at `main.py:10421`, but only for the
   terminal fail — the pauses stay quiet).
4. Else: `attempt += 1` (atomic `jsonb_set` helper copying `increment_job_memory_retry`,
   `postgres.py:1479` `[R]`); compute `next_retry_at = now + backoff_with_full_jitter(attempt)`,
   floored by any `retry_after_seconds` from the freeze; **write the full `freeze_data`
   including `next_retry_at`** (targeted `UPDATE jobs SET freeze_data=$1::jsonb`, like
   `main.py:10173`), then call **`pause_job(job_id)`** — which frees the agent
   (`assigned_agent_id=NULL`) and sets `status='paused'` while **leaving `freeze_data`
   intact** (verified body, `postgres.py:958-970`). Set `new_status=None` so the generic
   status write + loop-advance don't re-handle (mirrors `main.py:10368`). **Do NOT call
   `_trigger_dispatch()`** — the sweeper owns re-dispatch (and `freeze_data IS NULL`
   would block it anyway). `[R]`

*No new `park_job_frozen()` helper is needed* — `pause_job` already is the
freeze-preserving park. (A 1-line named alias is fine for readability.) `[R]`

*Workspace:* `pause_job` releases the agent; the lifecycle reconciler later reaps the
idle **pod** but **keeps the sandbox PVC** (that's what enables resume). Do **not** call
`container_provisioner.delete_workspace`. Graph state is portable via the Postgres
checkpointer; workspace files (`todos.yaml`/`plan.md`/`archive/`) survive on the PVC. `[R]`

**C. Sweeper (orchestrator).** New `llm_outage_redispatch_sweeper(shutdown_event)`,
`main.py`-inline style, near-verbatim clone of `delegation_timeout_sweeper`
(`main.py:9478`) `[R]`. Registered in the lifespan under **`run_when_leader(...)`**
(`main.py:5709` pattern) so N replicas don't double-dispatch. Each ~30s tick:

- `SELECT` due jobs: `status='paused' AND assigned_agent_id IS NULL AND
  freeze_data->>'freeze_type'='llm_unavailable' AND
  (freeze_data->>'next_retry_at')::timestamptz <= now()`.
- **Optional health gate** (see §Health-gating): skip a due job whose dependency probe
  is red, re-arming without advancing the attempt counter.
- For each due job, **atomic CAS clear** `claim_llm_outage_redispatch(job_id)` (modeled
  on `claim_delegation_resume`, `postgres.py:2600`): `UPDATE … SET freeze_data=NULL,
  assigned_agent_id=NULL WHERE id=$1 AND status='paused' AND
  freeze_data->>'freeze_type'='llm_unavailable' AND next_retry_at due` — so exactly one
  sweeper wins even in a transient dual-leader window. Belt-and-suspenders with
  `run_when_leader`. `[R]`
- **Ceiling backstop:** also fail-loud any due job already past 24h / MAX_ATTEMPTS
  (defense-in-depth with the `/complete` check; route through the same terminal path so
  the loop counts it once). `[R]`
- Call `_trigger_dispatch()` **once per tick** after clearing (dispatcher pulls up to
  50); the worker resumes from its checkpoint. `context.llm_outage.attempt` survives
  because it lives in `context`, not `freeze_data`. `[R]`

`get_dispatchable_jobs` needs **no change** — `freeze_data IS NULL` (`postgres.py:2652`)
already parks the job until the sweeper clears it. (Note: a parked *parent* also blocks
its children via the ancestor cascade guard, `postgres.py:2660` — correct for our
top-level loop/worker jobs. `[R]`)

### Backoff schedule + Full Jitter `[R]`

Base 30s, cap 60min, coefficient 2 (the universal default across Temporal / Step
Functions / Airflow / Celery). Deterministic envelope
`expo(n) = min(3600, 30·2^(n-1))` = 30, 60, 120, 240, 480, 960, 1920, then 3600s from
attempt 8. Apply **Full Jitter** (AWS's and the Anthropic/OpenAI-fleet recommendation
for shared-outage thundering-herd; it's what AWS's own SDKs default to):

```
next_retry_at = now + random_uniform(0, expo(attempt))     # floored by retry_after_seconds
```

Full Jitter (draws from the *entire* `[0, envelope]`) is chosen over the doc's original
±20% band because our worst case is a whole fleet of loop jobs failing together on a
shared 529/gateway outage and waking together. ±20% only spreads a ~24-min band around
the hour; Full Jitter flattens uniformly across the hour and ≈halves total load.
**Full Jitter is locked for v1** (paired with health-gating, which prevents wasted
near-immediate re-hits into a still-down dependency). `equal` (`expo/2 + random(0,
expo/2)`, a ≥50% floor) stays available via `llm_outage_jitter` as a conservative
fallback. `[R]`

### Health-gated resumption (v1) `[R]`

Both web agents flagged this as high-value *specifically because of our LiteLLM
gateway-OOM history*: a blind timer re-dispatches into a still-dead gateway, fails
instantly, and burns the attempt/24h budget — the exact wasted-cycle problem
health-gating solves (AWS: "retry only when we observe that the dependency is
healthy"; circuit-breaker Half-Open). The hook: before the sweeper's CAS clear, if the
job's model routes through the gateway and `gateway_is_healthy()` is red, skip (defer,
don't reset attempt). This is circuit-breaker Half-Open without a full state machine.
Ties into the standing "alert on `gateway_is_healthy=false`" TODO. **Ships in v1**
(feature-flagged via `llm_outage_health_gate`, default on). A job whose model routes
*directly* (not through the gateway) has no probe → not gated (falls through to the
timer), which is correct: direct-provider outages self-heal on the normal backoff.

## Checkpoint/resume safety — verdict `[R]`

**Safe**, given the preconditions. The frozen invocation ran `execute → check_todos →
check_goal → END` with **no `tools` node executed** (the LLM call failed before any
tool ran), so the checkpoint carries no un-replayed side effects. On re-dispatch,
`"paused"` ∈ `GRACEFUL_STOP_STATUSES` (`src/agent.py:807`) → `_resume_from_checkpoint`
→ `ainvoke(None)` → `route_entry → restore_todo_state` (which **explicitly clears
`should_stop`**, `src/graph.py:3369` — this node exists for freeze-then-resume) →
`execute` re-runs the failed LLM call. The `_llm_error_streak` closure is in-memory and
re-initialized to `[0]` each dispatch (`src/graph.py:894`) — nothing to reset; the
durable cross-dispatch counter is `context.llm_outage.attempt`. One benign nuance:
pre-LLM soft-state ops (pre-compaction capture, legacy `decrement_ttl`) can re-run on
resume, but they're gated on compaction actually firing and the aux LLM is down during
an outage anyway → they no-op. No consequential re-execution. `[R]`

## Loop semantics `[R]`

- **Terminal-gated advance** (`main.py:10531`, `if job.status in
  ("completed","failed","cancelled")`) means a `paused` job never rotates the loop or
  burns the failure budget. Ceiling→`failed` counts as exactly one failure + one
  iteration. This is the intended overnight-loop behavior, inherited for free.
- **Nothing reaps a paused job** — all stuck/orphan detection is scoped to
  `status='processing'`; the one age-based cancel is critic-subjob-only. A 24h pause is
  safe from reaping.
- **⚠️ `run_until` bleed:** a loop with a wall-clock deadline keeps consuming it during
  the pause (`_loop_deadline_passed`, `main.py:9852`). A 24h outage can silently exhaust
  an overnight loop's *date* budget, so it may stop on `deadline` the moment the job
  resolves. The *iteration* budget is untouched. Acceptable (the outage really did
  consume wall-clock), but document it; optionally add a per-loop knob to extend
  `run_until` by paused-time.

## Implementation map (exact insertion points) `[R]`

1. **`src/graph.py` execute node, ~:2385–2447** — for `classification in
   ("transient","rate_limit","auth_unavailable")` on Tier-1 exhaustion, return the
   `llm_unavailable` freeze (above) *instead of* the circuit-breaker `error` return
   (:2439). Leave `permanent`/`cooldown` returns as `error`. Add the new
   `quota_exhausted` fail-fast class in `_classify_llm_error` (:317).
2. **`config/defaults.yaml` `llm.max_retries: 0`** — the main worker LLM's SDK retries
   (single-layer retries; the aux/citation clients at `src/agent.py:492,554` stay at `1`).
3. **`orchestrator/services/completion.py`, after the memory block ~:345/:365** — add
   `if freeze_type == "llm_unavailable":` → `("paused", None)` under ceiling,
   `("failed", <msg>)` at ceiling (read `context.llm_outage`).
4. **`orchestrator/main.py` `/complete`, new branch mirroring :10346–10368** — the §Tier-2-B
   logic; `pause_job` + `new_status=None`; no `_trigger_dispatch`.
5. **`orchestrator/main.py`** — `llm_outage_redispatch_sweeper` (near `delegation_timeout_sweeper`
   :9478) + lifespan registration under `run_when_leader` (:5709 pattern) + shutdown await
   (:5860).
6. **`orchestrator/database/postgres.py`** — `list_due_llm_outage_jobs`,
   `claim_llm_outage_redispatch` (CAS, model `claim_delegation_resume` :2600),
   `increment_job_llm_outage_attempt` (model `increment_job_memory_retry` :1479),
   `fail_llm_outage_job`. `get_dispatchable_jobs` unchanged.

## Config surface (`config/defaults.yaml` `limits:` + env)

| Key | Default | Meaning |
|---|---|---|
| `llm_inproc_retries` | 5 | Tier-1 in-process fast retries |
| `llm_outage_backoff_base_seconds` | 30 | first Tier-2 envelope |
| `llm_outage_backoff_cap_seconds` | 3600 | 60-min cap |
| `llm_outage_jitter` | `full` | `full` \| `equal` |
| `llm_outage_ceiling_seconds` | 43200 | 12-h duration ceiling (primary); also the cooldown pause cutoff (updated 2026-07-15, was 86400/24h) |
| `llm_outage_max_attempts` | 60 | attempts backstop |
| `llm_outage_reset_window_seconds` | 7200 | gap that resets the attempt counter — **must exceed the backoff cap** (see below) |
| `LLM_OUTAGE_SWEEP_SECONDS` | 30 | sweeper tick |
| `llm_outage_health_gate` | `true` | skip re-dispatch while gateway probe red |

## Acceptance criteria

1. A worker job whose LLM endpoint is down (connection refused) is **not** `failed`
   after ~1 min; it becomes `paused` + `freeze_type=llm_unavailable`, agent/workspace-pod
   released, **PVC retained**.
2. The sweeper re-dispatches after the scheduled backoff; on recovery the job **resumes
   from checkpoint** (no cold-start replay, no duplicated tool side-effects) and completes.
3. Backoff follows `min(30·2^(n-1),3600)` with **Full Jitter**; `attempt` persists across
   re-dispatches via `context.llm_outage`; a server `Retry-After` floors the first wait.
4. After 24h (or `MAX_ATTEMPTS`) of continuous outage the job fails with an actionable
   message **and an operator alert**; the loop counts it as a failure then and only then.
5. `permanent` (401/404), `cooldown` (multi-day quota), and `quota_exhausted`
   (`insufficient_quota`) **fail fast** — never paused.
6. A paused-for-outage loop iteration does **not** advance the loop or burn the failure
   budget; a 24h pause is not reaped.
7. Two genuinely separate outages (a multi-hour productive gap between them, i.e.
   longer than `reset_window` > the backoff cap) start fresh (attempt counter +
   duration ceiling reset); a long *backoff wait* within one sustained outage does
   **not** reset.
8. With `CHECKPOINTER_BACKEND=sqlite`, the feature no-ops to today's fail-fast (no unsafe
   cold-start re-dispatch).
9. Concurrent sweeps / duplicate `/complete` cannot double-dispatch (CAS + leader gate)
   or double-count the attempt (atomic increment).
10. While `gateway_is_healthy()` is red, the sweeper defers re-dispatch of
    gateway-routed jobs **without advancing the attempt counter**, and resumes dispatch
    once the probe is green; direct-provider jobs are unaffected.

## Verify plan

- **Unit**: `_classify_llm_error` (+ new `quota_exhausted`); `completion.py` mapping
  (`llm_unavailable`→paused; ceiling & max-attempts→failed); backoff+Full-Jitter bounds
  and `Retry-After` floor; reset-window; sweeper due/not-due + CAS idempotency + ceiling
  backstop; health-gate skip (probe red → deferred, attempt not advanced; probe green →
  dispatched).
- **k3d E2E**: stop `srw-litellm` (or block egress), submit a worker/loop job, confirm
  `paused`+`freeze_type` + agent freed + PVC kept; watch the sweeper re-dispatch on
  schedule; restore the endpoint; confirm resume-from-checkpoint + completion. Shrink
  `llm_outage_ceiling_seconds`/`max_attempts` in a test overlay to exercise the loud-fail
  path quickly. Verify a paused iteration doesn't advance the loop.

## Decisions & risks

- **Locked 2026-07-01:** health-gating ships in **v1**; jitter is **Full**. (The two
  are coupled — Full Jitter's short early draws are safe because the health gate blocks
  wasted re-hits into a still-down gateway.)
- **Reset window must exceed the backoff cap (found during implementation).** The
  auto-reset compares `now − last_failed_at` to the reset window. But during Tier-2
  backoff, the gap between *consecutive failures* IS the backoff wait (up to the 60-min
  cap) — the job was parked, not running fine. A window ≤ the cap (the original 30-min
  value) would misread that idle wait as "ran fine" and spuriously reset `first_failed_at`
  every long cycle → **both ceilings would restart forever and a sustained outage could
  park a loop indefinitely** (the exact thing the ceiling prevents). Fixed by defaulting
  `reset_window = 7200s` (2h > 1h cap); the invariant is asserted in a unit test.
- **Health gate is fleet-granular in v1.** The sweeper defers *all* due jobs when the
  gateway is enabled + unhealthy (not per-job routing). A direct-routed job in a
  gateway-enabled fleet is thus deferred one cycle during a gateway outage — harmless
  (counter untouched). Per-job routing precision (a `via_gateway` flag stamped at freeze
  time) is deferred.
- **Sweeper ceiling backstop → loop advance via the safety net.** When the sweeper
  fail-loud's a job past the ceiling (rare; the common ceiling path is `/complete`), it
  sets `status=failed` directly. The existing `project_loop_sweeper` safety net ("terminal
  without advance") rotates the loop on its next tick, so no loop-advance coupling is
  added to the outage sweeper.
- **Latent side-finding (not blocking):** the exhaustive trace found **no code clears
  `freeze_data` for the `memory_unavailable`/`version_upgrade` paths**, so their
  "immediate re-dispatch" appears gated shut by their own retained `freeze_data`. Our
  sweeper avoids this by explicitly clearing. Worth a separate bug check on those paths.

## Implementation notes (2026-07-01)

Built on `develop`, uncommitted. Files touched:

- **`src/graph.py`** — `_is_insufficient_quota` helper + `quota_exhausted` class in
  `_classify_llm_error`; `quota_exhausted` fail-fast branch; the `llm_unavailable` freeze
  at Tier-1 exhaustion (gated on `checkpointer_backend()=="postgres"`); `retry_manager`
  now uses `limits.llm_inproc_retries`.
- **`config/defaults.yaml`** (`llm.max_retries: 0`, `limits.llm_inproc_retries: 5`),
  **`src/core/loader.py`** (`LimitsConfig.llm_inproc_retries` + both population sites),
  **`config/schema.json`**.
- **`orchestrator/services/completion.py`** — env-tunable constants, `_parse_context` /
  `_parse_ts`, `evaluate_llm_outage` (reset + ceiling), `llm_outage_backoff_seconds`
  (Full/Equal jitter + Retry-After floor), and the `llm_unavailable` branch in
  `determine_job_status`.
- **`orchestrator/database/postgres.py`** — `increment_job_llm_outage_attempt`
  (row-locked atomic, applies reset), `list_due_llm_outage_jobs`,
  `claim_llm_outage_redispatch` (CAS clear), `fail_llm_outage_job` (terminal CAS).
- **`orchestrator/main.py`** — `/complete` `llm_unavailable` branch (park + next_retry_at,
  no `_trigger_dispatch`; operator alert on terminal fail), `llm_unavailable` case in
  `_format_freeze_notification`, `_llm_outage_sweep_once` + `llm_outage_redispatch_sweeper`
  + lifespan registration under `run_when_leader` + shutdown await.
- **Tests** — `tests/test_llm_outage_resilience.py` (evaluate/backoff/determine),
  `tests/test_llm_outage_sweeper.py` (sweep tick: due/CAS/ceiling/health-gate), plus
  `quota_exhausted` cases in `tests/test_graph_helpers.py::TestClassifyLlmError`. All green;
  `ruff check`/`format` clean tree-wide.

### k3d E2E result (2026-07-01) ✅

Ran an **isolated** outage (a `developer` worker job pinned via `config_override.llm`
to a dead endpoint `http://127.0.0.1:9/v1` — the `_inject_model_credentials`
base_url+api_key early-return keeps it, so only this job is affected; no need to stop
the shared gateway). Observed on the freshly-rebuilt cluster:

- **Tier 1:** `LLM error (attempt 1/5 … 5/5)` with `1.1 → 2.2 → 4.2 → 8.7 → 16.6s`
  backoff against `Connection error.`, then the freeze at 6 in-process attempts:
  `LLM endpoint unavailable (transient) … freezing for pause+backoff re-dispatch`.
- **Pause (not fail):** job → `paused`, `freeze_type=llm_unavailable`, `attempt=1`,
  `next_retry_at` set (~25s, within the attempt-1 Full-Jitter [0,30s] envelope), agent
  freed. `Job … paused for LLM outage — attempt 1, next_retry_at=… (classification=transient)`.
- **Sweeper re-dispatch:** leader replica logged `LLM-outage sweeper: re-dispatched 1,
  failed 0 (of 1 due)`; `freeze_data` CAS-cleared → job resumed to `processing` with
  **`attempt` persisted** in `context.llm_outage`.
- **Escalation:** second cycle → `attempt=2`, longer `next_retry_at` (attempt-2 [0,60s]
  envelope). Backoff grows, counter persists across re-dispatches.
- **Health gate:** correctly inert — `LITELLM_BASE_URL` empty on this cluster (all-direct
  fleet), so no deferral.
- **Loud-fail ceiling:** forcing `attempt=60` (the backstop) → sweeper failed it:
  status `failed`, `error_message="LLM endpoint unavailable past the give-up ceiling
  (attempts, 60 attempts) — … Check the model endpoint/provider (Admin → Models)."` +
  **operator alert** (`Freeze notification sent … (llm_unavailable)`), `re-dispatched 0,
  failed 1`.

Not exercised live (low-risk, unit-covered): resume-to-**completion** on a *recovering*
endpoint (this cluster had no configured real model to switch to; the re-dispatch→resume
mechanism itself is proven — each cycle graceful-resumes the paused job from its
checkpoint and re-attempts, rather than cold-starting), and the direct `quota_exhausted`
fail-fast (same classifier path, unit-tested).

**Finding — subjobs bypass outage handling.** The auto-spawned **scholar** subjob emitted
`freeze_type=llm_unavailable` correctly (agent code fires for subjobs too), but
`determine_job_status`'s subjob branch (`parent_job_id is not None`,
`completion.py`) short-circuits to `pending_review` **before** the `llm_unavailable`
check — so scholar/critic subjobs get no pause/backoff, they surface as `pending_review`
to the parent. Acceptable for v1 (scoped to top-level worker/loop jobs; a real outage
pauses the parent anyway), but a **loop caveat**: if a loop role is modeled as a subjob
it won't get outage resilience. Tracked as a follow-up (route retriable `llm_unavailable`
before the subjob branch, or pause the subjob and let the parent inherit).

Remaining: commit + push (and, if desired, a resume-to-completion demo once a real model
endpoint is configured on k3d).

## Deferred

- **Cockpit surfacing**: "Paused — waiting on LLM, next retry ~HH:MM (attempt N)".
- **Fleet-wide retry budget** (token bucket) so a global 529 can't turn N workers into an
  aggregate storm — a layer above per-job jitter. `[R]`
- **Per-loop ceiling / `run_until`-extension** knobs.
- **Persistent sessions**: a shorter in-process-only variant if desired.
