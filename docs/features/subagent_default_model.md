---
tags:
  - feature
  - delegation
  - subagents
  - spawn_subagent
  - model-defaults
  - cost-control
related:
  - "[[delegation_light_mode_missing]]"
  - "[[subagents_never_used]]"
  - "[[loop_subagent_forensics]]"
  - "[[project_self_improvement_loop]]"
  - "[[loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown]]"
aliases:
  - subagent default model
  - cluster-wide subagent reader default
  - subagent kind in Defaults tab
  - delegation reader default model
---

# Cluster-wide default model for the `subagent` (delegation reader) tier

> Design doc — 2026-07-03, from the "add a subagent default to the Admin → LLM
> Configuration → Defaults tab" conversation. Deferred to a doc rather than built
> inline because — unlike the per-expert/per-job picker (purely additive UI) —
> this touches the **dispatch model-resolution + credential path for every job**.

**Status:** DESIGN — not built. Not urgent (usage-cap headroom is fine per the
2026-07-03 framing); this closes a *completeness* gap in the subagent model
controls, not a live cost problem.

**Prerequisite / pairs with:** the per-expert/per-job **Subagent model picker**
(built 2026-07-03, uncommitted on `develop` — `model-group.component.ts` +
`expert-editor.component.ts` + the `subagent` slot in `_effective_models_from_layers`;
see [[delegation_light_mode_missing]] and the memory topic
`project_spawn_subagent_light_delegation`). That picker sets *pins*; this doc adds
the *floor* below them. Together = the full control surface for the reader model.

---

## 1. Goal

Add a **"Subagent (delegation reader)"** row to the Admin → LLM Configuration →
**Defaults** tab (the "DEFAULT MODELS" panel), so an operator can set the
`spawn_subagent` reader model **cluster-wide, once**, without editing every
expert. This is the natural home for the "route readers to a cheaper tier" cost
lever from [[loop_subagent_forensics]] T1.1 — a MiniMax parent can default its
readers to a cheaper model fleet-wide.

Today the tier exists (`llm.subagent`, built in spawn_subagent Phase 0) and is
now settable per-expert/per-job (the picker), but there is **no cluster default**:
`VALID_DEFAULT_MODEL_KINDS` stops at the 8 kinds `chat / browser / citation /
embedding / vision / auxiliary / whisper / tts`.

## 2. The load-bearing design decision — precedence (this is a *floor*)

The existing per-kind defaults (aux/vision/whisper/tts/embedding) inject straight
into `config_override` at dispatch — the **top** of the precedence stack. **We must
NOT mirror that**, because the per-expert/per-job Subagent picker writes the
expert pin (expert-config layer) and the job pin (`config_override`), and a
cluster default injected into `config_override` would **clobber the picker**.

The correct order is *specific beats general*:

```
job pin  >  expert pin  >  cluster subagent default  >  tactical fallback  >  base
```

The clean place to slot a floor is the **base-defaults layer**, produced by
`_resolve_default_models(user_id)` (`orchestrator/main.py:1057`). Its docstring:
*"a config layer that `resolve_config` applies above the base config's placeholder
model and below the expert."* That is exactly how the cluster **chat** default
already works (`out["llm"]["model"] = chat`). So:

- Add `out.setdefault("llm", {}).setdefault("subagent", {})["model"] = subagent`
  (when the cluster `subagent` default is set) to `_resolve_default_models`.
- `resolve_config` merges expert config **on top**, so an expert `llm.subagent`
  pin overrides the cluster default ✓; the request override (`config_override`,
  the per-job picker) merges on top of that ✓.
- The agent's `_resolve_subagent_config`
  (`src/tools/delegation/spawn_subagent.py:113`) reads the *merged*
  `llm.subagent.model` → it sees job-pin → else expert-pin → else cluster-default;
  and if none of those are set (cluster default unset too), `llm.subagent` is
  absent and it still falls through `tactical → base` unchanged. **No agent-side
  change needed.**

## 3. The other load-bearing piece — credentials

`_resolve_default_models` sets model **names only** ("no transport" — its
docstring: *"base_url/api_key … are injected into the delivery blob, not here"*).
So the subagent model's endpoint `base_url` + `api_key` must be injected into the
delivery blob the same way the **chat** model (`llm.model`) is. A name with no
endpoint 404s.

**TODO for the build:** locate where the resolved config's `llm.model` (chat) gets
its blob credentials injected and extend it to cover `llm.subagent`. The dispatch
credential machinery is `_inject_model_credentials` (`main.py:3806`); there is a
`for _section …` loop (~`main.py:1840–1883`) that credentials pinned model
sections in `config_override`, plus per-capability blocks (aux `1885`, the
vision/whisper/tts/citation loop `1943`). The base-defaults-sourced models
(chat/aux) are credentialed on a separate blob pass — find it and add
`llm.subagent` (capability=`"chat"`; the reader is a chat-capable LLM).

## 4. Caveat / related finding — the existing "Browser (research subagent)" default looks **dormant**

While tracing consumption, a grep of `orchestrator/` + `src/` found **no consumer**
of the `browser` default: not `resolve_default_for_capability("browser")`, not
`BROWSER_LLM_MODEL`, not in the dispatch per-kind loop (`main.py:1943`, which is
vision/whisper/tts/citation only), not in `_resolve_default_models` (chat+aux
only), and `browser` is not in `_CATALOG_CAPABILITIES` (`postgres.py:6531`). So the
"Browser (research subagent)" row in the Defaults tab appears to **store a value
that nothing applies** — a likely pre-existing bug (dropdown exists ≠ takes
effect). This is the cautionary tale for this feature: **the UI row is inert
without the §2/§3 wiring.**

→ **Separate investigation worth filing:** confirm whether the `browser` default
is truly dead, and if so either wire it (browser research subagent should read
it) or remove the row. (May be a consumer my grep missed — e.g. the browser tool
reading the system setting directly. Not verified either way.)

## 5. Scope / implementation plan

Trivial allowlist + auto-rendered UI; the real work is §2 (floor) + §3 (creds).

| # | Change | File(s) | Effort |
|---|---|---|---|
| 5.1 | `+"subagent"` to `VALID_DEFAULT_MODEL_KINDS` | `orchestrator/main.py:5167` | trivial |
| 5.2 | `+"subagent"` to `DefaultModelKind` type + `DEFAULT_MODEL_KINDS` array + `EMPTY_DEFAULTS` | `cockpit/.../core/services/admin-providers.service.ts:40/50/61` | trivial |
| 5.3 | i18n label `admin.providers.defaults.kind.subagent` = "Subagent (delegation reader)" (en + de). The Defaults tab **auto-renders** the row from `DEFAULT_MODEL_KINDS`; `subagent` is a **chat-slot kind** (reader is a chat LLM) → falls into the existing `@else` branch = chat-capable model options. No template change. | `cockpit/src/assets/i18n/{en,de-DE}.json`, `admin-defaults.component.ts` (verify) | trivial |
| 5.4 | **Floor:** resolve `subagent` default into the base-defaults layer | `_resolve_default_models` `main.py:1057` | small |
| 5.5 | **Credentials:** inject `llm.subagent` endpoint creds into the delivery blob (§3) | `main.py` blob credential pass (locate) | **medium — the crux** |
| 5.6 | Reflect the cluster default in the picker's "Default" label: `_effective_models_from_layers` subagent slot should consult `resolve_default_for_capability("subagent")` before the `tactical → top` fallback (currently `_phase_inherit("subagent","tactical")`) | `main.py:19137` | small |
| 5.7 | Storage is automatic: `get/set_default_llm_model(kind)` (`postgres.py:6498/6514`) are generic → write `llm.default_subagent_model` system setting. GET/SET endpoints (`main.py:21485/21496`) gate on `VALID_DEFAULT_MODEL_KINDS` → 5.1 unlocks them | — | none |

**Is `subagent` catalog-validated or verbatim?** **Verbatim**, like `browser` —
keep it OUT of `_CATALOG_CAPABILITIES` (`postgres.py:6531`). No catalog model
carries a `"subagent"` capability hint, so `resolve_catalog_model(pin,
capability="subagent")` would find nothing and treat every pin as dangling.
`resolve_default_for_capability("subagent")` then returns the pin verbatim (the
non-catalog branch).

## 6. Open questions / decisions

- **Per-user subagent default too?** The Defaults tab is cluster-wide (admin).
  `UserSettingsUpdate` (`main.py:5190`) has `default_auxiliary_model` etc. but
  **v1 = cluster-only** — no `default_subagent_model` user setting unless demand
  surfaces. (If added later, `_resolve_default_models` already reads user
  settings first for chat/aux — mirror that.)
- **Session experts?** The picker deliberately omits a session-mode subagent
  control (sessions use a single Model dropdown). The cluster default applies via
  `_resolve_default_models` on session attach too (`_resolve_session_config`
  `main.py:1124` calls it), so a cluster subagent default would flow to sessions
  for free — decide whether that's wanted or should be job-only.
- **Browser dormancy** (§4) — file + fix separately, or fold into this build.

## 7. Verification plan (when built)

- **Unit:** `_effective_models_from_layers` returns the cluster subagent default
  in the `subagent` slot (source provenance) when no expert pin; expert pin still
  wins. Picker "Default" label reflects it (`model-group` spec).
- **Precedence (the regression that matters):** a job whose expert pins
  `llm.subagent.model` must keep the expert's model even when a cluster default is
  set — assert the resolved config, i.e. cluster default did NOT clobber the pin.
- **Credentials:** resolved blob for a job with only the cluster default set has
  `llm.subagent.{model,base_url,api_key}` populated (no 404).
- **k3d e2e:** set the cluster subagent default to a cheap model, run a scholar
  loop job with no expert/job subagent pin, confirm `call_type='subagent'` audit
  rows use the cluster model; then pin a different model on the expert and confirm
  the expert pin wins.

## 8. References

- **Picker this complements (built, uncommitted):** `project_spawn_subagent_light_delegation`
  (memory topic) — `model-group.component.ts`, `expert-editor.component.ts`,
  `_effective_models_from_layers` subagent slot.
- **Feature design authority:** [[delegation_light_mode_missing]]; adoption fix
  [[subagents_never_used]]; forensics + T1.1 cost lever [[loop_subagent_forensics]].
- **Precedence / why not top-of-stack:** [[loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown]]
  (the removed per-phase account defaults + the account/system floor semantics).
- **Code anchors:** `resolve_default_for_capability` `postgres.py:6535`;
  `get/set_default_llm_model` `postgres.py:6498/6514`; `_CATALOG_CAPABILITIES`
  `postgres.py:6531`; `_resolve_default_models` `main.py:1057`;
  `VALID_DEFAULT_MODEL_KINDS` `main.py:5167`; defaults GET/SET endpoints
  `main.py:21485/21496`; `_effective_models_from_layers` `main.py:19137`;
  `_resolve_subagent_config` `src/tools/delegation/spawn_subagent.py:113`;
  `DefaultModelKind`/`DEFAULT_MODEL_KINDS` `cockpit/.../core/services/admin-providers.service.ts:40/50`;
  Defaults tab `cockpit/src/app/views/admin/defaults/admin-defaults.component.ts`.
