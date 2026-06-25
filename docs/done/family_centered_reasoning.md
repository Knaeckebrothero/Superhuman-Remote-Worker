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

**Status:** **✅ SHIPPED — Slices A + B implemented + k3d-verified 2026-06-24; moved to
`done/`.** Slice A committed as `fc72dd70`; Slice B (orchestrator `/api/models` + cockpit)
and this doc move are uncommitted on `develop` at move time. Design agreed 2026-06-22, research-refined
the same day (3 codebase-mapping + 3 web-research agents). Intentionally deferred (§10):
B3 catalog per-model `reasoning_level` override (precedence layer 3 — inert until an
operator sets it); token-budget control for Claude/Gemini (Phase 2).

### What shipped

- **Slice A — backend delivery.** Per-family `reasoning` block in
  `config/model_config_matrix.yaml` (16 families); new `reasoning_capability()` /
  `resolve_reasoning_plan()` / `_set_nested()`; `detect_reasoning_method()` reimplemented
  on the capability (hardcoded family list deleted → YAML is the source of truth);
  OpenAI/OpenRouter/Codex factories deliver from the plan. Net behavior: gemma →
  `chat_template_kwargs.enable_thinking` (was an inert `reasoning_effort`); gpt-oss
  double-injection fixed; minimax inert effort stopped; effort families unchanged;
  OpenRouter `xhigh` preserved. Resolves
  [[reasoning_effort_injected_without_capability_guard]].
- **Slice B — API + UI feed.** `/api/models` returns `reasoning_by_model`
  (`{model_id: {method, default, options}}`, family-derived); `ModelService` caches it
  (`reasoningByModel` signal + `ReasoningCapability` type); `reasoning-options.ts`
  rewritten capability-driven (every hardcoded family/provider branch deleted) — gemma
  flips hidden → **On/Off**, gpt-oss → Low/Med/High, `always_on` → "Always on"; both
  consumers (`model-group`, `advanced-accordion`) read the capability via the service.

### Verification (k3d, 2026-06-24)

| Check | Result |
|---|---|
| Backend tests (`test_loader_routing` + matrix/prompt/config suites) | ✅ 387 green |
| Cockpit specs (reasoning-options, model-group, advanced-accordion, model.service) | ✅ 69 green |
| `ruff check`/`format` + `tsc --noEmit` | ✅ clean |
| Deployed agent image builds gemma → `enable_thinking:True` & no `reasoning_effort`; level=none → `False`; gpt-5.5 → `reasoning_effort:high` | ✅ |
| Live `gemma-4-moe` endpoint honors `enable_thinking` → `reasoning_content` populates; `tools=[…]` doesn't suppress | ✅ |
| `/api/models` over the real catalog → gemma `{binary_toggle, On/Off}`, gpt-5.5 `{effort_enum, Low/Med/High}` | ✅ |

Not captured: a live job-dispatch agent log (cluster agent-pool drift / zombie pods — a
pre-existing lifecycle quirk, not this change), so the deployed image's LLM-build was
exercised directly instead (equivalent); the Cockpit dropdown was not visually walked
(backend contract + frontend logic both verified).

## 1. Goal

Make the **model family the single source of truth** for reasoning control: the *kind*
of control a family has, the values it accepts, the default, and how the chosen value is
delivered on the wire. Backend (delivery + clamp + default) and Cockpit (the options
shown, and whether to show a control at all) both derive from that one declaration in
`config/model_config_matrix.yaml`, so they can no longer drift.

## 2. Motivation

Reasoning has no cross-provider standard. The web research (citations §A) confirms the
control space is **four mechanically-incompatible kinds** — and a single model can move
between them across versions:

| Kind | Families (real, mid-2026) | Wire shape | Disable? |
|---|---|---|---|
| **effort_enum** | OpenAI (`reasoning_effort`), gpt-oss (`Reasoning:` line) | string enum | model-dependent; o-series floors at `low` |
| **token_budget** | Anthropic (`thinking.budget_tokens`), Gemini (`thinkingConfig.thinkingBudget`) | int + min/max + sentinels | Anthropic yes; Gemini per-model |
| **binary_toggle** | gemma / Qwen3 / GLM (`enable_thinking`, default **on**); Granite / DeepSeek-V3.1 (`thinking`, default **off**) | bool kwarg | yes |
| **always_on** | DeepSeek-R1, QwQ, **Gemini 2.5 Pro** | none / floor only | **no** — disable is a silent no-op |

Two traps inside this that the design must respect:

- **Two *incompatible* effort enums.** gpt-oss (harmony) accepts **only `low/medium/high`**
  (default `medium`). Hosted OpenAI/OpenRouter accept a superset
  (`none/minimal/low/medium/high/xhigh/max`), model-dependent. A flat shared enum is wrong —
  this is exactly why per-family `options` is mandatory. (It's also why the gpt-oss UI list
  is wrong today; see §9.)
- **`always_on` needs a capability flag.** For DeepSeek-R1 etc. a "disable" toggle is
  silently ignored by the model — the UI must not offer "off" and validation must reject it
  before the wire.

Today SRW's knowledge of all this is **split across four places that drift**:

1. `detect_reasoning_method()` (`src/core/loader.py`) — hardcoded family → `prompt`/`api`/`none`.
2. `_clamp_reasoning_level()` + `_OPENAI_REASONING_LEVELS={low,medium,high}` — one hardcoded set.
3. Per-factory injection (`_create_openai_llm` / `_create_openrouter_llm` / `_create_codex_llm`)
   — gated only on `reasoning_level != "none"`, no capability check.
4. `cockpit/.../agent-settings/reasoning-options.ts` — a hand-maintained **duplicate** of the
   above, string-matching model names client-side (its own comment says "Mirrors backend logic").

`config/model_config_matrix.yaml` already owns every other model-dependent trait per family
(prompts, instructions, guardrails, sampling, context, multimodal, image tokens). Reasoning
is the one axis missing.

## 3. Current state — ground truth (from codebase mapping)

### 3.1 How reasoning is delivered per family TODAY

| Family | detect_method | Factory | Wire today | Notes |
|---|---|---|---|---|
| gpt-5, gpt-4o, o-series, default | api | openai | `model_kwargs.reasoning_effort` (clamped low/med/high) | |
| codex, codex-spark | api | codex | Responses `reasoning.{effort,summary}` **or** `model_kwargs.reasoning_effort` | dual path |
| openrouter/*, deepseek, glm, qwen, llama | api | openrouter | `extra_body.reasoning.effort` (**unclamped** → keeps xhigh) | |
| **gemma** | none | openai | **`reasoning_effort=high` still sent** (inert — wrong knob) | the bug ([[reasoning_effort_injected_without_capability_guard]]) |
| **minimax, minimax-m3** | none | openai | `reasoning_effort` sent (inert) | may actually be toggle/append-think — verify |
| claude-opus/sonnet/haiku | none | anthropic | **nothing** | no reasoning control wired at all |
| gemini | none | google | **nothing** for control; `include_thoughts=True` for 2.5/3 (output capture only) | |
| groq, mistral | none / — | groq / mistral | **nothing** | |
| gpt-oss | prompt | openai | **`Reasoning: {level}` system-prompt prefix** | the one prompt-delivery family |

Family detector `family_of()` (`src/core/model_registry.py:140-215`) yields 19 families.
Capture side (`src/llm/reasoning_chat.py`) already reads **both** `reasoning_content` *and*
`reasoning` (+ `reasoning_details`, Responses blocks), streaming and non-streaming — so it
tolerates the vLLM `reasoning_content`→`reasoning` rename and is **independent of the control
layer**.

### 3.2 Where the setting lives + precedence (as-built)

Config keys: `llm.reasoning_level` (base, default `high`) + `llm.reasoning_method` (auto) +
per-phase `llm.strategic/tactical.reasoning_level` + `context_management.reasoning_level`
(summarization) + the **auxiliary** LLM's own level. Effective order for a dispatch:

1. **Job/session override** (`config_override.llm[.strategic|.tactical].reasoning_level`).
2. **User default** (`user_settings.default_reasoning_level`) — injected at dispatch **only if
   the job didn't set one** (`orchestrator/main.py:1653-1660`).
3. **Expert/role config** → **base config** (`defaults.yaml:11` / `persistent_defaults.yaml:18`).
4. **Phase override** applied at LLM-build time; **dataclass default `"high"`** as floor.

### 3.3 Three confirmed defects this closes

- **Inert/wrong knob.** gemma (and minimax) get `reasoning_effort=high` forwarded to vLLM
  though the model reads `enable_thinking`, not `reasoning_effort`. Harmless only because vLLM
  ignores it — a real `gpt-4o` would 400.
- **No control for Claude/Gemini.** The factories inject nothing; reasoning is whatever the
  provider defaults to.
- **Catalog column is dead.** `models.reasoning_level` is seeded, serialized to the Admin API,
  and surfaced on `ModelMeta` — but **never consumed** at dispatch (`resolve_model()` isn't on
  the agent's config-load path). Promoting it to a real per-model override is net-new wiring.

## 4. Design principles

- **One declaration, two consumers.** The family `reasoning` block is authoritative; backend
  and frontend both read it (frontend via `/api/models`, never a re-implementation).
- **Separate *kind* from *wire*.** `method` says what the values mean + how to validate;
  `wire` says how to put the chosen value on the request. gpt-oss is `effort_enum` *delivered*
  by a system-prompt prefix — kind and delivery are orthogonal.
- **Per-family `options`/bounds, never a global list.** Two effort enums + per-model budget
  ranges make a shared list incorrect.
- **Capability-gated UI** (the Cursor/Cline pattern): drive affordances off `method` — hide the
  control for non-reasoning models, no "off" for `always_on`, a toggle for `binary_toggle`, a
  dropdown of exactly `options` for `effort_enum`.
- **Fail safe + fail loud.** Unknown/invalid value → drop to family default (degrade), never
  forward something that errors. Reject "off" for `always_on` before the wire.
- **Version-resilient.** Key the wire mapping on **family**, and make values **data**, so the
  Anthropic `budget_tokens`→adaptive and Gemini `thinkingBudget`→`thinking_level` migrations are
  config edits, not code.
- **Per-deployment override.** The matrix already deep-merges a `<deployment_dir>` overlay → a
  deployment can change a family's reasoning default with no code change.

## 5. The `reasoning` family block

```yaml
<family>:
  reasoning:
    method: effort_enum | token_budget | binary_toggle | always_on | none
    default: <value | null>          # family default when nothing else set
    options: [ ... ]                 # effort_enum / binary_toggle: allowed values (UI + validation)
    budget: { min: N, max: N, disable: <sentinel|null>, dynamic: <sentinel|null> }  # token_budget only
    wire:
      location: model_kwargs | extra_body | system_prompt | responses_api | top_level
      param: <dotted.path>           # e.g. reasoning_effort, reasoning.effort,
                                      #      chat_template_kwargs.enable_thinking, thinking.budget_tokens
      transform: passthrough | bool_map | prompt_prefix | effort_to_budget
      map: { on: true, off: false }  # bool_map
      prefix: "Reasoning: {value}"   # prompt_prefix
    constraints: [ ... ]             # optional, e.g. anthropic: no_temperature, no_forced_tool_use
```

**v1 family declarations** (what SRW actually runs):

```yaml
gemma:        # THE fix — was sending inert reasoning_effort
  reasoning:
    method: binary_toggle
    default: "on"
    options: ["on", "off"]
    wire: { location: extra_body, param: chat_template_kwargs.enable_thinking,
            transform: bool_map, map: {on: true, off: false} }

gpt-oss:      # corrected: low/medium/high ONLY (not the six)
  reasoning:
    method: effort_enum
    default: medium
    options: [low, medium, high]
    wire: { location: system_prompt, transform: prompt_prefix, prefix: "Reasoning: {value}" }

openai:       # gpt-5 / gpt-4o-reasoning / o-series (default family)
  reasoning:
    method: effort_enum
    default: high
    options: [minimal, low, medium, high]   # xhigh/none version-dependent — verify per model (§9)
    wire: { location: model_kwargs, param: reasoning_effort, transform: passthrough }

openrouter:
  reasoning:
    method: effort_enum
    default: high
    options: [none, minimal, low, medium, high, xhigh]   # xhigh→max upstream
    wire: { location: extra_body, param: reasoning.effort, transform: passthrough }

codex:        # keep existing Responses-API dual path
  reasoning:
    method: effort_enum
    default: high
    options: [low, medium, high]
    wire: { location: responses_api, param: reasoning.effort, summary: auto }

# Declared but NOT user-controllable in v1 (stops the inert injection; UI shows "Default" only)
claude:  { reasoning: { method: none, default: "off" } }   # Phase 2 → token_budget (§10)
gemini:  { reasoning: { method: none, default: "off" } }   # Phase 2 → token_budget; keep include_thoughts capture
minimax: { reasoning: { method: none, default: "off" } }   # verify: may be binary_toggle/append-think
mistral: { reasoning: { method: none } }
groq:    { reasoning: { method: none } }
```

Families without a block resolve to implicit `method: none`. The **Phase-2 token_budget**
shape (designed now, wired later) for reference:

```yaml
claude:   # Phase 2
  reasoning:
    method: token_budget
    default: "off"
    budget: { min: 1024, max: 32768, disable: 0, dynamic: null }
    wire: { location: top_level, param: thinking.budget_tokens, transform: effort_to_budget }
    constraints: [no_temperature, no_top_p, no_top_k, no_forced_tool_use]   # Anthropic hard rules
```

**Placement.** Add a new top-level `reasoning` section per family → add `"reasoning"` to the
matrix-parser whitelist (`src/core/loader.py:324`, the `("prompts","instructions","settings",
"guardrails")` tuple) + a `_matrix_subsection(matrix,"reasoning")` reader + (if we want DB
overrides) a `_reasoning_override_for(family)` parallel to `_settings_override_for` and the
`kind="reasoning"` whitelist in `set_config_overrides`. Lower-friction alternative: nest under
`settings` and read it the way `image_tokens` is read (routed to `limits`, not `llm`). **Decision:
top-level section** — reasoning is a capability, not an inference param; explicit reader beats
overloading `settings`. (Cost: the extra DB-override plumbing — flagged so it isn't forgotten.)

## 6. Resolution & precedence

Effective value, highest first — unchanged shape, plus the catalog gets a real job and a
family floor replaces the blanket `high`:

1. **Runtime override** (job/session `config_override`, incl. per-phase).
2. **User default** (`default_reasoning_level`).
3. **Catalog row** (`models.reasoning_level` → per-model override; currently dead, newly wired).
4. **Family default** (`<family>.reasoning.default`) — replaces blanket `llm.reasoning_level: high`
   leaking onto families that can't use it.

At **every** layer the value is **validated against the family's `options`/`budget`**; an
out-of-set value clamps (effort: nearest neighbor) or drops to the family default, never
forwarded blind. Resolution must apply consistently to **base, per-phase (strategic/tactical),
summarization, and the auxiliary LLM** — the aux path (`create_auxiliary_llm`) has its own
reasoning resolution and is a known divergence point (§7).

## 7. Backend changes + coupling risks

**Changes:**
- `detect_reasoning_method(model)` → read `<family>.reasoning.method` (fallback to the current
  heuristic for undeclared families; non-breaking during rollout).
- Factories inject by `method`/`wire`:
  - `effort_enum` → `reasoning_effort` (model_kwargs) / `reasoning.effort` (extra_body) / Responses
    / `Reasoning:` prefix, per `wire.location`.
  - `binary_toggle` → set `wire.param` (e.g. `chat_template_kwargs.enable_thinking`) in extra_body
    via `map`. **The gemma fix.**
  - `always_on` / `none` → inject nothing (and for `always_on`, reject an incoming "off").
  - `token_budget` → Phase 2.
  Injection fires only when `method` matches the factory's mechanism → the ungated-`reasoning_effort`
  leak is closed by construction.
- Replace the global `_clamp_reasoning_level` with **per-family clamp** against `options` (fixes
  OpenRouter `xhigh` being silently downgraded to `high`).
- Default resolution falls back to `<family>.reasoning.default`.

**Must NOT break (coupling risks surfaced by the mapping agent):**
- **Capture/streaming** — `reasoning_chat.py`'s SSE tap + non-stream extractor key on
  `reasoning_content`/`reasoning`/`reasoning_details`/Responses blocks. A toggle turning reasoning
  *off* legitimately yields empty capture; the tap must not error on absence. No field renames.
- **Tool-calling** — gemma worker requests ship `tools=[]`; verified live that `enable_thinking`
  does **not** suppress tool calls (§11). Don't route any new reasoning through the Responses API
  for tool-bearing calls (LangChain Responses tool-streaming is broken — only codex uses it today).
- **Per-phase / aux divergence** — a phase override can set `reasoning_method` without
  `reasoning_level`; and the aux LLM resolves reasoning separately. Apply the family resolution
  uniformly and validate that a phase/aux value is legal for that model's family.
- **DB-override plumbing** — a new top-level matrix section needs its `kind` whitelisted or
  user-authored overrides silently drop.

## 8. Frontend changes

- **Expose the capability** on `/api/models`: add a `reasoning` object
  (`{method, default, options}` — bounds for token_budget) to `_serialize_catalog_model`
  (`orchestrator/main.py:19882`) and the `/api/models` payload. The model row already carries
  `family`; the orchestrator resolves the family block and ships the resolved capability per model.
- **`getReasoningOptions(model)`** stops string-matching → reads `reasoning.options` for the
  selected model from the fetched list; keep a `[Default]`-only fallback for unknown models.
- **Affordance by kind:** `effort_enum` → dropdown of exactly `options`; `binary_toggle` → On/Off
  (gemma flips from *hidden* to a real toggle); `always_on` → no control, show "always reasons";
  `none` → "Default" only.
- **Footprint** (from the cockpit mapping): `core/models/api.model.ts` (+`ReasoningCapability`,
  `Model.reasoning`/`family`), `reasoning-options.ts` (consume capability), `model.service.ts`
  (parse + `getReasoningOptions`), `model-group.component.ts` + `advanced-accordion.component.ts`
  (call the service). `settings.component.ts` user-default dropdown stays static. Specs:
  `model-group.component.spec.ts`, `advanced-accordion.component.spec.ts`, `expert-config.spec.ts`,
  + a `model.service.spec.ts`.

## 9. Implementation slices

**Slice A — backend-first (k3d-verifiable immediately):**
1. Add `reasoning` blocks to `config/model_config_matrix.yaml` (v1 families, §5).
2. Whitelist the `reasoning` matrix section + reader (+ DB-override plumbing).
3. Data-drive `detect_reasoning_method` + factory injection + per-family clamp; add the
   `binary_toggle` delivery path; treat `always_on`/`none` as no-inject.
4. Family-default fallback in resolution; apply uniformly to phase + summarization + aux.
5. Tests: extend `tests/test_loader_routing.py` to pin per-family (param, value) — assert gemma
   sends `chat_template_kwargs.enable_thinking` and **no** `reasoning_effort`; gpt-5/codex still
   get effort; gpt-oss still gets the prompt line; openrouter keeps `xhigh`.

**Slice B — API + UI feed:**
6. `reasoning` capability on `/api/models` + serializer.
7. Rewrite `reasoning-options.ts` to consume it; delete hardcoded branches; capability-gated
   affordances; add specs.
8. Wire the catalog `reasoning_level` per-model override (precedence layer 3).

## 10. Scope decisions

- **v1 covers `effort_enum` + `binary_toggle` + `always_on` + `none`** — i.e. every model SRW
  actually runs (gemma, gpt-oss, openai/codex/openrouter incl. deepseek/glm/qwen/llama). Wins:
  gemma toggle works, gpt-oss corrected, OpenRouter `xhigh` survives, inert `reasoning_effort`
  stops for gemma/minimax/claude/gemini/mistral.
- **`token_budget` control for Claude/Gemini → Phase 2.** It's net-new (factories inject nothing
  today), carries hard provider constraints (Anthropic: no temp/top_p/top_k/forced-tools, min
  budget 1024; Gemini 2.5 Pro can't disable → `always_on`), and needs an effort→budget mapping.
  v1 declares them `none` (stops nothing that works today). The schema + mapping tables (§A) are
  designed now so Phase 2 is a data+adapter add, not a redesign.
- **Unified effort scale vs raw:** we use **per-family `options`** (effectively per-family-raw with
  a friendly UI), **not** a lossy global effort→budget scale. The mapping tables matter only if/when
  Phase 2 wants one user scale spanning effort + budget families.

## 11. Acceptance criteria (k3d smoke)

Backend slice done when, on k3d (`gemma-4-moe` via in-cluster LiteLLM → tunnel → homelab):
- gemma reasoning **on** (default) → populated `reasoning_content`; **off** → empty. Verified by
  the built request carrying `chat_template_kwargs.enable_thinking` + a live call. (Baseline
  2026-06-22: default → on ~440–550 compl tok; `enable_thinking=false` → ~107 tok; `tools=[…]`
  does **not** suppress.)
- gemma dispatch carries **no** `reasoning_effort`.
- gpt-5/codex/o-series still receive `reasoning_effort`; gpt-oss still gets one `Reasoning:` line;
  openrouter `xhigh` survives unclamped.
- `tests/test_loader_routing.py` pins the family→(param,value) matrix so backend and
  `reasoning-options.ts` can't diverge.

UI slice done when each model's dropdown shows exactly the family's affordance (gemma On/Off;
Claude/Gemini/Groq Default-only; OpenRouter the six; always_on shows "always reasons"), sourced
from `/api/models`, with no model-name string-matching left in `reasoning-options.ts`.

## 12. Open questions & known-imperfect

- **Per-family option exactness (the "fix later" the team OK'd).** gpt-oss = `low/medium/high`
  (current UI wrongly offers six). OpenAI `minimal` is GPT-5+ only (o-series floors at `low`);
  `none`/`xhigh` are version/model-dependent. Treat the exact `options` per family as a
  verify-at-impl pass against current provider docs — providers churn fast.
- **vLLM auto-toggle nuance.** vLLM auto-injects `enable_thinking` from `reasoning_effort`
  (`"none"`→off). For gemma we deliver the toggle **explicitly** (more robust than relying on
  auto-injection or the homelab orchestrator's injected default — which SRW doesn't own).
- **minimax** — declared `none` in v1; vLLM has a `minimax_m2_append_think` parser, so it may be
  `binary_toggle`/append-style. Verify before flipping it on.
- **deepseek family** spans always-on R1, hybrid V3.1 (`thinking`, default off), and OpenRouter
  effort — `family_of` collapses them to one `deepseek`. v1 keeps effort (status quo); add an
  `always_on`/`binary_toggle` row if a local R1/V3.1 endpoint is introduced.
- **Anthropic adaptive-thinking drift** — newest Claude moves `budget_tokens`→effort-based
  `adaptive`; Phase 2's `wire` must be version-keyed.

## 13. Prior art & best practices (research synthesis)

- **Two camps:** gateways (OpenRouter, LiteLLM) expose a unified effort knob and *translate*
  per backend (effort→token-budget); SDKs (Vercel AI SDK, LangChain) pass each provider's native
  param raw and normalize only the *output*. IDE assistants are hybrid and **capability-gate the
  UI** (Cursor omits the "Thinking" variant when unsupported; Cline gates the Anthropic checkbox
  on a Claude model). Our per-family declarative schema = the hybrid, at the right altitude.
- **Output normalization is universally agreed** even by tools that refuse to normalize input —
  we already do this in `reasoning_chat.py`; keep it.
- **Fail-loud > silent-drop.** LiteLLM errors by default and only drops with `drop_params=True`;
  OpenRouter silently drops reasoning on some models (still billed). Our validation should reject
  out-of-capability values, with drop/coerce as an explicit choice.
- **Billing:** reasoning tokens bill as **output** everywhere; `exclude`/hide still bills. Surface
  the resolved default in the UI so spend isn't a surprise.

## 14. Decision summary

- Reasoning becomes a declared per-family capability (`method` ∈ {effort_enum, token_budget,
  binary_toggle, always_on, none} + `default` + `options`/`budget` + `wire`) in
  `model_config_matrix.yaml`. Backend + Cockpit both read it; the `reasoning-options.ts` duplicate
  is retired.
- Subsumes the standalone gemma `enable_thinking` pin and resolves
  [[reasoning_effort_injected_without_capability_guard]]; promotes the dead catalog
  `reasoning_level` column to a real per-model override.
- **v1** = effort_enum + binary_toggle + always_on + none (everything SRW runs). **Phase 2** =
  token_budget control for Claude/Gemini (schema + mapping tables ready now).
- Build backend-first (k3d-verifiable), then API + UI feed.

## Appendix A — provider reasoning-control reference (web research, 2026-06-22)

Cross-provider summary (verify exact enums/ranges at impl — high version drift):

| Provider | Param | Kind | Values / range | Default | Disable? |
|---|---|---|---|---|---|
| OpenAI | `reasoning_effort` (CC) / `reasoning.effort` (Responses) | effort_enum | GPT-5: `minimal,low,medium,high`; o-series: `low,medium,high` | medium | floor `minimal`; no off |
| Anthropic | `thinking.{type,budget_tokens}` | token_budget | int ≥1024, < max_tokens | off | yes (`disabled`) |
| Gemini | `thinkingConfig.thinkingBudget` (+`includeThoughts`) | token_budget | per-model; `0`=off, `-1`=dynamic; 2.5 Pro 128–32768 **can't disable** | on (Pro/Flash), off (Flash-Lite) | per-model |
| gpt-oss/harmony | `Reasoning:` system line | effort_enum | `low,medium,high` only | medium | no clean off |
| Qwen3/GLM/gemma | `chat_template_kwargs.enable_thinking` | binary_toggle | on/off (default **on**) | on | yes |
| Granite/DeepSeek-V3.1 | `chat_template_kwargs.thinking` | binary_toggle | on/off (default **off**) | off | yes |
| DeepSeek-R1/QwQ | — | always_on | — | on | **no** (toggle = no-op) |
| OpenRouter | `reasoning.{effort,max_tokens,enabled,exclude}` | unified | effort `none..max`; or budget | enabled=medium | yes (`enabled:false`) |

**Effort→token-budget mapping tables (for Phase 2):**
- LiteLLM (`constants.py`, env-overridable): minimal→1024(floor), low=1024, medium=2048, high=4096,
  xhigh=8192, max=16384.
- OpenRouter: `budget = clamp(max_tokens * ratio, 1024, 128000)`; ratios minimal 0.1 / low 0.2 /
  medium 0.5 / high 0.8 / max,xhigh 0.95.

**Citations:** OpenAI reasoning guide (developers.openai.com/api/docs/guides/reasoning); Anthropic
extended thinking (platform.claude.com/docs/en/build-with-claude/extended-thinking + Bedrock mirror);
Gemini thinking (ai.google.dev/gemini-api/docs/thinking, firebase.google.com/docs/ai-logic/thinking);
vLLM reasoning outputs (docs.vllm.ai/en/latest/features/reasoning_outputs/) incl. vllm#38855;
Qwen3 card (huggingface.co/Qwen/Qwen3-8B); GLM-4.x cards (huggingface.co/zai-org); OpenAI harmony
(developers.openai.com/cookbook/articles/openai-harmony); OpenRouter reasoning-tokens
(openrouter.ai/docs/guides/best-practices/reasoning-tokens); LiteLLM reasoning_content + drop_params
(docs.litellm.ai); Vercel AI SDK (vercel.com/docs/ai-gateway/capabilities/reasoning); LangChain
init_chat_model (docs.langchain.com/oss/python/langchain/models).
