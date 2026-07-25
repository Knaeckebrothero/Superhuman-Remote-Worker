---
tags:
  - shell-tools
  - agent
  - model-family
  - config
related:
  - "[[shell_cancel_command]]"
  - "[[config_matrix_db_overrides]]"
---

# Model-family default for agent shell mode (stateful vs stateless)

**Date:** 2026-07-24
**Status:** **Implemented & shipped on `develop`.** Routing + 12 family entries live in `HEAD`; the
base-config floor removal rode in on `57430a2a` (`chore(config): remove unused default and
persistent YAML configuration files`, the same refactor that renamed `defaults.yaml →
worker_base.yaml` / `persistent_defaults.yaml → session_base.yaml`). 94 `settings_matrix` + 15
`config_resolver` tests + `ruff` green.
**Component:** `src/core/loader.py` (`_apply_settings_matrix`), `config/model_config_matrix.yaml`,
`config/worker_base.yaml`, `config/session_base.yaml`, `docs/shell.md`.

## Motivation

The stateless-vs-persistent shell split is **tier-dependent**: frontier models drive the stateful
multi-tab shell well, while smaller models fall back to one-off script-writing and get confused by
statefulness (see `docs/shell.md`, `docs/issues/persistant_shell.md`). After shipping the stateless
simplification + `[[shell_cancel_command]]`, the follow-up was to let *capable* models opt back
into the stateful shell automatically, without losing the safe floor for weak models.

## Design — 3-layer precedence

`shell.mode` (`stateless` | `persistent`) resolves most-specific-wins:

1. **Explicit human config** — expert / project / session / request (incl. the Cockpit "Shell"
   toggle, `advanced-accordion.component.ts`). Already worked; wins.
2. **Model-family default** — capable families → `persistent`, else fall through. **NEW.**
3. **Global floor** — `stateless` (the read-site default, not a config value).

Both toggle mechanism (`create_shell_tools`, `shell_tools.py`) and the tool-name aliasing
(`get_all_tool_names`, `loader.py`) already read `config.extra["shell"]["mode"]`, so only the
*decision* needed a new layer.

## Implementation

- **Capability data** — `settings.shell_mode: persistent` added to 12 capable families in
  `config/model_config_matrix.yaml`: `claude-opus/sonnet/haiku, gpt-5, gpt-5.6, codex, codex-spark,
  o-series, gemini, deepseek, glm, minimax-m3`. The other 5 (`default, gemma, gpt-oss, mistral,
  minimax`) omit the key → stateless floor. (Kimi is *not expressible* — no `kimi` family exists in
  `family_of`/`detect_family`/matrix; adding it is a separate multi-site change.)
- **Routing** — `_apply_settings_matrix` (`loader.py`) routes `settings.shell_mode` →
  `data["shell"]["mode"]`, beside the existing `image_tokens`/`pdf_render_dpi` non-LLM cases. It
  runs **last** in every resolution path (`config_resolver.py`, static `load_agent_config`, dispatch),
  so **human-wins is automatic via presence-detection** — it only fills `mode` when no human layer
  set it.
- **Floor removal** — `worker_base.yaml` / `session_base.yaml` no longer hard-set `mode: stateless`
  (they leave it unset with an explanatory comment) so the family default can apply; the stateless
  floor is preserved by the read-sites' `.get("mode", "stateless")`.

## Where a human sets it (overrides the family default)

1. **Cockpit** → Agent Settings / Expert editor → Advanced → **Shell** dropdown (per session or per
   expert; reset → back to family default).
2. **Expert YAML** — `config/experts/<name>/config.yaml`, top-level `shell: { mode: … }`.
3. **Model-family default** — `config/model_config_matrix.yaml` `settings.shell_mode` (change which
   families default to persistent).
4. **Programmatic** — API `config_override` / `project_experts.config_override`.

## Verification

`TestApplySettingsMatrixShellMode` (unit + end-to-end): capable family → `persistent`; non-capable →
floors to stateless; explicit human `shell.mode` wins on either. Full `test_settings_matrix.py`
(94) and `test_config_resolver.py` (15) green; `ruff` clean.
