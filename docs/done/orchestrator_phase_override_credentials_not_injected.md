---
tags:
  - orchestrator
  - bug
  - llm-routing
  - dispatch
  - credentials
  - resolved
related:
  - "[[agent_infinite_retry_on_permanent_llm_errors]]"
  - "[[agent_audit_collection_missing_indexes]]"
  - "[[orchestrator_mongodb_cascading_failure_resilience]]"
  - "[[llm_routing_issues]]"
  - "[[hardcoded_model_defaults]]"
  - "[[api_key_resolution]]"
---

# Phase-Level Model Overrides Skip Credential Injection

**Reported**: 2026-05-12
**Status**: **Resolved in `8b7a0bd`** (2026-05-12). Verified in
production on 2026-05-12 against job
`d7295039-c9d5-4e29-b3ae-640d47c4b981` (Scholar preset, identical
config shape to the original failing job): `resolved_config.agent.llm.
tactical.base_url` and `.strategic.base_url` were correctly written to
`http://srw-codex-proxy:8317/v1`, and the agent's outbound HTTP traffic
went to the codex-proxy with `HTTP/1.1 200 OK` on every call. See the
[Resolution](#resolution) section below for full verification.

Original report kept below for historical context — the analysis is
how we got to the fix.

## Summary

When a job's `config_override` pins a model only at phase level
(`llm.tactical.model` / `llm.strategic.model`) without also setting the
top-level `llm.model`, the orchestrator's dispatch-time credential
injection never resolves the phase model against the model registry.
The agent ends up with the right model **name** but the parent's
`base_url` — typically the user's `Local Router` (`https://ai.h4ll.app/v1`).
If the phase model isn't hosted at that URL, every LLM call returns
`404 Model '<id>' not found` and the job loops indefinitely producing
zero steps.

The Scholar preset is the canonical reproducer because its
`config_override` matches this exact shape.

## Observed Behavior

### Incident — 2026-05-12, prod cluster

A user-created Scholar-preset job (`418d6f58-...`) ran for ~9 minutes
with **0 completed steps** before being cancelled. Agent logs
(`srw-agent-j-97cec126`, container `agent`) show:

```
2026-05-12 10:49:16  httpx INFO  HTTP Request: POST
  https://ai.h4ll.app/v1/chat/completions "HTTP/1.1 404 Not Found"
2026-05-12 10:49:16  src.graph ERROR  [418d6f58-...] LLM error after 4
  attempts: Error code: 404 - {'error': {'message': "Model
  'gpt-5.3-codex-spark' not found", 'type': 'invalid_request_error'}}
2026-05-12 10:49:16  src.api.dual_app INFO  [Iteration 57] job=418d6f58-...
2026-05-12 10:49:16  src.managers.todo INFO  Todo state: total=5,
  completed=0, in_progress=0, pending=5
```

Iterations 56–60+ all show the identical pattern: 3 retries → "after 4
attempts" → next iteration → repeat. Todo state never changes because
no LLM call ever succeeds.

The endpoint `https://ai.h4ll.app/v1` exposes `gemma-4-*`, `qwen3-*`,
`whisper-*`, `kokoro`, `gpt-4o*` (aliases for gemma) — **no
`gpt-5.3-codex-spark`**. That model is registered in the DB and lives
behind `srw-codex-proxy` (`http://srw-codex-proxy:8317/v1`):

```
 srw=# SELECT m.model_id, e.label, e.base_url
       FROM models m JOIN llm_endpoints e ON m.provider_ref::uuid = e.id
       WHERE m.model_id = 'gpt-5.3-codex-spark';
       model_id       |    label    |            base_url
 ---------------------+-------------+--------------------------------
  gpt-5.3-codex-spark | codex-proxy | http://srw-codex-proxy:8317/v1
```

So the routing data is correct in the registry. The dispatch path is
not consulting it.

## Expected Behavior

When `config_override.llm.tactical.model` or
`config_override.llm.strategic.model` names a model that lives in the
registry under a non-default endpoint, the orchestrator should inject
that endpoint's `base_url` + `api_key` into the same section before
the override is shipped to the agent — exactly the way it already does
for the top-level `llm.model` and for user-default phase pins.

## Root Cause

`_inject_dispatch_credentials` in `orchestrator/main.py` looks **only at
the top-level `llm.model`** when deciding whether to call the model
registry:

```python
# orchestrator/main.py:748-756
config_override = config_override or {}
llm_over = config_override.setdefault("llm", {})
model_id = llm_over.get("model")           # ← only top-level
meta = None
if model_id:
    try:
        meta = await _resolve_model(model_id, user_id=user_id_str)
    except UnknownModelError:
        meta = None
```

The phase-override path further down (`orchestrator/main.py:826-852`)
**does** inject credentials for tactical/strategic, but only when the
phase model is sourced from `user_settings.default_strategic_model` /
`default_tactical_model`, and only when the phase block does **not**
already have a `model`:

```python
# orchestrator/main.py:826-852
for _phase, _setting_key in (
    ("strategic", "default_strategic_model"),
    ("tactical", "default_tactical_model"),
):
    _phase_model = user_settings.get(_setting_key)
    if not _phase_model:
        continue
    llm_block = config_override.setdefault("llm", {})
    if _phase in llm_block and llm_block[_phase].get("model"):
        continue                            # ← skips if job already pinned a phase model
    ...
```

The combined effect: a job whose `config_override` is exactly

```json
{
  "llm": {
    "tactical":  {"model": "gpt-5.3-codex-spark"},
    "strategic": {"model": "gpt-5.3-codex-spark"}
  }
}
```

walks through `_inject_dispatch_credentials` with `model_id = None`
(top-level branch skipped) and with both phase branches short-circuited
by the `already has a model` guard. No `base_url` / `api_key` is ever
written into the phase sections.

Confirmed against the failing job's `resolved_config.agent.llm`:

```
"model": "gemma-4-moe",
"base_url": "https://ai.h4ll.app/v1",   ← parent (default chat)
...
"tactical": {
  "model": "gpt-5.3-codex-spark",
  "base_url": null,                     ← never injected
  "provider": null
},
"strategic": {
  "model": "gpt-5.3-codex-spark",
  "base_url": null,
  "provider": null
}
```

The agent's LLM factory then falls back to the parent's `base_url`
(`https://ai.h4ll.app/v1`) but with the override's model name —
producing the 404 loop.

### Why the Scholar preset triggers this

The Scholar config (`config/experts/scholar/config.yaml:18`) declares
`llm: {}` and inherits the rest from `defaults.yaml`. The cockpit's
agent-settings panel writes phase pins (`llm.tactical.model`,
`llm.strategic.model`) into `config_override` without populating
top-level `llm.model`. Any other preset built the same way will hit the
same bug; Scholar is just the one users reach for "research with a
specific reasoning model."

## Code References

| File | Lines | Role |
|---|---|---|
| `orchestrator/main.py` | 717-794 | `_inject_dispatch_credentials` — top-level model resolution only |
| `orchestrator/main.py` | 750 | `model_id = llm_over.get("model")` — the missed lookup |
| `orchestrator/main.py` | 758-772 | Endpoint row lookup + `setdefault("base_url" / "api_key")` — only runs for top-level |
| `orchestrator/main.py` | 826-852 | Phase-pin injection — gated on user-default settings, not job overrides |
| `orchestrator/main.py` | 1710 | `_inject_model_credentials` — helper that would do the right thing if called |
| `src/core/model_registry.py` | 99-203 | `resolve_endpoint_route` — returns `base_url` + `api_key` from `user_llm_endpoints` |
| `src/core/loader.py` | 825-880 | Override-merge: phase `base_url=None` falls back to parent |

## Reproduction

1. In the cockpit, pick the Scholar preset (or any preset) and set
   "Strategic model" and "Tactical model" to a registry entry whose
   endpoint is **not** the user's default chat endpoint
   (e.g. `gpt-5.3-codex-spark`, which is registered against
   `srw-codex-proxy`).
2. Submit a job. Expected: agent routes phase calls to `srw-codex-proxy`.
   Actual: agent calls the user's default chat endpoint and gets 404.
3. Verify in psql:
   ```sql
   SELECT jsonb_path_query(resolved_config, '$.agent.llm.tactical')
   FROM jobs WHERE id = '<job-uuid>';
   ```
   `base_url` will be `null` on a job that exhibits the bug.
4. Inspect agent logs: `httpx ... POST https://<parent base_url>/v1/chat/completions
   "HTTP/1.1 404 Not Found"` repeating.

## Resolution

Fixed in commit `8b7a0bd` ("Add regression tests for
`_inject_dispatch_credentials` phase-override handling", 2026-05-12).
The commit modifies `orchestrator/main.py` to resolve credentials for
phase-level overrides (`llm.tactical.model`, `llm.strategic.model`)
the same way the top-level path already does, and adds
`tests/test_dispatch_phase_credentials.py` covering the failure shape
from this incident plus surrounding edge cases (pre-existing
`base_url`, unknown models, auxiliary parity).

### Production verification (2026-05-12, post-deploy)

Reproduced the original failure shape against the new orchestrator
image (`sha-8e92c81`):

| Aspect | Pre-fix | Post-fix |
|---|---|---|
| `config_override` shape | `llm.tactical.model=gpt-5.3-codex-spark`, no `base_url` | identical |
| `resolved_config.agent.llm.tactical.base_url` | `null` | `http://srw-codex-proxy:8317/v1` |
| `resolved_config.agent.llm.strategic.base_url` | `null` | `http://srw-codex-proxy:8317/v1` |
| Agent outbound HTTP | `POST https://ai.h4ll.app/v1/...` → `404` | `POST http://srw-codex-proxy:8317/v1/...` → `200` |
| Job behaviour | infinite iteration loop, 0 todos completed | normal progression with multiple tool calls per response |

Verified against scholar job
`d7295039-c9d5-4e29-b3ae-640d47c4b981` (parent
`74bf5d46-2946-40e3-9278-78c369d3e727`), Scholar preset, identical
phase-pin config to the original failing job.

### Minor follow-up (not blocking)

When the phase override names a model that doesn't exist in the
registry (e.g. typo'd / removed), the orchestrator logs `Dispatch:
injected credentials for <phase> override: <model>` even though the
helper falls through and only the parent default's `base_url` is
inherited. Cosmetic — the agent then surfaces a clear 404 and the new
classifier ([[agent_infinite_retry_on_permanent_llm_errors]]) fails
the job fast. Worth tightening the log to "could not resolve, falling
back" the next time this code is touched.

## Proposed Fixes

### Fix 1 — Resolve credentials for phase overrides in `_inject_dispatch_credentials` (Required)

After the top-level resolution block (`main.py:748-794`), iterate the
existing phase overrides and inject credentials for each model that
isn't already accompanied by a `base_url`:

```python
for _phase in ("strategic", "tactical"):
    phase_block = llm_over.get(_phase)
    if not isinstance(phase_block, dict):
        continue
    phase_model = phase_block.get("model")
    if not phase_model or phase_block.get("base_url"):
        continue
    await _inject_model_credentials(
        section=phase_block,
        model_id=phase_model,
        user_id=user_id_str,
        resolved_keys=resolved_keys,
    )
```

`_inject_model_credentials` (`main.py:1710`) already does the registry
lookup + `setdefault` pattern correctly; this just wires it up for the
explicit-override path.

### Fix 2 — Make the auxiliary path benefit from the same helper (Defensive)

`auxiliary.model` already calls `_inject_model_credentials`
(`main.py:805-810`). Mirror that exact treatment for phase overrides so
future capability blocks (e.g. a hypothetical `critic`) don't repeat
the bug.

### Fix 3 — Log a loud warning when a phase model resolves to no endpoint (Defensive)

If `_inject_model_credentials` returns without setting either `api_key`
or `base_url` for a phase override, surface a warning at dispatch time
naming the job + phase + model — same pattern as the existing
"skipping {phase} phase pin … no credentials resolvable" warning at
`main.py:843-848`. Catches future registry/endpoint deletions before
they manifest as opaque 404 loops in the agent.

### Fix 4 — Add a regression test (Required)

A unit test exercising `_inject_dispatch_credentials` with the exact
`config_override` shape from the incident (phase-only overrides, no
top-level model) and asserting `base_url` + `api_key` land in the
phase blocks.

## Priority

1. **Fix 1** — eliminates the bug class entirely. Quick (~30 LOC).
2. **Fix 4** — paired with Fix 1; locks the contract.
3. **Fix 3** — improves diagnosability of future routing failures.
4. **Fix 2** — small refactor, can ride along.

## Open Questions

- Should the cockpit also write `base_url` into the phase block at job
  creation time, as a belt-and-suspenders? The dispatch path is
  authoritative (it has the user's resolved keys), but the cockpit
  knows the endpoint label the user selected.
- Are there other consumers of `config_override.llm.*` that read the
  child blocks before dispatch credential injection runs? `creation_order`
  + the resume path (`main.py:1194-1201` re-runs `_inject_dispatch_credentials`)
  look fine, but worth a grep for `config_override["llm"]["tactical"]` /
  `["strategic"]` access on the orchestrator side.
- This bug surfaced because of [[agent_infinite_retry_on_permanent_llm_errors]]
  amplifying it into a cluster outage via
  [[agent_audit_collection_missing_indexes]] and
  [[orchestrator_mongodb_cascading_failure_resilience]]. Fix 1 stops
  the trigger; the related issues prevent the *next* misconfiguration
  from cascading the same way.
