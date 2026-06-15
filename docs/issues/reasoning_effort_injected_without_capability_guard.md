# `reasoning_effort` is injected without a model-capability guard

**Status**: Backlog — latent correctness bug + improper default for the
default model. Low-risk, self-contained fix in the LLM factories. Filed
2026-06-15.

## Context

Reasoning level is a first-class, user-settable knob: `llm.reasoning_level`
(base) plus per-phase `llm.strategic/tactical.reasoning_level`, defaulting to
`high` (`config/defaults.yaml:11`, `config/persistent_defaults.yaml:18`). It is
settable per job and per session via the Cockpit Advanced accordion → dispatch
`config_override`, and conversationally via the builder MCP tool
`update_agent_config` (`orchestrator/services/builder_tools.py:248`).

There are two delivery mechanisms:

- **prompt injection** — `Reasoning: {level}` prepended to the system prompt
  (gpt-oss via vLLM), and
- **API parameter** — `reasoning_effort` (OpenAI Chat Completions / Responses)
  or `reasoning.effort` (OpenRouter) on the request.

`detect_reasoning_method(model)` (`src/core/loader.py:2275`) classifies each
family into `prompt` / `api` / `none` and *looks* like the single source of
truth for "which models accept a reasoning control." It is not.

## Problem

**`detect_reasoning_method` only governs the prompt-injection path. The API
injection is gated purely on `reasoning_level != "none"`, with no
model-capability check at all.**

The method classifier is consumed in exactly three places — all of them prompt
loaders that decide whether to prepend `Reasoning: {level}` (system prompt
`loader.py:3161` & `:3211`, summarization `:3317`, auxiliary `:3361`). It is
**never** consulted by `create_llm`.

The API injection lives in `_create_openai_llm` (`loader.py:2512-2514`):

```python
reasoning_mode = "none"
if config.reasoning_level and config.reasoning_level != "none":
    level = _clamp_reasoning_level(config.reasoning_level, _OPENAI_REASONING_LEVELS)
    model_kwargs["reasoning_effort"] = level   # no family/capability check
```

with structurally identical, equally unguarded copies in
`_create_openrouter_llm` (`:2844`, unclamped) and `_create_codex_llm`
(`:2997`). The only gate is `reasoning_level != "none"`, and the default is
`high`.

Crucially, `_create_openai_llm` is the factory for the `openai` provider **and
every OpenAI-compatible endpoint-backed model** — endpoint rows are stamped
`provider="openai"` (`model_registry.py:204`), and `create_llm`'s final `else`
routes there (`loader.py:2413`). That set includes the locally-served vLLM
models: **gemma, minimax, gpt-oss, local qwen/llama** — not just real OpenAI.

### Consequence: even the default model gets `reasoning_effort=high`

The default model is `RedHatAI/gemma-4-31B-it-FP8-Dynamic` with
`reasoning_level: high`. Trace:

1. `defaults.yaml` sets `llm.reasoning_level: high`. Nothing overrides it —
   `settings_matrix.yaml`, the guardrails, and `model_config_matrix.yaml` do
   not touch `reasoning_level`, and the per-model catalog value does not
   propagate (see below).
2. gemma is endpoint-backed → `provider="openai"` → `_create_openai_llm`.
3. `reasoning_level="high" != "none"` → `model_kwargs["reasoning_effort"] = "high"`
   is sent to the vLLM endpoint.

gemma's family is `gemma` and `detect_reasoning_method("gemma") == "none"`, but
that classification has no effect on this path. So we forward a
`reasoning_effort` the model does not support. It has been benign only because
vLLM tolerates/ignores the unknown field — but gemma's reasoning parser is
known-fragile under tool calling (vllm#39043; this is exactly why the Cockpit
`getReasoningOptions` hides the control for gemma), so "currently ignored" is
not a guarantee.

### Why Claude / Gemini are safe (and it's almost by accident)

`claude-*` and `gemini` route to `_create_anthropic_llm` / `_create_google_llm`,
which contain **no reasoning injection code**. They are safe because of the
factory split, *not* because `detect_reasoning_method` returns `none` for them.
The `none` classification and the API gate are wired to different things.

### Exposure summary

| Model / family | Factory | Today | Risk |
|---|---|---|---|
| gemma (default), minimax (vLLM) | `_create_openai_llm` | `reasoning_effort=high` sent | improper; relies on vLLM ignoring it |
| gpt-oss (vLLM) | `_create_openai_llm` + prompt | reasoning applied **twice** (API + `Reasoning:` line) | redundant / conflicting |
| real `gpt-4o` | `_create_openai_llm` | would send `reasoning_effort` | **400** — OpenAI rejects it on non-reasoning models |
| bare `llama` / `default` / custom endpoint | `_create_openai_llm` | sends `reasoning_effort` | 400 or ignored, endpoint-dependent |
| claude-*, gemini | anthropic / google | nothing injected | safe |
| gpt-5, codex, o-series, deepseek/qwen reasoning | openai / codex / openrouter | `reasoning_effort` | correct |

`gpt-4o` is not seeded as a chat model in the live stack today (it appears only
in `config/schema.json` examples and a legacy `VISION_MODEL`), so that row is
latent. The gemma/minimax row is **active** — it's the default model.

### Secondary finding: catalog `reasoning_level` is metadata-only

The per-model catalog column `models.reasoning_level` (e.g. gemma seeded
`reasoningLevel: null`, `orchestrator/seed/llm_config.py:49`) surfaces on
`ModelMeta.reasoning_level` and the Admin model API, but is **never applied to a
job/session's effective config**. The only reasoning value injected at dispatch
is `user_settings.default_reasoning_level` (`main.py:1136-1142`); the catalog
row merely seeds that user setting (`main.py:17605`). So an operator who sets a
model's reasoning level in Admin → Models expecting it to constrain that model
gets no effect — it can't currently be used to suppress injection per-model
either. This is why the fix can't simply be "set gemma's catalog row to none."

## Proposal

Gate the API injection on an actual capability signal. Recommended layering:

1. **Immediate floor — family allow-list in the factories.** Add a
   `_API_REASONING_FAMILIES` set (`gpt-5`, `codex`, `codex-spark`, `o-series`,
   plus the reasoning variants of `deepseek`/`qwen`, and `gpt-oss` *only* for
   its prompt path) and, in `_create_openai_llm` / `_create_openrouter_llm` /
   `_create_codex_llm`, inject only when `family_of(config.model)` is in it.
   Zero schema change; stops the improper injection for gemma/minimax/gpt-4o
   today. Failure direction is safe (omit reasoning rather than 400). Mirror the
   logic the frontend `reasoning-options.ts` already encodes so the two don't
   drift.

2. **Durable per-model control — make the catalog authoritative.** Plumb
   `ModelMeta.reasoning_level` into the effective config at dispatch (when the
   user/job hasn't explicitly set one), and give it an explicit `none` value
   that always wins. Lets operators turn reasoning off for a specific
   endpoint-backed model without code changes. Note the real seed payload lives
   in the deployment repo (HomeLab `llm.yaml`), so the in-repo seed example can
   only document the convention.

Option 1 is the fix; option 2 is the follow-up that makes the knob operators
already see in Admin actually mean something. A capability `bool` on the catalog
(`supports_reasoning`) is an alternative to encoding it in the family allow-list
if we prefer data over heuristic — more correct, but a migration + seed + UI
change.

## Acceptance

- Building an LLM for gemma / minimax / a real gpt-4o with `reasoning_level=high`
  does **not** put `reasoning_effort` on the request (assert on the built
  `model_kwargs` / `extra_body`).
- gpt-5 / codex / o-series still receive `reasoning_effort` (and native
  gpt-5/o-series still get the Responses `reasoning.summary` path).
- gpt-oss still gets its single `Reasoning: {level}` prompt line and no longer
  also gets the API param.
- A unit test pins the family→inject matrix so backend and
  `reasoning-options.ts` can't silently diverge.
- (If option 2 is taken) a catalog row with `reasoning_level = none` suppresses
  injection for that model even when the family would otherwise allow it.

## Notes

- **Groq doc/code drift (cosmetic).** `detect_reasoning_method`'s docstring
  lists Groq under `none`, but the code doesn't — a groq model resolves by its
  stripped family. Harmless because `_create_groq_llm` (`loader.py:2735+`) never
  forwards `reasoning_effort` (only `top_p`/`top_k`). The one oddity worth a
  line: gpt-oss-via-groq classifies as `prompt` and would still prepend a
  `Reasoning:` line. Fold a docstring fix into the same change.
- **`minimal`/`xhigh` collapse on native OpenAI.** `_clamp_reasoning_level`
  (`loader.py:2328`, `_OPENAI_REASONING_LEVELS = {low, medium, high}`) maps
  `minimal→low`, `xhigh→high` on the OpenAI/Responses paths; only OpenRouter
  passes the full six through (unclamped). The Cockpit offers all six for
  OpenRouter/gpt-oss, so a user who picks `xhigh` on a native OpenAI model
  silently gets `high`. Intentional per the comments, but undocumented to the
  user — worth a note or a UI hint, not necessarily code.
- Quick runtime confirmation of the gemma claim: inspect the outbound request
  body (or vLLM access log) on a live session and check for `reasoning_effort`.
  Code-path analysis is unambiguous; this just confirms vLLM's handling.
