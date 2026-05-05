# Guardrails Matrix — design doc

**Status:** draft, 2026-05-05
**Author:** session continuation from `docs/issues/gemma_session_findings.md`
**Scope:** make every model-facing string in the agent stack family-aware via the existing `model_config_matrix.yaml` resolution chain. First slice: tool docstrings.

---

## TL;DR

Today, the agent emits hardcoded English strings to the LLM from ~45 sites across `src/`: tool docstrings (LangChain serializes `__doc__` into `tools[].function.description` on every request), runtime nudges in the graph, footer/header sections in todo/recall/knowledge formatters, and error returns from tool wrappers. Many of these strings include **Python parens-style call examples** (`tool_name(arg="x")`) that teach the wrong wire format to strict parsers — vLLM's `gemma4` tool-call parser was the precipitating incident (`docs/issues/gemma_tool_call_parser_loop.md`).

This doc proposes a **fourth section** in `config/model_config_matrix.yaml` — `guardrails:` — parallel to the existing `prompts:`, `instructions:`, `settings:` sections. Each family points at a single `guardrails/<family>.yaml` file holding keyed strings for runtime nudges and per-tool example blocks. At tool-bind time, we **strip the `Examples:` block from each tool's docstring and inject the family-correct one**. At runtime-nudge sites, we look up the keyed string instead of using a hardcoded f-string.

The resolution chain is the same one already used for prompts and instructions: family-specific → `default` → hardcoded fallback. Every family currently in the matrix gets a guardrails file (most inherit ~everything from `default`; only `gemma` overrides aggressively).

**First migration slice:** all tool docstrings (`src/tools/`) — highest blast radius because LangChain re-serializes them on every request. Graph nudges and config templates follow in subsequent slices.

---

## 1. Problem recap

From the four-agent codebase audit (see `docs/issues/gemma_session_findings.md` §findings):

| Surface | Sites | Highest-severity examples |
|---|---|---|
| Tool docstrings (`Examples:` blocks) | 9 | `git_log()`, `shell_execute(...)`, `next_phase_todos(todos=[...])` |
| Tool error/return wrappers | 5 | `read_file('...')` enforcement pattern |
| Graph runtime nudges | 20 | `next_phase_todos()` parens in budget rewind, `todo_complete with note` |
| Phase boundary helpers | several | `call job_complete` references |
| Manager + service formatters | 6 | todo footer, memory/knowledge delimiters |
| Config templates / expert instructions | 5+ groups | `kb_write(type=...)` parens baked into expert prompts |

Single common thread: **the model sees these strings on every request, treats them as few-shot examples, and copies their surface form**. When that surface form doesn't match the inference engine's parser, we get 1385× tool-call rejection loops.

The fix is structural: every model-facing string becomes resolvable per family.

---

## 2. The matrix extension

### 2.1 New section: `guardrails`

Adding a fourth subsection to each family block in `config/model_config_matrix.yaml`. It follows the exact same shape as `prompts:` and `instructions:` — a flat key→filename map, with the file living under a new `config/guardrails/` directory.

```yaml
# config/model_config_matrix.yaml (fragment)
default:
  prompts:    {...}     # existing
  instructions: {...}   # existing
  settings:   {...}     # existing
  guardrails:
    file: default.yaml  # NEW — resolves to config/guardrails/default.yaml

gemma:
  prompts:    {...}
  instructions: {...}
  settings:   {...}
  guardrails:
    file: gemma.yaml    # config/guardrails/gemma.yaml
```

**Why a single `file:` pointer per family instead of one key per string?** Two reasons:
1. Guardrails contain dozens of related multi-line blocks (one `tool_examples.<name>` per registered tool, one `nudges.<name>` per injection site). Inlining them in the matrix would explode it from ~280 lines to ~2000+.
2. Resolution is family-grained, not key-grained. A family either uses the default guardrails or it overrides the whole bundle. Keeping the bundle in one file keeps the override unit coherent.

The single-file pointer doesn't preclude **partial override** — at load time, `guardrails/<family>.yaml` is *deep-merged* on top of `guardrails/default.yaml`. A family file only needs to redefine the keys it changes.

### 2.2 Loader extension

The matrix loader (`src/core/loader.py`) already accepts arbitrary subsections — line 179 only allow-lists `prompts`, `instructions`, `settings` and rejects the rest. The change is:

```python
# src/core/loader.py:179 (existing)
if section in ("prompts", "instructions", "settings"):
# becomes:
if section in ("prompts", "instructions", "settings", "guardrails"):
```

…plus a new `_load_guardrails_matrix(deployment_dir=None)` symmetrical to `_load_settings_matrix`, plus a new `resolve_guardrails(model, deployment_dir=None)` symmetrical to `resolve_model_settings`.

`resolve_guardrails(model)` returns a fully-merged `dict` containing all keys from `default.yaml` overridden by family-specific keys. This is the single object every consumer (tool binder, graph nudge sites, manager formatters) uses.

### 2.3 File layout

```
config/
  model_config_matrix.yaml     # existing — adds `guardrails: file:` per family
  guardrails/                  # NEW
    default.yaml               # baseline (Python parens form for OpenAI-compatible parsers)
    gemma.yaml                 # Gemma canonical wire format
    gpt_oss.yaml               # gpt-oss harmony format quirks
    minimax.yaml               # MiniMax M2.7 specifics
    gpt_5.yaml                 # GPT-5 / Codex (OpenAI Responses API)
    codex.yaml                 # alias to gpt_5 unless we find drift
    codex_spark.yaml           # alias to gpt_5
  prompts/                     # existing, unchanged
  templates/                   # existing, unchanged
  experts/                     # existing, unchanged
```

**Note**: `gpt-5`, `codex`, `codex-spark` all use OpenAI Responses API and the same parens form. They could share a file via YAML anchors, or each have its own to allow future drift. Recommend: separate files, currently identical content, anchored from a shared block to avoid copy-paste drift.

---

## 3. Schema of a guardrails file

Two top-level sections: `tool_examples` (per-tool `Examples:` blocks injected into docstrings at bind time) and `nudges` (keyed strings used at runtime injection sites in graph/managers/persistent).

### 3.1 `tool_examples`

Map of `tool_name` → multi-line example block. Each block is what LangChain will serialize as the `Examples:` portion of `tools[].function.description`. The block format is **family-specific** — the consumer doesn't know or care what wire syntax is in there.

```yaml
# config/guardrails/default.yaml
tool_examples:
  next_phase_todos: |
    Examples:
        next_phase_todos(
            todos=["Extract document text from PDF", "..."],
            phase_name="Phase 1: Document Processing",
        )

  git_log: |
    Examples:
        git_log()                       # last 10 commits, compact
        git_log(max_count=5)            # last 5 commits
        git_log(oneline=False)          # full commit details

  shell_execute: |
    Examples:
        shell_execute(command="pytest tests/ -x")
        shell_execute(command="npm run dev", name="dev", is_async=True)
        shell_execute(command="C-c", name="dev", keys=True)
```

```yaml
# config/guardrails/gemma.yaml
tool_examples:
  next_phase_todos: |
    Examples:
        <|tool_call>call:next_phase_todos{todos:[<|"|>Extract document text<|"|>],phase_name:<|"|>Phase 1<|"|>}<tool_call|>

  git_log: |
    Examples:
        <|tool_call>call:git_log{}<tool_call|>                       # last 10 commits
        <|tool_call>call:git_log{max_count:5}<tool_call|>
        <|tool_call>call:git_log{oneline:false}<tool_call|>

  shell_execute: |
    Examples:
        <|tool_call>call:shell_execute{command:<|"|>pytest tests/ -x<|"|>}<tool_call|>
        <|tool_call>call:shell_execute{command:<|"|>npm run dev<|"|>,name:<|"|>dev<|"|>,is_async:true}<tool_call|>
```

```yaml
# config/guardrails/gpt_oss.yaml — gpt-oss has a harmony-style format that
# benefits from showing the channel structure explicitly. Most tools
# can still use parens form (OpenAI tool-calls), but the Examples block
# header sets context.
tool_examples:
  next_phase_todos: |
    # Tool calls are emitted on the `commentary` channel as JSON.
    Examples:
        next_phase_todos(
            todos=["..."],
            phase_name="...",
        )
  # Most other tools inherit from default.yaml unchanged.
```

### 3.2 `nudges`

Map of nudge-key → string template. Templates use **named-field `{}` placeholders** (Python `str.format`-style), filled by the call site. The set of placeholders for each key is part of the contract — documented inline in the file and validated at load time (any unknown placeholder = ValueError).

```yaml
# config/guardrails/default.yaml
nudges:
  # Sites: src/graph.py:1371-1380 (todo-action recovery nudge)
  # Placeholders: {todo_id}
  todo_action: |
    Action required: complete the current todo `{todo_id}` now by invoking the `todo_complete` tool.
    Use the tool-call format defined in your system prompt — do not type the call as plain text.

  # Sites: src/graph.py:2939-2951 (phase-budget hard cap rewind)
  # Placeholders: {used}, {cap}
  budget_rewind: |
    PHASE BUDGET REACHED: {used}/{cap} tool calls consumed.
    Before staging new todos:
      1. Review what worked and what didn't
      2. Identify root causes for the failures
      3. Stage smaller, more focused todos
    Do not repeat the same sequence of calls.

  # Sites: src/graph.py:1193-1208 (degenerate-response recovery)
  # Placeholders: {pattern_detail}
  degenerate_recovery_assistant: |
    My previous response was degenerate — it contained repetitive or malformed output. I need to retry with a shorter, well-formed response.
  degenerate_recovery_user: |
    Your last response was detected as degenerate and has been discarded. Issues found: {pattern_detail}.
    If you were trying to call a tool, retry with smaller arguments or a different approach.

  # Sites: src/graph.py:3020-3023 (loop-warning suffix appended to ToolMessage)
  # Placeholders: none
  loop_warning_suffix: |
    [LOOP WARNING] You have called this tool with the same arguments multiple times. You may be stuck in a loop.
    Consider a different approach: try different arguments, use a different tool, or mark the current todo as blocked and move on.

  # Sites: src/graph.py:3048-3051 (tool-not-found recovery suffix)
  # Placeholders: {tool_name}
  tool_not_found_suffix: |
    If your current todo requires {tool_name}, mark it as blocked using the `todo_complete` tool with a note explaining which tool is needed, and proceed with the next todo.

  # Sites: src/managers/todo.py:409 (todo-list footer)
  # Placeholders: none
  todo_list_footer: |
    Tools available: `todo_complete` (mark a task finished — pass the todo id), `todo_rewind` (revisit a completed task — pass the todo id), `mark_complete` (signal the current phase is done).
    Invoke them via the tool-call format defined in your system prompt.

  # Sites: src/services/recall_store.py:982,990,997 (memory block boundaries)
  memory_block_header_pinned: "--- Pinned Memories (TTL-active) ---"
  memory_block_header_retrieved: "--- Retrieved Memories (relevance-ranked) ---"
  memory_block_footer: "--- End Memories ({count} items, ~{tokens:,} tokens) ---"

  # Sites: src/services/knowledge_store.py:538,545
  knowledge_block_header: "--- Project Knowledge ---"
  knowledge_block_footer: "--- End Knowledge ({count} notes, ~{tokens:,} tokens) ---"

  # Sites: src/persistent_graph.py:598,673 (empty-response fallback)
  empty_response_recovery: "⚠ The model returned an empty response. Please try again or switch models."

  # Sites: src/tools/context.py:567, src/tools/registry.py:632, src/tools/workspace/files.py:851,926
  # Placeholders: {file_path}, {tool_name}
  read_file_required_error: |
    Error: You must read `{file_path}` before using {tool_name}. It contains critical instructions for this operation.
    Read it first, then invoke {tool_name} again.
```

```yaml
# config/guardrails/gemma.yaml — only the keys whose surface form matters
nudges:
  todo_action: |
    Action required: complete the current todo `{todo_id}` now by invoking the `todo_complete` tool.
    The tool-call format is `<|tool_call>call:fn{{key:<|"|>val<|"|>}}<tool_call|>` — use braces, never parens.

  budget_rewind: |
    PHASE BUDGET REACHED: {used}/{cap} tool calls consumed.
    Stage new todos by invoking the `next_phase_todos` tool. Use the canonical brace format defined in your system prompt — do not type Python-style calls.
    Before staging:
      1. Review what worked
      2. Identify root causes
      3. Stage smaller, more focused todos

  todo_list_footer: |
    Tools available: `todo_complete`, `todo_rewind`, `mark_complete`. All invocations use the canonical Gemma tool-call format from your system prompt: `<|tool_call>call:fn{{...}}<tool_call|>`.
    Never type tool calls as plain text or with parens.

  read_file_required_error: |
    Error: You must read `{file_path}` before using {tool_name}. It contains critical instructions for this operation.
    Read it first by invoking the `read_file` tool, then invoke {tool_name} again.

  # tool_not_found_suffix, loop_warning_suffix, memory/knowledge headers/footers,
  # empty_response_recovery, degenerate_recovery_* — inherit from default.yaml
```

### 3.3 Validation

At load time:
- Each `tool_examples.<name>` key MUST correspond to a tool registered in `TOOL_REGISTRY` (warn on orphans, error on missing required tools — TBD).
- Each `nudges.<key>` template MUST declare its placeholder contract somewhere accessible — either via a sibling `_placeholders` map, or via a generated allowlist from the codebase's call sites. Recommend: dedicated `src/services/guardrails.py` defines `KNOWN_NUDGES: dict[str, set[str]]` listing `{key: required_placeholders}`. `format()` calls validate against this at runtime.

---

## 4. Tool docstring strategy: strip-and-inject

The chosen approach (per the user's decision in this session): **strip the `Examples:` block from each tool docstring at bind time, and append the family-correct one from `tool_examples.<name>`**.

### 4.1 Why strip-and-inject (vs. format-neutral docstrings + system prompt examples)

| Strategy | Pros | Cons |
|---|---|---|
| **Strip-and-inject** (chosen) | Examples reach the model on every request via `tools[].function.description` (LangChain's stable surface). Zero risk of the system-prompt vs. tool-description mismatch. Per-tool granularity. | Adds bind-time mutation step. Requires every tool to have a `tool_examples.<name>` entry in `default.yaml`. |
| Format-neutral docstrings + system-prompt examples | Docstrings stay self-documenting, no mutation. | Examples in the system prompt are far from the tool definition the model sees. Harder to keep in sync as tools are added. Less effective teaching. |

The chosen approach centralizes wire-format teaching at the same surface where the model gets the tool schema, which is exactly where it makes the format decision.

### 4.2 Where it hooks in

LangChain's `bind_tools()` is the single place tool descriptions are serialized to the wire. Today:

```
src/agent.py:1825        self._strategic_llm.bind_tools(strategic_tools, **bind_kwargs)
src/agent.py:1828        self._tactical_llm.bind_tools(tactical_tools, **bind_kwargs)
src/api/persistent_session.py:456   self._llm.bind_tools(self.tools, **bind_kwargs)
src/services/auxiliary.py:462       self.llm.bind_tools(tools)
```

Add a new helper in `src/tools/registry.py`:

```python
# src/tools/registry.py — new function
def apply_guardrails_to_tools(tools: list, family: str) -> list:
    """Return a list of tools with docstrings rewritten for the model family.

    For each tool whose name appears in guardrails.tool_examples, replace
    the `Examples:` (or `Example:`) block in tool.description with the
    family-specific example block. Tools with no entry are returned as-is.
    """
    guardrails = resolve_guardrails(family=family)
    tool_examples = guardrails.get("tool_examples", {})
    out = []
    for t in tools:
        family_examples = tool_examples.get(t.name)
        if family_examples is None:
            out.append(t)
            continue
        new_description = _replace_examples_block(t.description, family_examples)
        # LangChain @tool returns a StructuredTool — description is mutable.
        new_t = t.copy(update={"description": new_description})
        out.append(new_t)
    return out
```

The four `bind_tools` call sites become:

```python
# Before
self._strategic_llm_with_tools = self._strategic_llm.bind_tools(strategic_tools, ...)

# After
strategic_tools_g = apply_guardrails_to_tools(strategic_tools, family=family_of(model))
self._strategic_llm_with_tools = self._strategic_llm.bind_tools(strategic_tools_g, ...)
```

### 4.3 Block detection regex

`_replace_examples_block(description, replacement)` finds the first occurrence of either:
- a line matching `^Example(?:s)?:\s*$` (heading style), through to the next blank line followed by a non-indented line, or end-of-string
- the legacy single-line `Example:` followed by inline content (rare in our codebase)

…and replaces the matched span with `replacement`. If no `Examples:` block is found, the replacement is appended (with a leading blank line). Implementation goes in `src/services/guardrails.py` alongside the loader; unit-tested with the docstrings already in `src/tools/`.

### 4.4 Docstring source-of-truth

After this change, the *Python docstring* in each tool function is no longer the model's source of truth for examples. The docstring should:
- Keep `Args:`, `Returns:`, `Raises:` blocks unchanged (these are stable across families).
- Keep a generic short prose summary (first paragraph).
- **Remove the existing `Examples:` block entirely** (the strip step would remove it anyway, but leaving it stale is misleading for human readers).

For human readers and the IDE: document in `CONTRIBUTING.md` (or a new section in this design doc, to be added when implementing) that **examples for tools live in `config/guardrails/default.yaml`**, with family-specific overrides in sibling files. The sweep of every tool docstring is the bulk of the migration work.

---

## 5. Per-family rollout

Every family currently in `config/model_config_matrix.yaml` gets a corresponding `config/guardrails/<family>.yaml`. Most are slim files that override only the keys whose surface form differs from `default.yaml`.

| Family | Wire format | Guardrails file | Override scope |
|---|---|---|---|
| `default` | OpenAI parens form | `default.yaml` | Full set — every `tool_examples` key, every `nudges` key |
| `gemma` | Canonical brace form (`<\|tool_call>call:fn{...}<tool_call\|>`) | `gemma.yaml` | All `tool_examples` keys (every tool needs brace form). Selected `nudges` keys (todo_action, budget_rewind, todo_list_footer, read_file_required_error) |
| `gpt-oss` | Harmony channel + JSON | `gpt_oss.yaml` | A few `tool_examples` keys with channel context. Most inherit from default. |
| `minimax` | OpenAI parens form (uses LangChain ChatOpenAI) | `minimax.yaml` | Inherits from default. Optional: M2.7 reasoning prefix in nudges if we find drift |
| `gpt-5` | OpenAI Responses API parens | `gpt_5.yaml` | Inherits from default. May tune some `nudges` if Responses-streaming surfaces drift |
| `codex` | Same as gpt-5 | `codex.yaml` | YAML-anchor inherits all from gpt_5 |
| `codex-spark` | Same as gpt-5 | `codex_spark.yaml` | YAML-anchor inherits all from gpt_5 |
| `claude-opus` | OpenAI parens form (LangChain ChatAnthropic) | (no file initially — uses `default.yaml` directly) | None — Anthropic tool-use parser is permissive |
| `gemini` | OpenAI parens form (function-calling) | (no file initially) | None |
| `deepseek` | OpenAI parens form | (no file initially) | None |
| `o-series` | OpenAI parens form | (no file initially) | None |

**Resolution behavior**: a family without a guardrails file falls back to `default.yaml`. So `claude-opus`, `gemini`, `deepseek`, `o-series` work with no work for slice 1.

Adding a new family later: drop a new file in `config/guardrails/`, add the `guardrails: file: ...` pointer in `model_config_matrix.yaml`, override only the keys that matter.

---

## 6. Migration plan — slice 1 (tools)

The tool docstring sweep is slice 1. Order chosen by per-request blast radius (LangChain re-serializes every docstring on every request).

### 6.1 Step-by-step

1. **Land the loader extension**
   - Allow-list `guardrails` in `_load_model_config_matrix_file` (1-line change).
   - Add `_load_guardrails_matrix(deployment_dir=None)` and `resolve_guardrails(model, deployment_dir=None)` to `src/core/loader.py`, mirroring the settings flow.
   - Unit tests: `tests/test_loader.py::test_resolve_guardrails_default_only`, `::test_resolve_guardrails_family_override`, `::test_resolve_guardrails_deep_merge_partial`.

2. **Add `src/services/guardrails.py`**
   - `apply_guardrails_to_tools(tools, family)` — strip-and-inject helper.
   - `_replace_examples_block(description, replacement)` — regex-based block replacement.
   - `format_nudge(key, family, **placeholders)` — keyed nudge resolution + `str.format` with placeholder validation against `KNOWN_NUDGES`.
   - `KNOWN_NUDGES: dict[str, set[str]]` — registry of every nudge key in use, with its required placeholder set. Read by validation.
   - Unit tests cover each function with mock guardrails dicts.

3. **Author `config/guardrails/default.yaml` and `config/guardrails/gemma.yaml`**
   - Default file enumerates every tool currently in `TOOL_REGISTRY`. For tools whose existing docstring already has an `Examples:` block, port the block verbatim. For tools without one (most core tools), write a short example block in OpenAI parens form.
   - Gemma file replaces every `tool_examples.<name>` with a brace-form variant. Most `nudges` inherit.

4. **Wire `apply_guardrails_to_tools` into the four bind sites**
   - `src/agent.py` (strategic + tactical), `src/api/persistent_session.py`, `src/services/auxiliary.py`.
   - Each site already knows the model name; resolve family via `family_of(model)`.

5. **Sweep tool docstrings** — remove every `Examples:` and `Example:` block from `src/tools/**/*.py`. The block lives in YAML now. Args/Returns blocks stay.
   - High-priority files: `src/tools/git/git_tools.py` (6 blocks), `src/tools/shell/shell_tools.py` (2 blocks), `src/tools/core/todo.py` (1 block).
   - Lower-priority files: every other `@tool` in `src/tools/` — full sweep.

6. **Author `config/guardrails/gpt_oss.yaml`, `minimax.yaml`, `gpt_5.yaml`, `codex.yaml`, `codex_spark.yaml`**
   - Most inherit; only the families with documented drift get overrides. Empty file or single-key file is fine — deep-merge handles it.

7. **End-to-end smoke**
   - Run `tests/manual_test_gemma_reasoning.py` (extended in this session) against gemma — scenario E should now show brace-form examples in the bound description.
   - Run a worker job on gpt-oss and on gpt-5 to confirm no regression for unaffected families.
   - Validate with the `tests/manual_test_*` scripts that exist for each model family.

### 6.2 Acceptance criteria

- All tool `Examples:` blocks live in `config/guardrails/*.yaml`. None remain in `src/tools/**/*.py` docstrings.
- Resolution chain works for all 11 families currently in the matrix.
- A worker job dispatched against any family receives bound tool descriptions whose `Examples:` blocks match the family's expected wire format.
- No regression in the diagnostic script or in test suites (`pytest tests/ -x`).

### 6.3 Deferred to later slices

- **Slice 2 — graph + manager nudges**: replace the 20+ hardcoded f-strings in `src/graph.py`, `src/core/phase.py`, `src/managers/todo.py`, `src/services/recall_store.py`, `src/services/knowledge_store.py`, `src/persistent_graph.py`, `src/services/auxiliary.py` with `format_nudge(key, family, ...)`.
- **Slice 3 — config templates and expert instructions**: `config/templates/instructions_*.md`, `config/experts/*/instructions.md`, `config/prompts/strategic_*.txt`. Many of these are already family-variant — the work is to remove parens-form examples and route them through `tool_examples` lookup at template-render time, OR to hand-port them to family-correct form per file (depending on whether the template engine has access to the guardrails dict at render time — TBD in slice 3 design).

---

## 7. Open questions

1. **Tool description copy semantics**: LangChain `StructuredTool.copy(update={"description": ...})` — confirm this is supported across our LangChain version and produces a tool the LLM provider serializes the same way. If not, fall back to mutating `t.description` directly with a deepcopy guard so we don't poison the source `@tool` registration.

2. **Per-expert override for guardrails**: today, experts can override `prompts:` and `instructions:` via a deployment-dir overlay (`<deployment>/model_config_matrix.yaml`). Should the same overlay accept `guardrails:` overrides? Cheap to support (the loader path already handles it). Recommend yes — same shape as other sections.

3. **Generated docstring stub** for human readability: after stripping `Examples:`, should the docstring grow a `(See guardrails matrix for tool-call examples.)` line? Adds noise but documents the indirection. Recommend yes — one line, only on tools that have an entry.

4. **CitationEngine and other auxiliary tool sets**: `src/services/auxiliary.py` binds tools for memory observers / curators. These run with a *different* model (sometimes a smaller auxiliary). The family for auxiliary is `family_of(self.aux_model)`, not the main model — already correct in spirit, just need to thread it through.

5. **Validation strictness on load**: warn vs. error when a guardrails file references a tool not in `TOOL_REGISTRY`. Tools come and go during refactors; warning is friendlier but lets stale entries linger. Recommend: warn in dev, error in CI via a dedicated `tests/test_guardrails_consistency.py`.

---

## 8. Out of scope for this design

- Disabling Gemma 4, patching vLLM upstream, or adding parser leniency.
- Reasoning-channel leak path (separate investigation, see `docs/issues/gemma_session_findings.md` Proposal C).
- Backend failover for gemma-4-moe (see Proposal E).
- Migrating expert instruction files (`config/experts/*/`) — slice 3.
- Migrating long-form prompts in `config/prompts/` (already family-variant; touch only if they contain parens-form tool examples — slice 3).

---

## 9. References

- `docs/issues/gemma_session_findings.md` — full session inventory and findings.
- `docs/issues/gemma_tool_call_parser_loop.md` — precipitating incident, root causes 1a/1b/2a/2b/2c, fixes shipped.
- `config/model_config_matrix.yaml` — existing matrix structure being extended.
- `src/core/loader.py:143-200` — existing matrix loader being extended.
- `src/core/model_registry.py:132` — `family_of()`, the resolution key.
- `src/tools/registry.py` — `TOOL_REGISTRY`, the tool-name source-of-truth.
- `src/agent.py:1825-1830`, `src/api/persistent_session.py:456`, `src/services/auxiliary.py:462` — the four `bind_tools` call sites.
