---
tags:
  - feature
  - architecture
  - llm
  - config
related:
  - "[[reasoning_effort_injected_without_capability_guard]]"
  - "[[gemma_session_findings]]"
  - "[[db_backed_model_catalog]]"
  - "[[db_backed_llm_config]]"
  - "[[custom_llm_endpoints]]"
aliases:
  - family-centered reasoning
  - reasoning capability matrix
  - per-family reasoning levels
---

# Family-Centered Reasoning Levels

**Status:** **Proposed — design agreed 2026-06-22; not yet implemented.**
Recommended path: backend-first slice (delivery + default + gemma toggle,
k3d-verifiable immediately), then the API + UI feed as a second pass. The
gemma `toggle` behavior this design formalizes was verified live on k3d
2026-06-22 (see §10) — those results are the regression contract.

## 1. Goal

Make the **model family the single source of truth** for reasoning control:
what kind of control a family has, which values are valid, what the default
is, and how the chosen value is delivered on the wire. Both the backend
(delivery + clamping + defaulting) and the Cockpit UI (the options shown)
derive from that one declaration, so they can no longer drift.

## 2. Motivation

Reasoning has no cross-provider standard. Families differ not just in
*values* but in *kind* of control:

- **binary toggle** — Gemma 4's `chat_template_kwargs.enable_thinking`
  (on/off), no levels.
- **effort enum** — OpenAI `none/low/medium/high`; OpenRouter adds
  `minimal`/`xhigh` (the latter mapped to "max" upstream).
- **prompt-injected level** — gpt-oss via vLLM (`Reasoning: {level}` in the
  system prompt).
- **token budget** — Anthropic extended thinking (a `budget_tokens` number,
  a different axis entirely). Not modeled in v1; see §9.
- **no control** — Gemini, Groq today.

Today SRW's knowledge of this is **split across four places that drift**:

1. `detect_reasoning_method()` (`src/core/loader.py`) — hardcoded
   family → `prompt`/`api`/`none` map.
2. `_clamp_reasoning_level()` (`src/core/loader.py`) — a hardcoded
   `{low, medium, high}` set for the OpenAI path.
3. The per-factory API injection (`_create_openai_llm` /
   `_create_openrouter_llm` / `_create_codex_llm`) — gated only on
   `reasoning_level != "none"`, **with no capability check** (see
   [[reasoning_effort_injected_without_capability_guard]]).
4. `cockpit/src/app/views/agent-settings/reasoning-options.ts` — a
   hand-maintained **duplicate** of all of the above, string-matching model
   names client-side. Its own comment admits it "Mirrors backend logic."

`config/model_config_matrix.yaml` already owns every other model-dependent
trait per family (prompts, instructions, guardrails, sampling, context,
multimodal, image tokens) — reasoning is the one axis conspicuously missing.

### Two concrete defects this closes

- **Inert/wrong knob for endpoint-backed models.** The default model
  (gemma) gets `reasoning_effort: high` forwarded to vLLM even though gemma
  reads `enable_thinking`, not `reasoning_effort`. Harmless only because
  vLLM ignores the unknown field — and a real `gpt-4o` would 400. (Full
  analysis: [[reasoning_effort_injected_without_capability_guard]].)
- **No app-level control of Gemma's thinking.** The UI hides reasoning for
  gemma entirely; the backend classifies it `none`. So Gemma's reasoning is
  at the mercy of the upstream model-orchestrator default (the homelab
  injection — see [[gemma_session_findings]] and §10), which SRW does not
  control and which can silently flip off.

## 3. Design principles

- **One declaration, two consumers.** The family block in
  `model_config_matrix.yaml` is authoritative; backend and frontend both
  read it (frontend via an API, never a re-implementation).
- **Declare the *kind*, not just the values.** A flat list of strings can't
  describe a binary toggle, an effort enum, and a token budget at once. The
  block carries `method` + `options` + `default` + `wire`.
- **Fail safe.** When a family has no `reasoning` block or the chosen value
  isn't valid for it, omit the param (degrade to provider default) rather
  than forward something that errors.
- **No silent client/server drift.** Deleting the hardcoded family logic in
  `reasoning-options.ts` is an explicit goal, not a side effect.
- **Per-deployment override.** `model_config_matrix.yaml` already
  deep-merges a `<deployment_dir>` overlay, so a deployment (e.g. homelab)
  can change a family's reasoning default with no code change.

## 4. The `reasoning` family block

Add a `reasoning` section per family. Shape:

```yaml
<family>:
  reasoning:
    method: toggle | effort | prompt | none      # how it's delivered (+ 'budget' later)
    default: <value>                             # used when nothing else is set
    options: [<value>, ...]                      # exactly what the UI offers + backend validates
    wire:                                        # delivery detail, method-specific
      ...
```

Examples (values illustrative; see §9 for the gpt-oss correctness caveat):

```yaml
gemma:
  reasoning:
    method: toggle
    default: "on"
    options: ["on", "off"]
    wire:
      param: chat_template_kwargs.enable_thinking   # rides in extra_body
      map: { "on": true, "off": false }

# OpenAI native reasoning models (gpt-5, o-series, reasoning 4o)
openai:
  reasoning:
    method: effort
    default: high
    options: [none, low, medium, high]
    wire: { param: reasoning_effort }               # model_kwargs (Chat Completions)

openrouter:
  reasoning:
    method: effort
    default: high
    options: [none, minimal, low, medium, high, xhigh]   # xhigh → "max" upstream
    wire: { param: reasoning.effort, in: extra_body }

gpt-oss:                                            # vLLM, prompt-injected
  reasoning:
    method: prompt
    default: medium
    options: [low, medium, high]                    # ⚠ see §9 — current code wrongly offers 6
    wire: { prompt_prefix: "Reasoning: {value}" }

claude:
  reasoning: { method: none, default: "off", options: [] }   # 'budget' method deferred (§9)
```

Families without a `reasoning` block resolve to an implicit
`method: none` (UI shows only "Default"; backend injects nothing) — i.e.
the safe default for anything not yet declared.

**Placement.** A new top-level `reasoning` section per family, which means
adding `"reasoning"` to the section whitelist in the matrix parser
(`src/core/loader.py`, the `("prompts", "instructions", "settings",
"guardrails")` tuple). Alternative with zero parser change: nest it under
the existing `settings` block (the way `image_tokens` already nests a dict
there) and read it via a helper. We prefer the explicit top-level section —
reasoning is a capability, not an inference param, and keeping it a sibling
of `settings` reads cleaner.

## 5. Resolution & precedence

Effective reasoning value for a dispatch, highest priority first:

1. **Runtime override** — per-job/session, from the Cockpit *Advanced*
   accordion → `config_override` (`llm.reasoning_level`, and per-phase
   `llm.strategic/tactical.reasoning_level`).
2. **User default** — `user_settings.default_reasoning_level`, injected at
   dispatch when the job didn't set one (`orchestrator/main.py`).
3. **Catalog row** — the per-model `models.reasoning_level` column
   (`ModelMeta.reasoning_level`). Today this is metadata-only; this design
   gives it a real job as the per-model override of the family default.
4. **Family default** — `<family>.reasoning.default`. Replaces the blanket
   `llm.reasoning_level: high` in `config/defaults.yaml` being applied to
   families that can't use it.

At every layer the value is **validated against `<family>.reasoning.options`**;
an out-of-set value is clamped (effort: nearest neighbor, as
`_clamp_reasoning_level` does today) or dropped (→ family default), never
forwarded blind.

## 6. Backend changes

- **`detect_reasoning_method(model)`** → look up `<family>.reasoning.method`
  (fallback to the current heuristic for families without a block, so the
  change is non-breaking during rollout).
- **Factories** (`_create_openai_llm`, `_create_openrouter_llm`,
  `_create_codex_llm`) → inject by `method`:
  - `effort` → `reasoning_effort` (model_kwargs) or `reasoning.effort`
    (extra_body), per `wire`.
  - `prompt` → prepend `wire.prompt_prefix` (the existing gpt-oss path).
  - `toggle` → set `wire.param` (e.g. `chat_template_kwargs.enable_thinking`)
    in `extra_body`, mapped via `wire.map`. **This is the gemma fix.**
  - `none` → inject nothing.
  - Injection only fires when `method` matches the factory's mechanism, so
    the ungated-`reasoning_effort` leak is closed by construction.
- **`_clamp_reasoning_level`** → clamp against `<family>.reasoning.options`
  instead of the hardcoded `_OPENAI_REASONING_LEVELS`.
- **Default resolution** → fall back to `<family>.reasoning.default`.

## 7. Frontend changes

- **Expose the capability.** Add a `reasoning` object
  (`{method, default, options}`) to `_serialize_catalog_model`
  (`orchestrator/main.py`) and `/api/models`. The model row already carries
  `family`, so the orchestrator resolves the family's block and ships the
  resolved capability with each model.
- **`getReasoningOptions(model)`** → stop string-matching; read the
  `reasoning.options` of the selected model from the fetched model list.
  Keep a minimal `[Default]`-only fallback for an unknown/old model.
- For gemma this flips the control from **hidden** to a real **On / Off**
  toggle (`options: ["on","off"]`, default On).

## 8. Implementation slices

**Slice A — backend-first (verifiable on k3d immediately):**
1. Add `reasoning` blocks to `config/model_config_matrix.yaml`
   (gemma, openai, openrouter, gpt-oss, claude/gemini/groq = none).
2. Whitelist the `reasoning` section in the matrix parser.
3. Data-drive `detect_reasoning_method` + the factory injection + the clamp;
   add the `toggle` delivery path.
4. Family-default fallback in resolution.
5. Tests: extend `tests/test_loader_routing.py` (it already pins the
   reasoning matrix) — assert per-family that the right param (and only
   that param) lands on the built LLM; add a gemma-toggle case.

**Slice B — API + UI feed:**
6. `reasoning` capability on `_serialize_catalog_model` / `/api/models`.
7. Rewrite `reasoning-options.ts` to consume it; delete the hardcoded
   family branches; add a Cockpit spec.
8. Per-model catalog override wiring (precedence layer 3), if not done in A.

## 9. Open questions & known-imperfect

- **gpt-oss option list is wrong today (flagged, fix during impl).** The
  current `reasoning-options.ts` offers gpt-oss all six levels
  (`none/minimal/low/medium/high/xhigh`); gpt-oss effort is really
  `low/medium/high`. v1 declares `[low, medium, high]`, but the exact set
  (and whether `none` is even meaningful for a model that always reasons)
  needs a correctness pass against the gpt-oss/harmony spec. Tracked here so
  it isn't silently carried forward.
- **Anthropic `budget` method deferred.** Extended thinking is a token
  budget, not an enum — a different `method` and `wire` shape. v1 declares
  claude as `none`; add `budget` when we want a thinking control for Claude.
- **`xhigh`/`minimal` collapse.** Native OpenAI clamps `minimal→low`,
  `xhigh→high`; only OpenRouter passes the full set. The per-family
  `options` makes this explicit instead of silent, but the clamp mapping
  for cross-family value reuse still needs a defined table.
- **Endpoint-served families that share a base model** (e.g. a vLLM-hosted
  qwen/deepseek reasoning variant) — confirm `family_of()` resolves them to
  a family that has the right `reasoning` block, or add rows.

## 10. Acceptance criteria (k3d smoke)

Backend slice is done when, on k3d (`gemma-4-moe` via the in-cluster
LiteLLM → tunnel → homelab path):

- A gemma dispatch with reasoning **on** (default) produces populated
  `reasoning_content`; with reasoning **off** it does not — verified by the
  built request carrying `chat_template_kwargs.enable_thinking` and by a
  live call. (Baseline 2026-06-22: default → reasoning on ~440–550
  completion tokens, `enable_thinking=false` → off ~107 tokens, and
  `tools=[…]` does **not** suppress it.)
- A gemma dispatch carries **no** `reasoning_effort` on the wire.
- A gpt-5/codex/o-series dispatch still receives `reasoning_effort`
  (effort path unbroken).
- A gpt-oss dispatch still gets its single `Reasoning: {value}` prompt line
  and no API param.
- `tests/test_loader_routing.py` pins the family → (param, value) matrix so
  backend and `reasoning-options.ts` can't diverge.

UI slice is done when the Cockpit reasoning dropdown for each model shows
exactly the family's `options` (gemma = On/Off; Claude/Gemini = Default
only; OpenRouter = the six), sourced from `/api/models`, with no
model-name string matching left in `reasoning-options.ts`.

## 11. Decision summary

- Reasoning becomes a declared, per-family capability in
  `model_config_matrix.yaml` (`method` + `default` + `options` + `wire`).
- Backend delivery, defaulting, and clamping all read it; the Cockpit reads
  it via `/api/models`. The hardcoded duplicate in `reasoning-options.ts`
  is retired.
- This subsumes the standalone "pin gemma `enable_thinking`" option and
  resolves [[reasoning_effort_injected_without_capability_guard]]; it also
  promotes the catalog `reasoning_level` column from metadata to a real
  per-model override.
- Build backend-first (k3d-verifiable), then the API + UI feed.
- Known carried-forward imperfection: gpt-oss option list needs a
  correctness pass (§9) — explicitly deferred, not dropped.
