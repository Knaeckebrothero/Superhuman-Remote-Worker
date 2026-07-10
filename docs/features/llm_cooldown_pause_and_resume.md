# Cooldown-aware pause: wait out a short quota cooldown instead of failing the job

Status: **PROPOSED / DESIGN — not implemented; reset-anchor decision RESOLVED 2026-07-10**
Date: 2026-07-07 (design decision resolved 2026-07-10)
Scope: worker/loop jobs (the LangGraph worker in `src/graph.py`). Extends
`[[llm_outage_pause_and_backoff_redispatch]]` — reuses its Tier-2 pause →
scheduled re-dispatch machinery wholesale; the only new logic lives in the
`cooldown` branch of the `execute` node. Persistent interactive sessions remain
out of scope (same boundary as the sibling doc).
Provenance: live investigation on the **main** cluster, 2026-07-07 — three
consecutive loop iterations hard-failed on a ~2.1h `gpt-5.5` quota cooldown.

## Problem

A `model_cooldown` 429 ("all credentials cooling down") **fails the job fast,
non-recoverable** (`src/graph.py:2406-2454`). This is the intended Track-2/C1 fix
from `[[loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown]]`, which
cured a 5.5-day cooldown that **live-locked** a loop for 5+ hours. And
`[[llm_outage_pause_and_backoff_redispatch]]` §Non-goals reaffirms it: *"cooldown
(`model_cooldown` multi-day quota) … keep failing fast — pausing 24h … helps
nobody."*

**Both rest on the assumption that a cooldown is a multi-day quota wall.** That is
often false. A codex/ChatGPT subscription (and OpenAI usage-tier limits) reset on
a **rolling window of a few hours**, not days. When that trips, the right behavior
is exactly what the sibling feature already does for transient outages: **pause,
wait out the window, resume from checkpoint** — no operator, no lost iteration.

### Incident evidence (2026-07-07, main cluster)

Three consecutive loop iterations of the *Hotel Rheinland ERP* self-improvement
loop failed after the 05:15Z success:

| Job | Iter · role | Model | Failure |
|---|---|---|---|
| `87344382-…` | 13 · SCHOLAR | `gpt-5.5` (codex-proxy) | `cooldown`, `recoverable:false`, `reset_seconds≈7803` (~2.2h) |
| `b333108f-…` | 14 · BUGHUNTER | `gpt-5.5` (codex-proxy) | `cooldown`, `recoverable:false`, `reset_seconds≈7673` (~2.1h) |
| `f0b6f263-…` | 12 · DEVELOPER | `MiniMax-M3` | *unrelated* — shell command hung >60s, terminated with no LLM error (out of scope here) |

Both cooldown errors reported a reset **~2.1–2.2h out** — a real provider quota
window returned in the 429 body, an order of magnitude *under* the 24h pause
ceiling the sibling feature already tolerates. Had these two been routed to the
outage-pause path, each would have paused ~2h and resumed, and the loop would have
continued unattended.

## Goals

- A cooldown whose **stated reset fits inside the pause budget** (≤ the 24h
  ceiling) **pauses** the job (non-terminal) and **resumes from checkpoint** when
  the window reopens — driven by the provider's own `reset_seconds`, not a
  hardcoded wait.
- A cooldown whose reset is **longer than the pause budget** (the original
  multi-day wall) still **fails fast** with the actionable operator message —
  unchanged.
- **No new subsystem.** Reuse the sibling's `llm_unavailable` freeze → `/complete`
  park → outage sweeper → resume path verbatim. The `retry_after_seconds` floor
  and 24h/60-attempt ceilings it already ships are exactly the primitives needed.

## Non-goals

- **`quota_exhausted`** (OpenAI `insufficient_quota` — a billing/spend-cap 429 with
  *no* time-based reset) keeps **failing fast**. Confirmed with the user
  (2026-07-07): out-of-money needs a human; pausing 24h re-hitting a spend cap
  wastes pod-boots and helps nobody. This carve-out is deliberate.
- **`permanent`** (401/403/404/400-invalid_request) keeps failing fast.
- **Fallback-model routing** (dispatch a cooldown-benched role onto a sibling
  model instead of pausing) — a larger, orthogonal change; the sibling doc's open
  question *"should a loop role carry a fallback model?"* Tracked separately, not
  in this doc.
- **Persistent interactive sessions** — same boundary as the sibling.

## Design — threshold the cooldown by its reset window

The classifier (`_classify_llm_error`, `src/graph.py:366`) still returns
`cooldown`; only the *handling* changes. `_cooldown_detail` (`src/graph.py:319`)
already extracts `(reset_seconds, model)` from the error body. The decision:

```
reset_s, cd_model = _cooldown_detail(e)

if reset_s is not None
   and reset_s <= COOLDOWN_MAX_PAUSE_SECONDS        # fits the pause budget
   and checkpointer_backend() == "postgres":         # same precondition as Tier-2
    → return the llm_unavailable freeze (below), classification="cooldown",
      retry_after_seconds = reset_s
else:
    → keep today's fail-fast error (multi-day wall, unknown reset, or sqlite)
```

`COOLDOWN_MAX_PAUSE_SECONDS` defaults to `LLM_OUTAGE_CEILING_SECONDS` (24h) — "if
we can't wait it out inside our max pause budget, don't pause at all." One knob,
one meaning.

The freeze return is **identical to the sibling's Tier-2 freeze** (`src/graph.py:2526`),
only the classification differs and `retry_after_seconds` is seeded from the
cooldown's stated reset rather than from a `Retry-After` header:

```jsonc
return {
  "freeze_data": {
    "freeze_type": "llm_unavailable",
    "classification": "cooldown",
    "error_summary": "<model X in quota cooldown; resets in ~Nh>",
    "model": "<phase model>",
    "retry_after_seconds": <reset_s>          // floors the first backoff to the true window
  },
  "should_stop": true,
  "iteration": iteration + 1
}
```

**Everything downstream is unchanged and already implemented:**
- `/complete`'s `llm_unavailable` branch parks the job (`pause_job`, agent freed,
  PVC retained) and schedules `next_retry_at = now + backoff`, where
  `llm_outage_backoff_seconds` **floors the wait by `retry_after_seconds`**
  (`completion.py:427-448`) → the first re-dispatch lands ~`reset_s` out (one long
  sleep, *not* a per-minute hammer).
- The outage sweeper re-dispatches on schedule; the worker resumes from its
  Postgres checkpoint (side-effect-clean — the LLM call failed before any `tools`
  node ran).
- The 24h duration ceiling + 60-attempt backstop already bound it: a cooldown that
  outlives its stated reset can't park the loop forever; past the ceiling the job
  fails **loudly with an operator alert**.

Net: one branch in `src/graph.py`, a config constant, and the anchored-reset
fix in `evaluate_llm_outage` + `next_retry_at` persistence (§Design decision).
Sweeper and dispatch untouched — the `llm_unavailable` path is
classification-agnostic.

## Design decision (RESOLVED 2026-07-10) — anchor the auto-reset at the end of the scheduled wait

### The trap

The sibling's auto-reset (`evaluate_llm_outage`, `completion.py:400-408`) zeroes
the attempt counter + duration ceiling when `now − last_failed_at >
LLM_OUTAGE_RESET_WINDOW_SECONDS` (**2h**), reading a long gap since the last
failure as "the job ran fine in between → fresh outage." That proxy holds for
transients only because their backoff wait is capped at **1h < 2h** — the
sibling §Decisions pins the invariant `reset_window > backoff cap` for exactly
this reason.

A cooldown breaks the proxy: we deliberately schedule a wait of `reset_s`
(2–5h) — beyond the 2h window. On re-dispatch against a still-cooling model the
next failure lands `reset_s + ε` after the last one → the reset **spuriously
fires**, zeroing `first_failed_at` → the 24h ceiling restarts every cycle → a
never-clearing cooldown parks the job **indefinitely** — the exact failure the
ceiling exists to prevent.

### Rejected: skip the reset when the freeze is a cooldown

The seemingly-minimal fix — suppress the gap-reset when the current (or
persisted prior) `classification == "cooldown"` — has a latent **false-fail**.
`context.llm_outage` is never cleared on success; the gap-reset *is* its
cleanup. So a long-lived job that hit a cooldown once, cleared it, ran
productively for days, then hit a **second** cooldown would suppress the reset,
inherit the ancient `first_failed_at`, see `elapsed ≥ 24h`, and **hard-fail
instantly** on a 2h cooldown it should have paused through. Patching that
("only suppress when the gap is explained by the scheduled wait") requires
persisting the scheduled wait — at which point the anchor below is *simpler*:
classification never enters the evaluator at all.

Also rejected for v1: **progress-based reset** (reset only on a real successful
LLM call since `first_failed_at`, via a `last_success_at`). The fully-general
answer, but the orchestrator only hears from the agent at freeze/complete time
— mid-job successes are invisible to `evaluate_llm_outage` without breaking its
purity (querying `llm_requests`) or new agent→orchestrator signaling. The
anchor reaches the same verdict in every case on the table below.

### Chosen: measure idle time from when the job could actually run again

Persist the scheduled resume time into the durable outage record and anchor the
reset comparison there:

- **Park time** (`/complete` `llm_unavailable` branch): compute the backoff
  first, then write `next_retry_at` into `context.llm_outage` in the **same
  atomic write** as the attempt increment (extend
  `increment_job_llm_outage_attempt`). Today `next_retry_at` survives only in
  `freeze_data`, which the sweeper CAS-clears at re-dispatch.
- **Evaluate time**: `anchor = max(last_failed_at, prev_next_retry_at)`; reset
  iff `now − anchor > LLM_OUTAGE_RESET_WINDOW_SECONDS`. A missing field falls
  back to `last_failed_at` — byte-for-byte today's behavior, so legacy
  in-flight outage state degrades gracefully.

`now − anchor` measures **time the job was free to run productively**, not time
parked in a wait *we* scheduled. Case analysis:

| Case | Behavior |
|---|---|
| Still cooling at re-dispatch (fresh `model_cooldown` 429) | idle ≈ 0 → **no reset**; `attempt` climbs, `first_failed_at` holds → 24h ceiling (or 60-attempt backstop) trips after ~5–12 windows → loud fail + operator alert. Never parks. |
| Provider under-reported `reset_s` (still cooling after the stated window) | the fresh 429 carries the *remaining* window → next pause is that remainder, not another full window. Self-adapting. |
| Same job: cooldown cleared, **days of productive work**, then a second cooldown (or any failure) | idle ≈ days ≫ 2h → **reset fires** → independent outage, pauses normally. (The rejected option's false-fail case.) |
| Transient outage (backoff ≤ 1h cap) | reset now requires >2h idle *beyond the scheduled backoff* — a narrow, deliberate semantics correction (below). |

**No separate cooldown counter, no new knob.** Once the spurious reset is gone,
the existing `attempt` + `first_failed_at` machinery bounds cooldowns: each wait
is floored at a full quota window (2–5h), so the 24h duration ceiling *is* the
"give up after a few windows" bound — for free.

### Effect on the shipped transient path (deliberate, narrow, fail-safer)

Anchoring subtracts the scheduled wait for *all* classifications, which shifts
the verified transient semantics in one band: a job that failed, waited its ≤1h
backoff, ran productively for **<2h**, then failed again is now a
*continuation* (no reset) where today it counts as fresh. That is the window's
*intended* meaning ("2h of runtime = ran fine") — today's code over-resets
because it cannot distinguish wait from runtime, which is why the sibling had
to inflate the window to 2h in the first place.

- **Direction of error is safe:** fewer resets → ceilings accumulate → an
  endpoint that keeps failing every <2h of runtime trips the 24h ceiling and
  alerts an operator instead of being re-read as endless fresh outages.
- The `reset_window > backoff cap` invariant becomes **redundant** (the anchor
  subtracts the wait exactly). Keep the 2h default and the invariant test as
  belt-and-suspenders; no config churn.
- The sibling's reset unit tests need updating to feed `next_retry_at` (or
  assert the legacy fallback); its k3d E2E conclusions stand (short backoffs
  never enter the changed band).

## Implementation map

1. **`src/graph.py` `cooldown` branch (`:2406-2454`)** — replace the unconditional
   fail-fast `error` return with the threshold above: within budget + Postgres →
   `llm_unavailable` freeze (`classification="cooldown"`, `retry_after_seconds =
   reset_s`); otherwise the existing fail-fast (unchanged message/audit). The freeze
   construction mirrors `:2526-2566` — factor the shared shape into a small helper
   if it reads cleaner.
2. **`src/graph.py`** — add `COOLDOWN_MAX_PAUSE_SECONDS` (env-tunable, default =
   `LLM_OUTAGE_CEILING_SECONDS`). Keep `_COOLDOWN_MIN_RESET_SECONDS=300` as the
   *lower* bound that still classifies a 429 as cooldown vs. per-minute rate-limit
   (`:280`, unchanged).
3. **`orchestrator/services/completion.py`** — `evaluate_llm_outage`: anchor the
   reset at `max(last_failed_at, prev next_retry_at)`, falling back to
   `last_failed_at` when the field is absent (§Design decision).
4. **`orchestrator/main.py` `/complete` + `orchestrator/database/postgres.py`** —
   compute the backoff before the attempt increment and persist `next_retry_at`
   into `context.llm_outage` in the same atomic write
   (`increment_job_llm_outage_attempt` grows an optional parameter).
5. **No change** to the outage sweeper, `list_due_llm_outage_jobs`,
   `claim_llm_outage_redispatch`, or dispatch — they key on `freeze_type`, not
   `classification`.

## Config surface

Reuses the sibling's `limits:` + env knobs (`llm_outage_backoff_*`,
`llm_outage_ceiling_seconds`, `llm_outage_max_attempts`,
`llm_outage_reset_window_seconds`, `LLM_OUTAGE_SWEEP_SECONDS`), plus:

| Key | Default | Meaning |
|---|---|---|
| `cooldown_max_pause_seconds` | `86400` (= outage ceiling) | Longest cooldown reset we'll pause through; longer → fail fast |

## Acceptance criteria

1. A worker/loop job hitting a `model_cooldown` 429 with `reset_seconds ≈ 2h`
   becomes **`paused` + `freeze_type=llm_unavailable`** (classification `cooldown`),
   agent freed / PVC retained — **not** `failed`.
2. `next_retry_at` is floored to the cooldown's `reset_seconds` (one ~2h sleep, not
   a per-minute retry); the sweeper re-dispatches at the window; the job **resumes
   from checkpoint** and continues.
3. A cooldown with `reset_seconds > cooldown_max_pause_seconds` (multi-day wall)
   still **fails fast** with the model + reset-window operator message — unchanged.
4. `quota_exhausted` (`insufficient_quota`) and `permanent` still fail fast.
5. A cooldown that **outlives** its stated reset (still cooling on re-dispatch) does
   **not** reset the duration ceiling; it fails **loudly with an operator alert** at
   24h / `MAX_ATTEMPTS`, never parking indefinitely.
6. With `CHECKPOINTER_BACKEND=sqlite`, cooldown handling **no-ops to today's
   fail-fast** (no unsafe cold-start re-dispatch) — same gate as the sibling.
7. A paused-for-cooldown loop iteration does **not** advance the loop or burn the
   failure budget (inherited from the sibling's terminal-gated advance).
8. Two cooldowns separated by a genuine productive stretch (idle beyond the prior
   scheduled wait > reset window) are **independent outages** — the second pauses
   normally, with no spurious instant ceiling-fail from stale `first_failed_at`.
9. Legacy outage state without `next_retry_at` evaluates exactly as today
   (anchor falls back to `last_failed_at`); in-flight pauses across the upgrade
   don't crash or misbehave.

## Verify plan

- **Unit** (extend `tests/test_graph_helpers.py` + `tests/test_llm_outage_resilience.py`):
  cooldown within budget → `llm_unavailable` freeze with `retry_after_seconds=reset_s`;
  cooldown over budget → fail-fast `error`; sqlite → fail-fast; the anchored-reset
  suite from §Design decision (still-cooling re-dispatch → no reset, ceiling
  accumulates; second cooldown after days of productive work → reset; transient
  re-fail inside the `(2h, backoff+2h)` band → no reset, asserted as the
  deliberate semantics change; missing `next_retry_at` → legacy behavior);
  `quota_exhausted` still fail-fast.
- **k3d E2E**: reuse the sibling's isolated-outage harness — pin a worker job to a
  model/endpoint that returns a synthetic `model_cooldown` 429 with a short
  `reset_seconds` (shrink `cooldown_max_pause_seconds` + `llm_outage_*` in a test
  overlay), confirm `paused` → sweeper re-dispatch at the floored delay →
  resume-from-checkpoint → completion once the stub stops cooling. Confirm the
  over-budget reset still fails fast.

## Relationship to prior work

- **Supersedes** the blanket cooldown fail-fast decided in
  `[[loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown]]` §Track-2/C1 —
  *only* for reset windows within the pause budget. The C2 consecutive-failure
  circuit breaker and the >budget fail-fast remain the backstop for the multi-day
  case that motivated C1. Update that issue doc's status when this lands.
- **Extends** `[[llm_outage_pause_and_backoff_redispatch]]`: this is effectively
  promoting `cooldown` from its v1 Non-goals list into the pause path, gated by the
  reset-window threshold. That doc's Tier-2 mechanism is the load-bearing part.
- **Hardens** the sibling's auto-reset for every classification: anchoring at
  `next_retry_at` removes the wait-vs-runtime ambiguity (the reason the reset
  window had to exceed the backoff cap) for any long scheduled wait, not just
  cooldowns.

## Open questions

- **Cutoff value.** Default `cooldown_max_pause_seconds = 24h` (reuse the ceiling)
  vs. a shorter dedicated bound (e.g. 12h) so a loop never sleeps more than half a
  day on one model. The user's real cases (codex ~2h, OpenAI ~5h) fit either.
- **Fallback model instead of pausing.** Out of scope here, but the strictly-better
  end state for an autonomous loop: degrade a cooldown-benched role onto a sibling
  model and keep moving, pausing only when *no* model is available. Worth its own
  design.
