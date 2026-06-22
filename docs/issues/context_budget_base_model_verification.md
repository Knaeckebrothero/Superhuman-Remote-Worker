---
tags:
  - issue
  - verification
  - context-management
  - model-config
  - experiment
related:
  - "[[context_budget_uses_base_model_not_phase_models]]"
---

# Verification plan: prove the context budget is sized from the base `llm.model`, not the phase models

**Filed:** 2026-06-22, as the controlled repro for
[[context_budget_uses_base_model_not_phase_models]]. The root-cause doc traces the bug
through the code + the live dev job `9639710d` (gpt-5.5/gpt-5.4-mini capped at gemma's
131072). This doc is the **falsifiable test** that proves (or refutes) the mechanism with a
model that has *nothing to do with gpt-5 / codex-proxy / LiteLLM* — to rule those out.

**Status:** ✅ CONFIRMED 2026-06-22 — budget follows the base `llm.model`. See [Results](#results).

> Line numbers were accurate on 2026-06-22 and will drift — re-grep `model = llm_data.get("model"`
> + `limits<-derived` (`src/core/loader.py`), `Created OpenAI LLM` (`src/core/loader.py`),
> `family_of` (`src/core/model_registry.py`) when acting on this.

## Hypothesis (the falsifiable claim)

The agent's **global** context budget (`config.limits.model_max_context_tokens`) is derived
from `family_of(llm.model)` — the **base/primary** model slot — in `_apply_settings_matrix`
(`src/core/loader.py:577`). The per-phase `strategic`/`tactical` models carry no window of
their own, so they **inherit that global budget** (`src/graph.py:1258`). Therefore:

> Any job that overrides only `strategic`/`tactical` to a >128K model while leaving the base
> `llm.model` at the gemma default is capped at **gemma's 131072**, regardless of the phase
> model's true window — and regardless of provider/transport.

If true, this is **independent of the model family**. gpt-5 was just the first one we hit.

## Mechanism under test

```
_apply_settings_matrix (loader.py:577):  family = family_of(llm.model)   # base slot
                          (loader.py:626-630): limits.model_max_context_tokens = family_window
                                               threshold      = 0.80 * family_window
                                               msg_count_min  = 0.40 * family_window
graph.py:1258:  model_max = phase_llm_config.model_max_context_tokens (None) or config.limits.…
```

`family_of()` mappings relevant here (`model_registry.py:163-213`), all verified:
- `RedHatAI/gemma-4-31B-it-FP8-Dynamic` → `gemma` → **131072**
- `minimax/minimax-m3` → `minimax-m3` → **1000000**
- `gemini-3.5-flash` → `gemini` → **1000000**
- `gpt-5.5` → `gpt-5` → **1050000**

## Experiment — single-variable contrast

Two scholar jobs. **Only the base `llm.model` differs**; `strategic`/`tactical` are identical
(both `minimax/minimax-m3`). minimax-m3 is chosen deliberately — see
[model choice](#why-minimax-m3-and-not-gemini).

| Job | `strategic`/`tactical` | base `llm.model` | **Predicted budget** | Predicted threshold (0.8×) |
|---|---|---|---|---|
| **A** (repro)   | `minimax/minimax-m3` | gemma (default, unset) | **131072**  | 104857 |
| **B** (control) | `minimax/minimax-m3` | **`minimax/minimax-m3`** | **1000000** | 800000 |

`config_override` payloads:

```jsonc
// Job A — mirrors the failing gpt-5.5 job: phase models set, base left at the gemma default
{ "llm": { "strategic": { "model": "minimax/minimax-m3" },
           "tactical":  { "model": "minimax/minimax-m3" } } }

// Job B — identical, plus the ONE changed line: base model = minimax-m3
{ "llm": { "model":     "minimax/minimax-m3",
           "strategic": { "model": "minimax/minimax-m3" },
           "tactical":  { "model": "minimax/minimax-m3" } } }
```

## What to observe (no need to grow context to 139K)

The budget is decided at **agent startup**, when the LLM clients are built — long before any
overflow. Read the `max_context_tokens=` field on the client-creation line:

```bash
# Find the agent handling the test job, then read the creation line:
for p in $(kubectl --context=main -n superhuman-remote-worker get pods \
            -l srw/managed-by=agent-provisioner -o name); do
  kubectl --context=main -n superhuman-remote-worker logs "$p" -c agent --tail=20000 2>/dev/null \
    | grep -E "Created OpenAI LLM: model=minimax" | grep "<JOB_ID>"
done
```

Expected lines:
- **Job A:** `Created OpenAI LLM: model=minimax/minimax-m3 … max_context_tokens=131072`
- **Job B:** `Created OpenAI LLM: model=minimax/minimax-m3 … max_context_tokens=1000000`

The number flips from **131072 → 1000000** when the *only* change is the base model. That is
the proof.

### Optional confirmatory step (the loud symptom)

Let **Job A** run until accumulated context crosses 131072. Because minimax-m3 routes through
`ReasoningChatOpenAI` (has the Layer-0 preflight, `reasoning_chat.py:783-815`), it should
reproduce the **identical synthetic 413** + stuck compaction loop seen on `9639710d`:
`Context overflow at HTTP layer: … exceeds limit of 131,072`, with **zero non-200s from the
provider** (the request never leaves the pod).

## The falsifier

- If **Job A** shows `max_context_tokens=1000000` with a gemma base → **hypothesis is wrong**;
  the budget is *not* coming from the base model and the root-cause doc needs revision.
- If **Job B** still shows `131072` after setting base = minimax-m3 → the base-model lever
  isn't the (only) cause; look for a global cap injected at dispatch.

## Why minimax-m3, and not gemini

Both are 1M-window and registered on dev, but the **client path differs**, which changes
whether the *symptom* reproduces:

| Model | Family window | Factory | Layer-0 preflight? | Symptom if budget=131072 |
|---|---|---|---|---|
| `minimax/minimax-m3` | 1,000,000 | OpenRouter → `ReasoningChatOpenAI` | **yes** | **loud** — synthetic 413 + stuck loop (matches `9639710d`) |
| `gemini-3.5-flash`   | 1,000,000 | Google → `ChatGoogleGenerativeAI`  | **no**  | quiet — over-compacts at 131K, call still succeeds |
| `gpt-5.5` (original) | 1,050,000 | codex-proxy → `ReasoningChatOpenAI`| yes     | loud (already observed) |

Only `openai / openrouter / mistral / codex` factories wrap `ReasoningChatOpenAI`; `google /
anthropic / groq` do not (`loader.py:2447-2456`, creation sites at `:2621/:2975/:3060/:3179`).
So **gemini still proves the *budget* bug** (its clients/compaction will show `131072`), but
it won't crash the same way — making it a noisier signal. Use minimax-m3 for a clean,
same-symptom repro; gemini is a fine secondary check that the bug isn't OpenAI-client-specific.

## Results

**Run 2026-06-22. Verdict: ✅ CONFIRMED — the budget follows the base `llm.model`.**

### Method 1 — live cluster A/B (blocked by an unrelated grant gate)

Created on dev: Job A `13dd903f-8d74-450c-b4ae-35cf2a31005c`, Job B
`36e91e93-acec-495d-a9fa-a08495d22ddb` (both `config_name=scholar`, the
`config_override`s above). Workspaces + agents provisioned, but **both were
dispatch-denied at the capability gate** before any agent ran (0 audit entries):

```
Dispatch denied: shell_tools: tools.shell requires the shell_tools grant;
                 delegation: delegation requires the delegation grant   (main.py:2014)
```

scholar + `defaults.yaml` request `tools.shell` (run_command) and
`tools.delegation`; the **MCP-token identity** I submitted under lacks those
grants (the cockpit user has them, which is why the original job ran). This is
an **MCP-scripted-job auth gotcha, not a property of the bug** — see
[follow-up](#follow-up-finding). Jobs cancelled, workspaces reaped.

### Method 2 — direct `_apply_settings_matrix` (decisive)

Called the function under test against the real `config/model_config_matrix.yaml`,
single-variable (strategic/tactical = `minimax/minimax-m3` in **both**; only the
base model differs):

| Job | base `llm.model` | `family_of(base)` | `model_max_context_tokens` | threshold |
|---|---|---|---|---|
| **A** | `RedHatAI/gemma-4-31B-it-FP8-Dynamic` | `gemma` | **131072** | 104857 |
| **B** | `minimax/minimax-m3` | `minimax-m3` | **1000000** | 800000 |

Predictions hit **exactly**. The budget flips 131072 → 1,000,000 when the *only*
change is the base model — with identical 1M-window phase models. minimax-m3
(OpenRouter) shares nothing with gpt-5/codex-proxy/LiteLLM, so this rules those
out as the cause.

### Corroborating live evidence (already on record)

The original job `9639710d` logged, in the full live dispatch path:
`Created OpenAI LLM: model=gpt-5.5 … max_context_tokens=131072` — a `gpt-5`
(1.05M-family) model capped at gemma's 131072. Method 2 extends that to a second
family and proves the base-model flip lifts it.

### Follow-up finding

MCP-created **worker** jobs are dispatch-denied for `shell_tools` / `delegation`
unless the submitting identity holds those grants. For future scripted repros:
drop those tools in `config_override` (e.g. `"tools": {"shell": [], "delegation": []}`)
or submit under a granted identity. Tracked as a usability note; not part of this bug.

## Decision matrix

| Outcome | Meaning | Next |
|---|---|---|
| A=131072, B=1000000 | Hypothesis **confirmed**, base-model is the lever | Ship the proper fix (derive budget from phase models) in [[context_budget_uses_base_model_not_phase_models]] |
| A=1000000 | Hypothesis **refuted** | Re-investigate; budget not from base model |
| A=131072, B=131072 | Base model isn't the (only) lever | Look for a dispatch-time global cap |
