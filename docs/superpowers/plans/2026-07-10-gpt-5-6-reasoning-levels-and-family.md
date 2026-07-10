# GPT-5.6 Reasoning Levels + Model Family Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let GPT-5.6 (Luna/Terra/Sol, GA 2026-07-09) run at its new `xhigh`/`max` reasoning efforts end-to-end: matrix-driven effort clamping (no more hardcoded low/medium/high on the OpenAI/codex transports), a `gpt-5.6` model family, and a codex-proxy (CLIProxyAPI) pin bump so the proxy actually knows the 5.6 model ids.

**Architecture:** Three independent slices. (1) `src/core/loader.py`'s `_clamp_reasoning_level` becomes a ladder walk against the family's `reasoning.options` from `config/model_config_matrix.yaml` (already carried in the resolve plan's `cap`), making the matrix the single source of truth — the Cockpit dropdown is already backend-driven and already has `xhigh`/`max` labels, so it needs zero changes. (2) A new `gpt-5.6` matrix family (options `[low, medium, high, xhigh, max]`, 1M ctx) + detection branch in all four family sites, inserted *before* the `gpt-5` rules (order matters; codex rules keep precedence so a future `gpt-5.6-codex` stays `codex`). (3) Helm/deployment values bump the proxy pin `v7.2.27 → v7.2.61` (GPT-5.6 registry landed upstream in v7.2.55). `ultra` is deliberately OUT of scope — it is `reasoning.mode`, a different API parameter, not an effort level.

**Tech Stack:** Python (pytest, ruff), YAML config matrix, Angular/vitest (cockpit), Helm values.

## Global Constraints

- Work directly on `develop`. Commit per task. **NEVER push** — the user pushes explicitly.
- Local pytest env is noisy (Py3.14, missing deps); CI (Py3.12) is the gate. Run the *targeted* suites below — they are import-light and pass locally; if an unrelated import error appears, note it and rely on CI.
- `ruff check` + `ruff format` must be clean on touched Python files before each commit.
- Cockpit tests: `npx vitest run` from `cockpit/` (fast, reliable). No production build needed — no template/SCSS changes.
- `config/README.md` is stale (pre-unification) — do NOT extend it.
- Do NOT touch the OpenRouter factory's pass-through (no clamp there, by design) or `orchestrator/services/tts.py` (`_clamp_effort` is already options-driven, and read-aloud levels are server-validated to off/low/medium/high).

---

### Task 1: Capability-driven reasoning-effort clamp (loader)

**Files:**
- Modify: `src/core/loader.py:2661-2680` (`_OPENAI_REASONING_LEVELS` + `_clamp_reasoning_level`, plus the two call sites at ~`:3058` and ~`:3638`)
- Test: `tests/test_loader_routing.py` (extend `TestReasoningLevelClamping`; add `TestSupportedEfforts`, `TestFamilyOptionsDriveClamp`)

**Interfaces:**
- Consumes: `resolve_reasoning_plan(config)` → `{"method", "value", "delivery", "cap"}` (unchanged; `cap` is the family's matrix `reasoning` block incl. `options`).
- Produces: `_supported_efforts(cap: Dict[str, Any]) -> set[str]` and `_clamp_reasoning_level(level: str, supported: set[str]) -> str` (same signature, ladder semantics), module constant `_EFFORT_LADDER: tuple[str, ...]`. Task 2's tests rely on these exact names.

- [ ] **Step 1: Write the failing tests**

In `tests/test_loader_routing.py`, extend the import block at the top of the file:

```python
from src.core.loader import (
    _should_use_reasoning_summary,
    _clamp_reasoning_level,
    _supported_efforts,
    _OPENAI_REASONING_LEVELS,
    _create_openai_llm,
    _create_openrouter_llm,
    _create_codex_llm,
    detect_reasoning_method,
    reasoning_capability,
    supports_parallel_tool_calls,
)
```

Add inside the existing `class TestReasoningLevelClamping` (after `test_unknown_level_falls_back_to_high`):

```python
    def test_max_clamped_to_high_when_unsupported(self):
        """max walks the ladder down past xhigh to high on a low/medium/high family."""
        assert _clamp_reasoning_level("max", {"low", "medium", "high"}) == "high"

    def test_max_clamps_down_to_xhigh_first(self):
        """The nearest supported level below wins — never skip past one."""
        assert (
            _clamp_reasoning_level("max", {"low", "medium", "high", "xhigh"})
            == "xhigh"
        )

    def test_xhigh_and_max_pass_when_supported(self):
        gpt56 = {"low", "medium", "high", "xhigh", "max"}
        assert _clamp_reasoning_level("xhigh", gpt56) == "xhigh"
        assert _clamp_reasoning_level("max", gpt56) == "max"

    def test_level_is_case_insensitive(self):
        assert (
            _clamp_reasoning_level("XHigh", {"low", "medium", "high", "xhigh"})
            == "xhigh"
        )
```

Add two new classes directly after `TestReasoningLevelClamping`:

```python
class TestSupportedEfforts:
    """_supported_efforts derives the clamp set from the family capability."""

    def test_reads_family_options(self):
        cap = {"options": ["low", "medium", "high", "xhigh", "max"]}
        assert _supported_efforts(cap) == {"low", "medium", "high", "xhigh", "max"}

    def test_falls_back_to_openai_set(self):
        assert _supported_efforts({}) == _OPENAI_REASONING_LEVELS
        assert _supported_efforts({"options": []}) == _OPENAI_REASONING_LEVELS

    def test_normalizes_case(self):
        assert _supported_efforts({"options": ["Low", "HIGH"]}) == {"low", "high"}


class TestFamilyOptionsDriveClamp:
    """The matrix `reasoning.options` — not a hardcoded transport set — decide
    what effort reaches the wire (docs/done/family_centered_reasoning.md)."""

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_deepseek_xhigh_survives_openai_factory(self, mock_chat):
        """deepseek declares xhigh in its options → no clamp on the OpenAI wire."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="deepseek-v4", reasoning_level="xhigh")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "xhigh"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_gpt5_xhigh_still_clamped(self, mock_chat):
        """gpt-5 family still lists only low/medium/high → xhigh clamps to high."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-5.2-pro", reasoning_level="xhigh")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "high"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_loader_routing.py -x -q`
Expected: FAIL at collection with `ImportError: cannot import name '_supported_efforts'`.

- [ ] **Step 3: Implement the ladder clamp**

In `src/core/loader.py`, replace the block from `# Reasoning levels supported by each provider API` through the end of the current `_clamp_reasoning_level` (currently `:2661-2680`) with:

```python
# Reasoning levels assumed for the OpenAI wire when a family declares no usable
# `options` list (conservative fallback; the matrix `reasoning.options` is the
# source of truth — see docs/done/family_centered_reasoning.md).
_OPENAI_REASONING_LEVELS = {"low", "medium", "high"}

# Known effort levels, weakest → strongest. Clamping walks this ladder downward
# from an unsupported request (never silently exceeding the asked-for effort),
# then upward only when nothing below is supported (e.g. `minimal` on a
# low/medium/high family).
_EFFORT_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _supported_efforts(cap: Dict[str, Any]) -> set[str]:
    """Effort values a family accepts, from its matrix ``reasoning.options``.

    Falls back to the conservative OpenAI set when the capability block carries
    no usable options list (fall-through ``default`` entries, malformed blocks).
    """
    options = cap.get("options") if isinstance(cap, dict) else None
    if isinstance(options, (list, tuple)) and options:
        return {str(o).lower() for o in options}
    return set(_OPENAI_REASONING_LEVELS)


def _clamp_reasoning_level(level: str, supported: set[str]) -> str:
    """Clamp a reasoning level to the nearest supported value.

    Walks ``_EFFORT_LADDER`` downward from the requested level, then upward
    when nothing below is supported. Unknown values off the ladder fall back
    to ``high``.
    """
    level = str(level).lower()
    if level in supported:
        return level
    if level in _EFFORT_LADDER:
        idx = _EFFORT_LADDER.index(level)
        for candidate in reversed(_EFFORT_LADDER[:idx]):
            if candidate in supported:
                logger.debug(f"Clamped reasoning level '{level}' -> '{candidate}'")
                return candidate
        for candidate in _EFFORT_LADDER[idx + 1 :]:
            if candidate in supported:
                logger.debug(f"Clamped reasoning level '{level}' -> '{candidate}'")
                return candidate
    logger.debug(f"Unknown reasoning level '{level}' -> 'high' (safe fallback)")
    return "high"
```

Then update BOTH factory call sites (they are the only two callers passing `_OPENAI_REASONING_LEVELS`):

In `_create_openai_llm` (~`:3058`):
```python
        level = _clamp_reasoning_level(_rplan["value"], _supported_efforts(_rplan["cap"]))
```

In `_create_codex_llm` (~`:3638`):
```python
        level = _clamp_reasoning_level(_rplan["value"], _supported_efforts(_rplan["cap"]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_loader_routing.py -q`
Expected: PASS, including all pre-existing clamp tests (`xhigh`→`high` on gpt-5.2-pro, `minimal`→`low` on o3-mini, codex `codex/gpt-5.4-pro` `xhigh`→`high`, OpenRouter pass-through — all unchanged behavior for existing families).

- [ ] **Step 5: Lint + commit**

```bash
ruff check src/core/loader.py tests/test_loader_routing.py && ruff format src/core/loader.py tests/test_loader_routing.py
git add src/core/loader.py tests/test_loader_routing.py
git commit -m "feat(llm): drive reasoning-effort clamp from family capability options

_clamp_reasoning_level now walks an effort ladder (none..max) against the
family's matrix reasoning.options instead of a hardcoded {low,medium,high},
so families that declare xhigh/max (GPT-5.6) reach the OpenAI/codex wire
unclamped while gpt-5/o-series keep today's behavior. OpenRouter pass-through
and tts.py's options-driven clamp are unchanged."
```

---

### Task 2: `gpt-5.6` model family (matrix + 4 detection sites + tests)

**Files:**
- Modify: `config/model_config_matrix.yaml` (new `gpt-5.6:` block between `gpt-5:` and `codex:`)
- Create: `config/guardrails/gpt_5_6.yaml`
- Modify: `src/core/model_registry.py:204-207` (`family_of` branch)
- Modify: `orchestrator/services/family_matcher.py` (`_FAMILY_RULES`, between the `codex` and `gpt-5` rules)
- Modify: `cockpit/src/app/views/agent-settings/agent-settings.types.ts` (`detectModelFamily`)
- Modify: `cockpit/src/app/views/admin/config/admin-config.component.ts:40` (`FAMILIES`)
- Test: `tests/test_model_registry.py`, `tests/test_family_matcher.py`, `tests/test_settings_matrix.py`, `tests/test_loader_routing.py`, `cockpit/src/app/views/agent-settings/agent-settings.types.spec.ts`

**Interfaces:**
- Consumes: `_supported_efforts` / ladder clamp from Task 1 (the codex-path integration test asserts `max` survives).
- Produces: family key string `"gpt-5.6"` — must be byte-identical across matrix key, `family_of()`, `detect_family()`, `detectModelFamily()`, and the `FAMILIES` array (stored verbatim in `config_overrides.family`, matched against `family_of(model)` at dispatch — migration 0021).

- [ ] **Step 1: Write the failing Python tests**

`tests/test_model_registry.py` — add inside `class TestFamilyOf` (after `test_codex_spark_beats_codex`):

```python
    def test_gpt_5_6_tiers(self):
        assert family_of("gpt-5.6-sol") == "gpt-5.6"
        assert family_of("gpt-5.6-terra") == "gpt-5.6"
        assert family_of("openai/gpt-5.6-luna") == "gpt-5.6"
        assert family_of("codex/gpt-5.6-sol") == "gpt-5.6"

    def test_gpt_5_6_codex_stays_codex(self):
        # Codex checks keep precedence: a future 5.6 codex variant is `codex`.
        assert family_of("gpt-5.6-codex") == "codex"

    def test_gpt_5_5_stays_gpt_5(self):
        assert family_of("gpt-5.5") == "gpt-5"
```

`tests/test_family_matcher.py` — add to the big `@pytest.mark.parametrize` list, right after `("gpt-5-mini", "gpt-5"),`:

```python
        # GPT-5.6 tiers (Luna/Terra/Sol) — dedicated family, must beat gpt-5;
        # codex variants keep the codex family (rule order).
        ("gpt-5.6-sol", "gpt-5.6"),
        ("gpt-5.6-terra", "gpt-5.6"),
        ("openrouter/openai/gpt-5.6-luna", "gpt-5.6"),
        ("gpt-5.6-codex", "codex"),
```

`tests/test_settings_matrix.py` — add a new class after `class TestGlmFamily` (mirror its style; same helpers `_load_settings_matrix` / `_apply_settings_matrix` already imported there):

```python
class TestGpt56Family:
    def test_gpt56_registered_in_matrix(self):
        matrix = _load_settings_matrix()
        assert "gpt-5.6" in matrix
        assert matrix["gpt-5.6"]["temperature"] == 1.0
        assert matrix["gpt-5.6"]["multimodal"] is True
        assert matrix["gpt-5.6"]["parallel_tool_calls"] is False
        assert matrix["gpt-5.6"]["model_max_context_tokens"] == 1000000
        # Single context value per family — leaves derive at load, no `limits` here.
        assert "limits" not in matrix["gpt-5.6"]

    def test_gpt56_settings_applied(self):
        data = {"llm": {"model": "gpt-5.6-sol"}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        assert data["llm"]["temperature"] == 1.0
        assert data["llm"]["model_max_context_tokens"] == 1000000
        assert data["limits"]["model_max_context_tokens"] == 1000000
        assert data["limits"]["context_threshold_tokens"] == 800000  # 1000000 * 0.80
        assert data["limits"]["message_count_min_tokens"] == 400000  # 1000000 * 0.40
```

Also add to the `TestRealMatrixFamilies` parametrize list (`tests/test_settings_matrix.py:854-859` area):

```python
            ("gpt-5.6-sol", 1000000),  # GPT-5.6 (Luna/Terra/Sol): 1M ctx
```

`tests/test_loader_routing.py` — add after `TestFamilyOptionsDriveClamp` (from Task 1):

```python
class TestGpt56Reasoning:
    """gpt-5.6 family: xhigh/max are declared in the matrix and survive the
    codex (Responses API) path un-clamped."""

    def test_capability_lists_xhigh_and_max(self):
        cap = reasoning_capability("gpt-5.6-sol")
        assert cap["method"] == "effort_enum"
        assert cap["default"] == "high"
        assert cap["options"] == ["low", "medium", "high", "xhigh", "max"]

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_max_reaches_codex_responses_api(self, mock_chat):
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-5.6-sol", reasoning_level="max")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["reasoning"] == {"effort": "max", "summary": "auto"}

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_xhigh_reaches_codex_responses_api(self, mock_chat):
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/gpt-5.6-terra", reasoning_level="xhigh")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["reasoning"] == {"effort": "xhigh", "summary": "auto"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_model_registry.py tests/test_family_matcher.py tests/test_settings_matrix.py tests/test_loader_routing.py -q`
Expected: FAIL — `family_of("gpt-5.6-sol")` returns `"gpt-5"`, matrix has no `gpt-5.6` key.

- [ ] **Step 3: Implement matrix block + guardrails + the two Python detection sites**

`config/model_config_matrix.yaml` — insert between the end of the `gpt-5:` block and `codex:` (i.e. right before line `codex:`):

```yaml
gpt-5.6:
  # GPT-5.6 (GA 2026-07-09): Luna / Terra / Sol tiers, 1M ctx / 128K max output.
  # Dedicated family because effort options are a family-level property and
  # 5.6 adds xhigh + max, which older gpt-5.x rows must not offer. `ultra` is
  # NOT an effort — it's `reasoning.mode` (standard/pro/ultra), deliberately
  # unsupported here (different API param, ~4x token burn).
  reasoning: { method: effort_enum, delivery: native, default: high, options: [low, medium, high, xhigh, max] }
  prompts:
    # Aliases the gpt-5 prompt set (same wire format) — exactly how `codex`
    # does it. Fork into _gpt_5_6 files only when 5.6-specific drift shows up.
    systemprompt: systemprompt_gpt_5.txt
    systemprompt_interactive: systemprompt_interactive_gpt_5.txt
    persona: persona_gpt_5.txt
    strategic: strategic_gpt_5.txt
    tactical: tactical_gpt_5.txt
    summarization: summarization_prompt_gpt_5.txt
    memory_extraction: memory_extraction_prompt_gpt_5.txt
    memory_assembler: memory_assembler_prompt_gpt_5.txt
    curation: curation_prompt_gpt_5.txt
  guardrails:
    file: gpt_5_6.yaml
  settings:
    temperature: 1.0  # Reasoning model: only default 1.0 is supported
    multimodal: true  # GPT-5.6 supports image inputs
    # TODO: Re-enable once langchain-ai/langchain#34660 is fixed.
    # (Same Responses-API streaming constraint as gpt-5 — codex proxy path.)
    parallel_tool_calls: false
    model_max_context_tokens: 1000000  # Luna/Terra/Sol: 1M ctx, 128K max output
    max_output_tokens: 65536  # reasoning headroom (resolver clamps to ctx backstop)
    # Same 32px-patch vision scheme as the gpt-5 family.
    image_tokens: { mode: openai_patches, patch_px: 32, budget: 10000, flat: 3000 }
```

Create `config/guardrails/gpt_5_6.yaml`:

```yaml
# GPT-5.6 (Luna/Terra/Sol) guardrails
#
# GPT-5.6 shares the GPT-5 wire format. This file is intentionally empty so
# `gpt-5.6` resolves through `default.yaml` exactly like `gpt_5.yaml`. Kept
# as a separate file so 5.6-specific drift can be added later without
# touching the matrix.

tool_examples: {}

nudges: {}
```

`src/core/model_registry.py` — in `family_of()`, insert between the `codex` branch and the `gpt-5` branch (currently `:204-207`):

```python
    if "codex" in name and name.startswith("gpt-5"):
        return "codex"
    if name.startswith("gpt-5.6"):
        return "gpt-5.6"
    if name.startswith("gpt-5"):
        return "gpt-5"
```

`orchestrator/services/family_matcher.py` — in `_FAMILY_RULES`, insert between the `codex` rule and the `gpt-5` rule:

```python
    (re.compile(r"codex", re.IGNORECASE), "codex"),
    # GPT-5.6 tiers (Luna/Terra/Sol) — must beat the generic gpt-5 rule below.
    (re.compile(r"gpt-5\.6", re.IGNORECASE), "gpt-5.6"),
    # OpenAI gpt-5 family + o-series reasoning models
    (re.compile(r"gpt-5", re.IGNORECASE), "gpt-5"),
```

- [ ] **Step 4: Run Python tests to verify they pass**

Run: `python -m pytest tests/test_model_registry.py tests/test_family_matcher.py tests/test_settings_matrix.py tests/test_loader_routing.py -q`
Expected: PASS.

- [ ] **Step 5: Cockpit — detection branch, FAMILIES entry, spec**

`cockpit/src/app/views/agent-settings/agent-settings.types.ts` — in `detectModelFamily`, insert between the codex line and the gpt-5 line:

```ts
  if (name.includes('codex') && name.startsWith('gpt-5')) return 'codex';
  if (name.startsWith('gpt-5.6')) return 'gpt-5.6';
  if (name.startsWith('gpt-5')) return 'gpt-5';
```

`cockpit/src/app/views/admin/config/admin-config.component.ts:40` — add `'gpt-5.6'`:

```ts
const FAMILIES = ['gemma', 'gpt-5', 'gpt-5.6', 'gpt-oss', 'deepseek', 'glm', 'minimax', 'minimax-m3', 'codex', 'codex-spark'];
```

`cockpit/src/app/views/agent-settings/agent-settings.types.spec.ts` — add after the `describe('detectModelFamily — GLM', ...)` block (`detectModelFamily` is already imported):

```ts
describe('detectModelFamily — GPT-5.6', () => {
  it('maps GPT-5.6 tiers to the gpt-5.6 family, ahead of gpt-5', () => {
    expect(detectModelFamily('gpt-5.6-sol')).toBe('gpt-5.6');
    expect(detectModelFamily('gpt-5.6-terra')).toBe('gpt-5.6');
    expect(detectModelFamily('openai/gpt-5.6-luna')).toBe('gpt-5.6');
    expect(detectModelFamily('codex/gpt-5.6-sol')).toBe('gpt-5.6');
  });

  it('keeps neighbors unaffected', () => {
    expect(detectModelFamily('gpt-5.5')).toBe('gpt-5');
    expect(detectModelFamily('gpt-5.6-codex')).toBe('codex');
  });
});
```

- [ ] **Step 6: Run cockpit tests**

Run: `cd cockpit && npx vitest run`
Expected: PASS (all suites; the new GPT-5.6 describe block green).

- [ ] **Step 7: Lint + commit**

```bash
ruff check src/core/model_registry.py orchestrator/services/family_matcher.py tests/ && ruff format src/core/model_registry.py orchestrator/services/family_matcher.py tests/test_model_registry.py tests/test_family_matcher.py tests/test_settings_matrix.py tests/test_loader_routing.py
git add config/model_config_matrix.yaml config/guardrails/gpt_5_6.yaml src/core/model_registry.py orchestrator/services/family_matcher.py cockpit/src/app/views/agent-settings/agent-settings.types.ts cockpit/src/app/views/agent-settings/agent-settings.types.spec.ts cockpit/src/app/views/admin/config/admin-config.component.ts tests/test_model_registry.py tests/test_family_matcher.py tests/test_settings_matrix.py tests/test_loader_routing.py
git commit -m "feat(llm): gpt-5.6 model family (Luna/Terra/Sol) with xhigh/max efforts

New matrix family with options [low..max] (1M ctx, gpt-5 prompt aliases,
empty guardrails scaffold) + detection in all four family sites, ordered
before gpt-5 and after codex. With the capability-driven clamp, xhigh/max
now reach the codex proxy's Responses API; the Cockpit dropdown picks the
new options up from /api/models with no frontend change."
```

---

### Task 3: Codex-proxy pin bump (v7.2.27 → v7.2.61)

**Files:**
- Modify: `helm/values.yaml:1119` (chart default `:latest` → pinned)
- Modify: `deployment/values-experimental.yaml:129-140` (homelab dev pin + comment)
- Modify: `deployment/values-local.yaml:287-289` (local k3d pin)
- Modify: `deployment/values-local.example.yaml:240-242` (example pin)

**Interfaces:**
- Consumes: nothing from Tasks 1-2 (independent; deployable alone).
- Produces: proxy image `docker.io/eceasy/cli-proxy-api:v7.2.61` everywhere. CLIProxyAPI ≥v7.2.55 carries the GPT-5.6 (Sol/Terra/Luna) model registry; v7.2.59 revised the Sol config; v7.2.60 exposed the ultra effort to Codex clients. The currently-pinned v7.2.27 predates ALL GPT-5.6 support.

- [ ] **Step 1: Pin the chart default**

`helm/values.yaml` — replace:

```yaml
  image: docker.io/eceasy/cli-proxy-api:latest
```

with:

```yaml
  # Pin a concrete upstream version — CLIProxyAPI ships multiple builds/day and
  # `latest` makes pod restarts silently change behavior. Overlays may override.
  image: docker.io/eceasy/cli-proxy-api:v7.2.61
```

- [ ] **Step 2: Bump the deployment overlays**

`deployment/values-experimental.yaml` — replace the `image:` line and extend the comment block (keep the existing history lines, append):

```yaml
  # Bumped 2026-07-10 (v7.2.27 → v7.2.61): GPT-5.6 (Sol/Terra/Luna) model
  # registry landed v7.2.55 (GA day), Sol config revised v7.2.59, ultra effort
  # exposure v7.2.60 — v7.2.27 predates all of it, so gpt-5.6-* rows can't
  # resolve through the proxy. Verify after rollout: (1) codex auth survives
  # >13 calls (no 401 storm — the v7.1.x identity-obfuscation regression),
  # (2) no raw `<|tool_call>` leak in chat content (the v7.1.39 harmony
  # regression), (3) a gpt-5.6-sol completion succeeds. Revert this line if
  # 401s return.
  image: docker.io/eceasy/cli-proxy-api:v7.2.61
```

`deployment/values-local.yaml` and `deployment/values-local.example.yaml` — replace:

```yaml
  image: docker.io/eceasy/cli-proxy-api:v7.2.27
```

with:

```yaml
  image: docker.io/eceasy/cli-proxy-api:v7.2.61
```

- [ ] **Step 3: Render check**

Run: `helm template srw helm --set codexProxy.enabled=true 2>/dev/null | grep "cli-proxy-api"`
Expected: one line, `image: docker.io/eceasy/cli-proxy-api:v7.2.61`. (If the chart needs required values to render, fall back to: `grep -rn "cli-proxy-api" helm/ deployment/` and confirm no `:latest` or `:v7.2.27` remains.)

- [ ] **Step 4: Commit**

```bash
git add helm/values.yaml deployment/values-experimental.yaml deployment/values-local.yaml deployment/values-local.example.yaml
git commit -m "chore(helm): bump codex proxy to v7.2.61 (GPT-5.6 registry)

v7.2.27 predates GPT-5.6 support (registry v7.2.55, Sol revision v7.2.59);
also pins the chart default, which was :latest."
```

---

## Rollout notes (post-merge, manual)

1. Dev deploys via develop CI (`sha-XXX`); the proxy Deployment uses Recreate + reloader, so the image bump rolls the pod. Watch for the 401-storm / tool-call-leak regressions called out in the values comment.
2. Admin → Models: the existing `gpt-5.6-sol` / `gpt-5.6-terra` rows were created while they resolved to `gpt-5` — update their `family` field to `gpt-5.6` (dispatch-time settings/reasoning use `family_of()` and are correct regardless; the row field feeds the admin display and family-override matching). Then set `reasoning_level` (e.g. `xhigh`) as desired; the session/job dropdowns now offer X-High and Max.
3. `srw-prod-private` pins its own proxy image — bump there before the next prod cut (out of scope for this repo).

## Explicitly deferred

- **`ultra`** — `reasoning.mode` (standard/pro/ultra), a different Responses-API parameter with ~4× token burn and 1-day-old proxy support; needs its own small design (param plumbing + cost guard + quota interaction).
- **`codex` family options bump** — no gpt-5.6-codex rows exist yet; when they do, it's a config-only matrix edit.
- **OpenRouter clamp** — stays pass-through by design.
