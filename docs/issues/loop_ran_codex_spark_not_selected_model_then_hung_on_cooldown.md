# Loop job ran `gpt-5.3-codex-spark` instead of the selected `gpt-5.5`, then hung for 5+ hours on a model-cooldown 429

**Status:** Investigation complete, root causes isolated on the live main cluster. Three distinct defects, causally chained (A → B → C below). **P1 (Track 1 Layers 1+2+4-backend) implemented + locally verified 2026-06-27, uncommitted on `develop`** — see the Implementation status note under Sequencing. P2 (Layer 3 + Layer 4-frontend) and Track 2 (Defect C) pending.
**Found:** 2026-06-27. Job `8bf2be7e-7ed3-4c7d-9a8b-4cdad339d69b` ("Loop iter 2 · CRITIC"), project **Better Resavio** (`0b24067c-1345-4e97-8614-3a950b1ba892`), owner `knaeckebrothero` / `overlygenericaddress@pm.me`, main cluster.
**Severity:** High. The wrong model runs **silently** (the UI shows the model the user picked while a different one executes), burning a quota-limited Codex subscription; when that quota trips, the job does not fail or freeze — it **live-locks for the duration of the cooldown (~5.5 days)**, wedging the project self-improvement loop, holding the agent + workspace pods, growing the audit collection, and evading every stuck-detection mechanism.
**Component:** cockpit model picker (`agent-settings/model-group.component.ts`) · orchestrator dispatch (`orchestrator/main.py::_inject_dispatch_credentials`) · agent config resolution (`src/core/loader.py::LLMConfig.get_phase_config`) · agent graph retry (`src/graph.py::create_execute_node`) · project-loop materialization (`orchestrator/services/project_loops.py`)
**Related:** `[[agent_infinite_retry_on_permanent_llm_errors]]` (Defect C is the **explicitly-deferred "Fix 3"** from that resolved incident — same model, sibling failure mode) · memory topics `project_self_improvement_loop`, `project_loop_repo_compounding`, `reference_debug_session_usage_llm_routing`

---

## Symptom (user-reported, then confirmed)

The user created the "Better Resavio" self-improvement loop with **`gpt-5.5`** selected as the model. The Loop card confirms it: *Model: gpt-5.5*. Yet both loop jobs — Scholar `6e8bfcc5` (completed) and Critic `8bf2be7e` (processing) — ran **entirely on `gpt-5.3-codex-spark`**. The Critic job then sat in `processing` for 5+ hours making zero progress, with the operator seeing only a repeating 429:

```
Error code: 429 - {'error': {'code': 'model_cooldown',
  'message': 'All credentials for model gpt-5.3-codex-spark are cooling down via provider codex',
  'model': 'gpt-5.3-codex-spark', 'provider': 'codex',
  'reset_seconds': 482593, 'reset_time': '134h3m13s'}}
```

Two independent surprises, both confirmed real:
1. **Why codex-spark at all?** The user never knowingly configured a Codex model for this loop.
2. **Why stuck for hours** instead of failing or pausing?

## TL;DR — three causally-chained defects

| | Defect | Effect |
|---|---|---|
| **A** | The per-job/per-session **model picker silently persists** the strategic/tactical selection to **account-level** preferences on every change. | The user's account `default_strategic_model` / `default_tactical_model` became `gpt-5.3-codex-spark` without any deliberate "set an account default" action — just from a prior job/session create where codex-spark was selected (or prefilled and left). |
| **B** | An **explicit per-loop model does not win** over those account phase defaults at dispatch. | The loop set only the **top-level** `llm.model = gpt-5.5`; dispatch filled the empty `llm.strategic`/`llm.tactical` slots from the account defaults; the agent resolves model **per phase**, where a phase pin beats the top-level. → `gpt-5.5` is dead weight, both phases run codex-spark. |
| **C** | A 429 `model_cooldown` with a **multi-day** reset is treated as an ordinary transient rate-limit and **retried forever**. | codex-spark hit its weekly quota → ~5.5-day cooldown → the agent's outer graph loop re-enters `execute` every ~5 min indefinitely, with no circuit breaker and no freeze/fail signal to the orchestrator. |

A made the wrong model sticky; B made it override the explicit choice; C turned the resulting quota wall into an unbounded hang.

## Runtime evidence (the stuck job)

| Observation | Value | Source |
|---|---|---|
| Status / progress / elapsed | `processing` / **0.0%** / **310 min** | `get_job_progress` |
| Last *real* LLM work | iter **120**, `todo_complete`, **00:58:20Z** | `list_llm_requests` (121 total, all `gpt-5.3-codex-spark`) |
| Audit rows | **1085**, but last tool-call is ~`[792]` | `get_job` / `get_audit_trail` |
| Error rows | ~62, each `attempts: 4, recoverable: True` | `get_audit_trail filter=errors` |
| Live loop (repeats ~every 5 min) | `[1081] LLM → [1082] ERROR 429 → [1083] memory_retrieve → [1084] memory_inject → [1085] LLM …` | `get_audit_trail page=-1` |
| Cooldown reset (counting down) | `482593 → 482306 → 482013` s (~**5.5 days**) | error payloads |
| Agent | `90e7445b` **working**, heartbeat **fresh** (05:54:55Z) | `list_agents` |
| Stuck-detector verdict | **"No stuck jobs found"** (30-min threshold) | `get_stuck_jobs` |
| Processing jobs cluster-wide | **1** (this one); loop wedged behind it | `list_jobs status=processing` |

The model is confirmed end-to-end: request `7414` params show `model_name: gpt-5.3-codex-spark`, `temperature: 1.0`, `max_tokens: 16384`. The ~5-min cadence is the inner retry's 4 attempts × ~90 s default backoff (no `retry-after` header — the reset lives in the JSON body), after which the outer loop immediately re-enters.

---

## Defect A — a per-job control silently writes a **global account default**

The model picker persists every selection to account-level preferences, not just to the job being created:

```ts
// cockpit/.../agent-settings/model-group.component.ts
onStrategicModelChange(value) { ...; this.persistModel('strategic', resolved); this.change.emit(); }  // 324
onTacticalModelChange(value)  { ...; this.persistModel('tactical',  resolved); this.change.emit(); }  // 331

persistModel(key, value) {                                              // 393
  localStorage.setItem(STORAGE_KEYS[key], value)                        //   for next-time prefill
  this.settingsService.updatePreferences({ [`default_${key}_model`]: value }).subscribe();  // 405 → PATCH /api/settings/preferences
}
```

- Fires on **every change** (no save button), in the create flow — `AgentSettingsComponent` is mounted in **`job-create.component.ts:272`** and **`session-create.component.ts:126`**, not only the Settings page.
- `prefillFromConfig` (370-383) seeds the picker from `loadSavedModel` (localStorage) when the expert/config doesn't pin a model — so a previously-selected `codex-spark` is **re-shown and re-persisted** even if the user never touches it.
- `STORAGE_KEYS`→settings map (11-12): `strategic → default_strategic_model`, `tactical → default_tactical_model`.

Net: there is **no separate, deliberately-configured "account phase model" feature**. The per-job picker doubles as a global writer, so a single earlier job/session create silently set the account default that every later dispatch now reads. (The same `persistModel` path also backs `default_session_model` via `sessions-page.component.ts:729`.)

## Defect B — the explicit loop model is **shadowed** by the account phase defaults

Three facts compose into the shadowing:

1. **The loop writes only the top-level slot.** `create_loop_job` (`services/project_loops.py:233-235`):
   ```python
   model = loop.get("model")          # "gpt-5.5"
   if model:
       config_override["llm"] = {"model": model}   # top-level only; no strategic/tactical
   ```
   This rides through `resolve_config` as the winning `request_override` layer (`config_resolver.py:75-78,130-134`) — but it only ever sets `llm.model`.

2. **Dispatch fills the empty phase slots from the account defaults.** `_inject_dispatch_credentials` (`orchestrator/main.py:1704-1731`):
   ```python
   for _phase, _setting_key in (("strategic","default_strategic_model"),
                                ("tactical","default_tactical_model")):
       _phase_model = user_settings.get(_setting_key)        # gpt-5.3-codex-spark
       if _phase in llm_block and llm_block[_phase].get("model"):
           continue                                          # loop didn't set these → NOT skipped
       ...
       llm_block[_phase] = {"model": _phase_model, ...creds}  # strategic+tactical pinned to codex-spark
   ```
   (Note the asymmetry: the top-level `default_model` and the system chat default *are* guarded by `if "model" not in llm_override` — lines 1690-1693, 1819-1824 — so they correctly defer to the loop's `gpt-5.5`. Only the **phase** block has no such deference, because the loop never populates phase slots.)

3. **The agent resolves model per phase, and a phase pin beats the top-level.** `graph.py:1351` → `LLMConfig.get_phase_config` (`loader.py:1315-1321`):
   ```python
   override = getattr(self, phase, None)        # llm.strategic / llm.tactical
   if not override: return self
   return LLMConfig(model=override.model if override.model is not None else self.model, ...)
   ```

Effective delivered config:
```
llm.model           = gpt-5.5             ← what the Loop card shows; NEVER used for inference
llm.strategic.model = gpt-5.3-codex-spark ← every strategic phase
llm.tactical.model  = gpt-5.3-codex-spark ← every tactical phase
```

The same dispatch path + same owner settings explains why the Scholar sibling ran codex-spark too.

## Defect C — a long-cooldown 429 is retried forever (no circuit breaker)

`_classify_llm_error` maps **every** HTTP 429 → `rate_limit`, with no inspection of the `model_cooldown` code or the `reset_seconds`:

```python
# src/graph.py:287
if status_code == 429:
    return "rate_limit"
```

The retry loop then:
- inner: 4 attempts (`tool_retry_count=3`) with ~90 s backoff (`graph.py:2155-2186`);
- on exhaustion returns `{"error": {recoverable: True}}` **without `should_stop`** (`graph.py:2225-2232`) — only a `permanent` classification sets `should_stop` (`graph.py:2120-2153`);
- the outer graph loop re-enters `execute`: `route_after_execute` → `check_todos` → `route_after_check_todos` sees `should_stop=False, phase_complete=False` → **back to `execute`** (`graph.py:3013-3038`).

There is no counter for *consecutive no-progress LLM errors* (the empty-response / no-tool-call / `tool_use_failed` paths each have streak thresholds — the LLM-error path does not). With a ~5.5-day reset this spins until the cooldown clears (≈ 2026-07-02/03) **iff** the pod survives that long — realistically until manual cancel or a pod roll.

This is verbatim the deferred work in `[[agent_infinite_retry_on_permanent_llm_errors]]`:
> *"Fix 3 (iteration-level circuit breaker) — still worth doing as defence in depth for failure modes the classifier doesn't cover. Not urgent now that the most common cause is handled."*

## Why nothing caught it

- **Not `permanent`** → no `should_stop` → orchestrator never receives a freeze/fail signal → job stays `processing` (orchestrator is sole status authority).
- **`get_stuck_jobs` is blind**: each retry cycle writes audit rows → bumps `jobs.updated_at` → the staleness-based detector always sees a "fresh" job.
- **Heartbeat stays fresh** (5 s) → agent never marked offline → orphan auto-pause (3-min gap) never fires.
- **Graph fingerprint stuck-detector is blind**: it keys on `(tool_name, args_hash)`, but the LLM 429s *before* emitting any tool call — nothing to fingerprint.

## What was ruled out (so the next person doesn't re-chase)

- Bundled `scholar`/`critic` configs — both `$extends: defaults` → `gemma-4-31B`; `get_expert scholar` confirms `LLM: RedHatAI/gemma-4-31B-it-FP8-Dynamic`. Not the source.
- Project `default_config_override` — **`None`** for Better Resavio. Not the source.
- `_resolve_default_models` (`main.py:1050-1075`) — only sets **top-level** `llm.model` / `auxiliary.model`, never phase slots.
- `gpt-5.5` unroutable → fallback — **false**: `list_models` shows `gpt-5.5` ready on `codex-proxy`; dispatch's `unrouted_model_slots` would have hard-failed it otherwise.
- DB expert overlay with codex phase pins — the resolved scholar/critic is the gemma bundled config; no project/DB expert pins codex.

By elimination, the **only** writer of `llm.strategic`/`llm.tactical` pins in this path is the account-settings injection (Defect B), fed by the sticky persist (Defect A).

## Reproduce

1. As any user, open **New Job** (or **New Session**), pick `gpt-5.3-codex-spark` in the strategic/tactical model picker, submit (or just open the form when codex-spark is the last-saved selection). → `default_strategic_model` / `default_tactical_model` are now silently `codex-spark` in your account (`users.settings`; verify via `GET /api/settings/preferences`).
2. Create a project self-improvement loop, choose `gpt-5.5` as the loop model, start it.
3. Inspect a dispatched loop job's `resolved_config` (or the `llm_requests` model): every phase runs `codex-spark`, not `gpt-5.5`.
4. Let codex quota exhaust (or pin any model whose provider returns a long-reset `model_cooldown` 429): the job sits in `processing` at 0 % indefinitely; `get_stuck_jobs` reports nothing.

## Resolution design (decided)

Two independent tracks:

- **Track 1 — model selection & display refactor** (Defects A + B): a 4-layer change that **removes the conflicting concept** (per-phase account model defaults) rather than patching precedence, and makes the picker show the model that will actually run. **Decision: strip the per-phase account defaults (`default_strategic_model` / `default_tactical_model`); keep the single top-level `default_model`** — the one model preference that composes safely (top-level, cleanly overridden by experts/phase-pins/overrides) and lets a non-admin set a personal default without authoring a custom expert.
- **Track 2 — agent resilience** (Defect C): independent; the cooldown circuit breaker.

The earlier B1/B2 dispatch-precedence quickfix is **superseded** by Track 1 Layer 1 — stripping the per-phase defaults closes Defect B's harmful path at the source, so no precedence tweak is needed.

## Implementation plan — Track 1 (model selection & display)

Resolution order the design enforces, per slot (most-specific wins):

```
per-job/session override
  → expert (llm.{strategic,tactical}.model ?? llm.model)
    → project default_config_override
      → account default_model (top-level)
        → system registry default (resolve_default_for_capability)
```

There is **no per-phase account layer** after this work. `account_default` can still surface for a strategic/tactical slot, but only inherited from the single top-level `default_model`.

### Layer 1 — Strip per-phase account model defaults (backend) — *fixes Defects A-harm + B*
**Goal:** remove `default_strategic_model` / `default_tactical_model` from the dispatch chain and the settings surface so an explicit per-job/loop model is never shadowed.
**Changes:**
- `orchestrator/main.py:1704-1731` — delete the `for _phase, _setting_key in (("strategic","default_strategic_model"),("tactical","default_tactical_model"))` injection block. (Per-job explicit phase pins still flow via `config_override.llm.{strategic,tactical}` from the create form — that's the `request_override` layer, untouched.)
- `orchestrator/main.py:4928-4929` — remove the two fields from `UserSettingsUpdate`. PATCHes of the removed keys then become no-ops (Pydantic ignores unknown fields unless `extra="forbid"` — verify the model's config).
- Data hygiene: one-off migration/script to strip the now-dead keys from `users.settings` (incl. the `knaeckebrothero` row that triggered this). Inert if left unread, but remove for cleanliness.
**Acceptance:**
- A loop with `model=gpt-5.5` + bundled roles dispatches with `llm.strategic.model == llm.tactical.model == gpt-5.5` (top-level inherited; no phase pins) — verify against the dispatched job's `resolved_config`.
- A form-created job with an explicit strategic pick still carries that pin (request_override path intact).
- A user with the old keys still present in `users.settings` is unaffected (keys unread).
**Tests:** dispatch-resolution test asserts no phase pins are injected from user settings; loop-job resolution test asserts the top-level model propagates to both phases.

### Layer 2 — Make the picker's persist UI-only (frontend) — *fixes Defect A*
**Goal:** a per-job control must not mutate global account state.
**Changes:**
- `cockpit/.../agent-settings/model-group.component.ts:393-406` (`persistModel`) — drop the `settingsService.updatePreferences(...)` call; keep only the `localStorage` write (pure UI preselect). Once Layer 3 lands, the localStorage prefill is secondary to the server-resolved default and can be removed entirely (decide during Layer 3).
- `STORAGE_KEYS` (lines 10-14) — rename off the `default_*_model` names (e.g. `ui.lastModel.strategic`) so the UI-preselect key can never be confused with / collide with an account settings key.
**Acceptance:** selecting strategic/tactical/session models in New Job / New Session issues **no** `PATCH /api/settings/preferences`; account defaults change only from the Settings page.
**Tests:** model-group spec asserts `updatePreferences` is not called by `onStrategic/Tactical/SessionModelChange`.

### Layer 3 — Show the *effective* model from the backend (the real refactor) — *fixes "nothing is displayed"*
**Goal:** the picker's unset state names the model that will actually run, with provenance, computed by the **same** resolution dispatch uses (no client-side re-derivation → no drift).
**Changes:**
- Backend: extend the expert-detail response (the endpoint behind `expertDetail()`, which already returns `.config` / `.settings_matrix`) with an `effective_models` block — or add `GET /api/experts/{id}/effective-models?project_id=…`. It runs `resolve_config` with `base_defaults = _resolve_default_models(user)` + the dispatch phase-selection, returning per slot `{model, source}` where `source ∈ {override, expert, project, account_default, system_default}`. Context: `{expert_id|config_name, project_id, current user}`. Recompute on **expert/project change only** — the unset effective value doesn't depend on other slots, so no per-keystroke re-resolve.
- Frontend `model-group.component.ts` — replace `resolvedStrategicModel`/`resolvedTacticalModel`/`resolvedSessionModel` (config-only derivation, lines 255-267) with the server `effective_models` value; render the null/unset `<option>` as `Default → {model} ({source})` and keep the inherited-vs-overridden styling (existing `.modified` left-border). `getOverrides` (349-359) stays as-is — explicit picks still emit `llm.{strategic,tactical}.model` / `llm.model`.
- Wire-through: `job-create.component.ts:274` / `session-create.component.ts:126` pass `effective_models` (from `expertDetail()`) into `<app-agent-settings>` → `<app-model-group>` alongside `[config]`.
**Acceptance:** New Job with bundled Scholar shows strategic/tactical as e.g. `Default → gemma-4-31B (system default)` (or `… (account default)` when the user set a top-level `default_model`); picking a model flips the field to an override with a reset affordance; the untouched value equals what dispatch produces (cross-check `resolved_config`).
**Tests:** backend resolve unit tests across {bundled expert, custom expert w/ phase pin, account default set/unset, project override}; model-group spec asserts the unset label reflects the injected `effective_models`.

### Layer 4 — Registry-accurate resolved defaults + consistent Settings display — *fixes stale/blank defaults*
**Goal:** wherever the UI shows a "system/account default" model, it matches dispatch.
**Changes:**
- `orchestrator/main.py:21035-21087` (`_resolve_preference_defaults`) — source `default_model` / `default_auxiliary_model` from `postgres_db.resolve_default_for_capability("chat"/"auxiliary")` (the DB registry dispatch uses), not the `defaults.yaml` placeholder; make it async (or fetch the registry defaults in `get_user_preferences`, 21090-21102).
- `cockpit settings.component.ts` — render the Preferences "Default Model" / "Auxiliary" dropdowns with the resolved default as placeholder/preselect (mirror the Persistent Agent "Model" field, which already shows `Default: …`). No strategic/tactical fields on any settings surface (Layer 1 removed the concept).
**Acceptance:** Settings → Preferences shows the real chat/aux defaults (not blank, not the YAML placeholder unless that genuinely is the registry default); `GET /api/settings/preferences._resolved.default_model == resolve_default_for_capability("chat")`.
**Tests:** `_resolve_preference_defaults` unit test asserts it reads the registry; settings spec asserts the dropdown shows the resolved value when unset.

### Sequencing
- **P1 (correctness, mostly deletion):** Layer 1 + Layer 2 + Layer 4-backend. Kills Defects A & B, stops the silent drift, makes the loop honor its model. Low risk. Would have prevented this incident.
- **P2 (UX refactor):** Layer 3 + Layer 4-frontend. The display work that makes the dropdowns meaningful.

**Implementation status (2026-06-27, uncommitted on `develop`):**
- ✅ **Layer 1** — removed the per-phase account-default injection (`main.py` `_inject_dispatch_credentials`), the `UserSettingsUpdate` fields, the `UserSettings` TS type fields; migration `0039_drop_per_phase_account_model_defaults.sql` strips the dead keys. Regression test `tests/test_dispatch_phase_credentials.py::TestPerPhaseAccountDefaultsRemoved`.
- ✅ **Layer 2** — `model-group.component.ts` `persistModel` is localStorage-only (dropped `updatePreferences`; `STORAGE_KEYS` renamed to `ui.lastModel.*`; removed the unused `SettingsService`); same fix applied to `sessions-page.component.ts` `persistSessionModel`. Spec: model-group "persist (UI-only, no account write)".
- ✅ **Layer 4-backend** — `_resolve_preference_defaults` now async and sources chat/aux/session defaults from `resolve_default_for_capability` (registry), not the YAML placeholder.
- Verified locally: `ruff check`/`format` clean; `pytest` dispatch + resolver + resolution-adjacent suites (26 + 16 + 223) pass; cockpit `vitest` model-group (16) + sessions-page (29) pass; `tsc --noEmit` clean. Not yet committed/pushed.
- Verified on **k3d** (`srw`): migration `0039` applied cleanly on the live Postgres; code live in the orchestrator pod; orchestrator boots healthy (async `_resolve_preference_defaults` didn't break startup). **Behavioral E2E (Layer 1):** seeded the test user's account with `default_strategic_model`/`default_tactical_model = gemma-4-moe` (the incident state), then ran the live `_inject_dispatch_credentials` for a job pinned to top-level `gpt-5.4-mini` → result kept `model=gpt-5.4-mini` with **no `strategic`/`tactical` pins** (pre-fix they'd have been `gemma-4-moe`). **Layer 4:** `_resolve_preference_defaults()` returned the registry default (`gemma-4-moe`), not the YAML placeholder. Seed data cleaned up afterward. **Layer 2:** confirmed the running cockpit pod's source (what `ng serve` ships) carries the change — `model-group.component.ts` has 3 `ui.lastModel` keys, **0** `updatePreferences` calls, **0** `settings.service` imports; `sessions-page.component.ts` has 0 `updatePreferences`. So a model pick structurally cannot fire a settings PATCH in the served bundle (the call path is gone), corroborating the `vitest`/`tsc` coverage. A literal in-browser click+network capture was gated by login (password entry declined) and is the only belt-and-suspenders step left.
- **Still open:** P2 (Layer 3 effective-model display + Layer 4-frontend Settings surfacing) and Track 2 (Defect C circuit breaker).

## Track 2 — Defect C (agent resilience, independent)

- **C1:** cooldown-aware 429 classification (`src/graph.py:287`) — a long `reset_seconds` / `model_cooldown` body → **freeze for resume** (`freeze_data` `budget_exceeded`/`blocking_message`, `should_stop=True`), not tight-retry; surface the reset time to the operator.
- **C2:** the long-deferred **iteration-level circuit breaker** (`[[agent_infinite_retry_on_permanent_llm_errors]]` Fix 3): N consecutive `execute` returns with no tool output / no todo progress → hard-freeze. Catches every "retriable but never-recovering" class, not just cooldowns.
- **C3:** stop bumping `jobs.updated_at` on pure retry-exhaustion (or add `effective_progress_at`) so `get_stuck_jobs` can see these live-locks.

Independent of Track 1; lands regardless, since any quota-limited model can trip it.

## Immediate remediation (operational)

- **This job:** cancel `8bf2be7e` — it will not self-heal for ~5.5 days, and pausing only invites re-dispatch onto the same cooldown. Then re-kick the loop after fixing the model.
- **This account:** in **Settings**, reset strategic + tactical models to "Server default" (PATCHes them to `null`), or set them to `gpt-5.5`. Until Track 1 Layer 1 lands, clearing the sticky account phase default is the only lever that makes the loop's own model selection take effect.

## Open questions

- Should `recoverable=False` / cooldown freezes propagate a structured reason to the cockpit (e.g. "Model X cooling down until T") rather than a generic `processing`?
- Codex routing: `gpt-5.3-codex-spark` reaches the codex-proxy/CLIProxyAPI, which currently **bypasses** the LiteLLM gateway's RPM/quota controls (see `reference_debug_session_usage_llm_routing`), so nothing throttled the loop before it hit the hard provider cooldown. Should loop roles route through the gateway, or carry a fallback model?
- Should an autonomous multi-iteration loop ever be allowed to pin a single quota-limited subscription model with no fallback?

## Code references

| File | Lines | Role |
|---|---|---|
| `cockpit/.../agent-settings/model-group.component.ts` | 324-336 | `onStrategic/TacticalModelChange` → `persistModel` on every change (Defect A) |
| ″ | 393-406 | `persistModel` → localStorage **+** `updatePreferences` (account PATCH) |
| ″ | 370-383 | `prefillFromConfig` → `loadSavedModel` (re-shows last selection) |
| ″ | 345-360 | `getOverrides`: job mode → phase pins; session mode → top-level |
| `cockpit/.../create/job-create.component.ts` | 272, 274 | mounts `<app-agent-settings>`; feeds `[config]="expertDetail()?.config …"` (config-only, misses base_defaults — Layer 3) |
| `cockpit/.../session-create/session-create.component.ts` | 126 | mounts `<app-agent-settings>` in New Session |
| `cockpit/.../agent-settings/agent-settings.component.ts` | 283-288, 312-313 | `getOverrides` aggregation + `prefillFromConfig` cascade to sub-groups |
| `cockpit/.../agent-settings/model-group.component.ts` | 255-267 | `resolvedXModel` config-only derivation — the incomplete "default" (Layer 3 replaces) |
| `orchestrator/main.py` | 21035-21087 | `_resolve_preference_defaults` reads YAML placeholder, not the registry (Layer 4) |
| `orchestrator/main.py` | 1704-1731 | injects account `default_strategic/tactical_model` into empty phase slots (Defect B) |
| ″ | 1690-1693, 1819-1824 | top-level / system-chat defaults — correctly guarded by `"model" not in` |
| ″ | 1050-1075 | `_resolve_default_models` — top-level only |
| ″ | 21105-21119, 4916-4941 | `PATCH /api/settings/preferences` + `UserSettingsUpdate` |
| `orchestrator/services/project_loops.py` | 233-235 | loop sets only top-level `llm.model` (Defect B) |
| `orchestrator/services/config_resolver.py` | 75-78, 130-134 | layer order; `request_override` merged last (top-level only) |
| `src/core/loader.py` | 1315-1321 | `get_phase_config` — phase pin beats top-level model (Defect B) |
| `src/graph.py` | 287-288 | every 429 → `rate_limit` (Defect C) |
| ″ | 2155-2186, 2225-2232 | inner retry; exhaustion returns `recoverable:True`, no `should_stop` |
| ″ | 3013-3038 | `route_after_execute` / `route_after_check_todos` loop back to `execute` |
| `docs/done/agent_infinite_retry_on_permanent_llm_errors.md` | Fix 3 | the deferred iteration-level circuit breaker (Defect C) |
