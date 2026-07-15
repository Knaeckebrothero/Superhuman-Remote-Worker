# LLM fallback-model routing: keep working on a sibling model instead of pausing

Status: **PROPOSED / DRAFT — not scheduled.** Design sketch only; forks below are
open decisions, not settled.
Date: 2026-07-15
Scope: worker/loop jobs (the LangGraph worker). Layers *above*
`[[llm_cooldown_pause_and_resume]]` — fallback becomes the first response to a
model being unavailable; the cooldown/outage pause path is the last resort when
**no** candidate model is available.
Provenance: spun out of `[[llm_cooldown_pause_and_resume]]` (the 2026-07-07
`gpt-5.5` cooldown incident) as its deferred "strictly-better end state." The
underlying question is old — `[[loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown]]`
§Open-questions already asked *"should an autonomous multi-iteration loop ever be
allowed to pin a single quota-limited subscription model with no fallback?"*

## Problem

When a role's pinned model hits a quota cooldown (or any temporary
unavailability), the best we do today — after `[[llm_cooldown_pause_and_resume]]`
— is **pause and wait out the window** (2–5h). For an autonomous
self-improvement loop, that's hours of zero progress waiting for the *preferred*
model when a perfectly usable sibling (`gpt-5.4-mini`, `MiniMax-M3`, `gemma-4-*`)
is sitting idle and ready.

The strictly-better behavior: **degrade, don't stall.** Route the role onto the
next available model in a fallback chain and keep iterating; pause only when the
whole chain is cold.

The hard constraint learned from history: it must be **loud**.
`[[loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown]]` classified a
*silent* wrong-model substitution as the original bug — the UI showed `gpt-5.5`
while `gpt-5.3-codex-spark` actually ran, burning a quota-limited subscription
invisibly. Auto-fallback that isn't surfaced recreates exactly that failure, and
for a compounding loop it's worse: later iterations silently build on weaker-model
output.

## Goals

- On a `cooldown` (and optionally other transient-unavailability classes), the
  dispatcher/agent substitutes the next **available** model from a fallback chain
  and the job keeps running.
- Pause (via `[[llm_cooldown_pause_and_resume]]`) only when **every** model in the
  chain is unavailable.
- **Every substitution is loud** — an audit event and a UI badge naming the model
  that ran, the model it fell back from, and why (`gpt-5.5 cooling, resets ~2h`).
- **Opt-in, not silent-by-default** — a loop/role must elect fallback; the default
  stays "run the pinned model or pause," so nobody's quality-sensitive loop
  silently downshifts.

## Non-goals

- **Not a replacement** for the pause path — pause remains the last resort.
- **Not automatic model _upgrade_** — fallback only moves *down* a declared chain.
- **`quota_exhausted`** (billing/spend-cap) and `permanent` still fail fast — a
  fallback doesn't fix out-of-money or a bad key (though a chain *could* let a
  different provider's key carry on; see open questions).

## Key design forks (decide when scheduled)

### Fork A — where the swap happens

- **Dispatch-time substitution (cheaper, partial).** The orchestrator, when
  dispatching a job, checks whether the pinned model is known-cold and substitutes
  a fallback before the agent starts. Helps the *next* job/iteration, **not** the
  one already mid-run when the 429 fires. Needs the orchestrator to *know* a model
  is cold up front (Fork C).
- **Mid-run swap (complete, invasive).** The agent catches the cooldown 429 and
  re-issues against the fallback within the same run. Zero lost iterations, but
  touches per-phase config resolution, **per-provider credential/`base_url`
  injection** (the codex-proxy vs. direct hazard from
  `[[codex_proxy_transient_401_fails_job]]` / `[[srw_codex_session_gateway_baseurl_401]]`),
  and cross-family settings-matrix differences (`[[srw_session_switch_stale_topk]]`).

**Leaning:** dispatch-time-first for v1 (skips the invasive mid-run credential
plumbing); mid-run as a later increment. For a loop, "next iteration runs the
fallback" recovers almost all the value — only the single in-flight iteration
pauses.

### Fork B — where the fallback chain is declared

Options: per-role (expert config), per-loop, per-account, or **capability-based**
off the model registry (`resolve_default_for_capability("chat"/"strategic"/…)`
already exists and dispatch already uses it). Capability-based gives a sane
default chain with zero per-role config ("next ready strategic-tier model");
expert/loop overrides layer on top for the quality-sensitive cases.

**Leaning:** capability-based default chain, expert-overridable.

### Fork C — how "is this model cold?" is known at dispatch time

Dispatch-time substitution needs cooldown state *before* the call. Today cooldown
is discovered mid-run via the 429; the `KeyRing` circuit-breaker
(`src/llm/key_ring.py`) holds per-key cooldown, but it's **in the agent process**,
not visible to the orchestrator. Cleanest bridge: the cooldown pause path
(`[[llm_cooldown_pause_and_resume]]`) already records the model + `reset_seconds`
in `freeze_data`/`context.llm_outage` when a job freezes. Promote that into a
small **shared "model cooling until T" registry** the dispatcher reads. So the
*first* job to hit a cooldown pauses (or fals back mid-run), and its recorded
cooldown informs every subsequent dispatch to substitute until T.

### Fork D — visibility surface (non-negotiable, only the form is open)

Audit event on every substitution + a UI badge on the job/loop card. Minimum:
`ran <model> · fell back from <pinned> · <reason, reset ~Nh>`. Reuse the existing
audit step + freeze-notification plumbing.

### Fork E — quality policy

Opt-in per loop/expert (a `fallback_models` / `allow_fallback` field), default
**off**. A compounding loop building on a downshifted model is a real quality
risk, so trust is earned, not assumed.

## Layering with the pause path

```
role model cold?
  ├─ fallback enabled + a ready sibling exists → run the sibling (loud), keep going
  └─ no candidate available                    → pause via [[llm_cooldown_pause_and_resume]]
```

The pause substrate is what fallback falls back *to*; building the pause path
first (as planned) is a prerequisite, not throwaway work.

## Rough scope

Bigger than the pause fix: touches dispatch (substitution + the cooling
registry), config resolution (chain), the model registry, credential injection
(mid-run variant), and the UI (visibility). Best sequenced **after**
`[[llm_cooldown_pause_and_resume]]` ships and is trusted.

## Open questions

- **Cross-provider fallback for billing walls?** A `quota_exhausted` on provider
  A's key *could* fall over to provider B rather than fail fast — but that spends
  real money on a different account without a human in the loop. Probably still
  operator-gated; flag it.
- **All unavailability classes, or cooldown-only?** Should `transient`/`5xx`
  outages also prefer a fallback over the outage pause, or is fallback reserved for
  quota cooldowns (where the wait is long and the cause is a specific model, not
  the whole endpoint)?
- **Chain ordering & cost/quality tiers** — does the chain respect a
  cost/capability ordering, and is that per-role or global?
- **Interaction with usage accounting** — a fallback shifts spend/tokens onto a
  different model; the usage ledger (`[[usage_ledger_recording_granularity]]`)
  should attribute it correctly and the substitution should be visible in the
  usage dashboard.
