# Remove `workspace.md` vestiges (post-migration dead code)

**Status**: Backlog — deferred cleanup. Filed 2026-06-03.

## Context

`workspace.md` (the agent's old injected long-term-memory file) was removed in
favor of the project knowledge base (`kb_write`/`kb_search`) + the memory system
(RecallStore), both injected every LLM call. The prompt/config/code/doc sweep
landed 2026-06-01 (see the `project_workspace_md_removal` memory note and the
README "Knowledge & Memory" section).

During that sweep, several pieces of `workspace.md` plumbing were **deprecated in
place rather than deleted** — they are write-only-dead or unused, but removing
them cascades into test churn and the migration was already large. They were
left working (and harmless) with a clear paper trail so we can excise them in a
focused follow-up. This issue is that follow-up.

None of these are LLM-facing — the agent no longer sees or is told about
`workspace.md`. This is pure internal dead-code removal.

## Why it's deferred (not urgent)

- Everything listed is dead or no-ops at runtime; nothing here changes agent
  behavior.
- The removals touch the graph build signature, the agent state schema, the
  instruction matrix, and ~5 test files. That's a contained but real change set
  better done deliberately than bolted onto the migration PR.
- Leaving it costs only mild confusion for future readers, which the
  deprecation docstrings + this doc mitigate.

## Inventory of vestigial code to remove

> Line numbers drift — these were accurate around 2026-06-03; re-grep
> `workspace_memory` / `workspace_template` / `MemoryManager` when implementing.

### 1. `MemoryManager`

- `src/managers/memory.py` — the whole class (`MEMORY_FILE = "workspace.md"`),
  already marked `DEPRECATED` in its docstring.
- `src/managers/__init__.py` — the export and its docstring bullet.
- `src/graph.py` — instantiated as `memory_manager = MemoryManager(workspace)`
  in the graph-build function(s) and passed to `create_init_workspace_node(...)`,
  but `init_workspace` ignores it (it returns `{"workspace_memory": ""}` and does
  an audit-only step). Drop the instantiation and the `memory_manager` parameter
  from `create_init_workspace_node`.
- **Tests**: `tests/test_managers_memory.py` (delete — it tests the removed
  class), `tests/test_graph.py` (check for `MemoryManager`/`init_workspace`
  wiring assertions).

### 2. `workspace_memory` state key

- `src/core/state.py` — the `workspace_memory: str` field on
  `UniversalAgentState`, its `create_initial_state(...)` initializer, and the
  docstrings (already annotated "Legacy; unused"). Write-only; no reader exists.
- `src/graph.py` — `init_workspace` returns `{"workspace_memory": ""}`; drop it
  once the field is gone.
- **Tests**: `tests/test_state.py` (references `workspace_memory`).

### 3. `workspace_template` (config + loader + agent + graph)

This is the largest piece — the template system still resolves a
`workspace_template` entry that nothing consumes (see the
`# workspace_template is no longer used` comment in `src/graph.py`).

- **Matrix entries**: `config/model_config_matrix.yaml`
  (`workspace_template: workspace_template.md`),
  `config/experts/developer/model_config_matrix.yaml`, and the hardcoded default
  in `src/core/loader.py` (`"workspace_template": "workspace_template.md"`).
- **Template files (4)**: `config/templates/workspace_template.md`,
  `config/experts/developer/workspace_template.md`,
  `config/experts/scholar/workspace_template.md`,
  `config/experts/designer/workspace_template.md`.
- **Agent**: `src/agent.py` — `_load_workspace_template()` (dead method) and the
  `workspace_template=""` it threads into the graph build.
- **Graph**: `src/graph.py` — the `workspace_template` parameter on the
  graph-build function(s) and `create_init_workspace_node`, the pass-throughs,
  and the now-stale comment.
- **Docs**: `config/README.md` — the deprecated-template line and the
  `Instruction Matrix` entry list (drop `workspace_template`).
- **Tests**: `tests/test_instruction_matrix.py` (asserts on
  `workspace_template` resolution).

### 4. `phase_snapshot.py` file-copy/restore lists

- `src/core/phase_snapshot.py` — `files_to_copy` / `files_to_restore` still list
  `"workspace.md"` (gracefully skipped now that it's never written), plus the
  module docstring bullet. Drop `"workspace.md"` from both lists and the
  docstring.
- **Tests**: `tests/test_phase_snapshot.py` (asserts on the snapshot file set).

## Intentionally kept (do NOT remove in this issue)

- `src/tools/knowledge/workspace_converter.py` — the legacy `workspace.md` →
  knowledge-notes migration tool. Keep until we're confident no pre-migration
  job workspaces still need converting; revisit separately.
- `src/services/cloud_sync/base.py` `SYNC_IGNORE_PATTERNS` keeps `workspace.md`
  for backward compatibility (old job workspaces predating the migration still
  contain it and shouldn't sync to user clouds). Low cost; can stay.
- The accurate `# workspace.md no longer used` comments in `src/graph.py`.

## Suggested approach

Do it as one small PR, in dependency order so tests stay green between steps:

1. **State key** (#2) — remove `workspace_memory`; update `test_state.py` and the
   `init_workspace` return.
2. **MemoryManager** (#1) — delete class + export, drop the instantiation and the
   `create_init_workspace_node` parameter; delete `test_managers_memory.py`.
3. **`workspace_template`** (#3) — remove matrix entries + loader default + the 4
   files + `_load_workspace_template` + the graph param threading; update
   `test_instruction_matrix.py` and `config/README.md`.
4. **phase_snapshot** (#4) — trim the file lists; update `test_phase_snapshot.py`.
5. Run `ruff check src/ orchestrator/ tests/` + the affected test files, then the
   full suite.

## Acceptance criteria

- `rg -n 'workspace_memory|workspace_template|MemoryManager' src/ config/` returns
  only intentional hits (none — all four pieces gone).
- `create_init_workspace_node` no longer takes `memory_manager` /
  `workspace_template`.
- The 4 `workspace_template.md` files are deleted and no matrix/loader entry
  references them.
- `ruff check` clean; full test suite green (no skips that were green before).
- The only remaining `workspace.md` references in `src/` are
  `workspace_converter.py` and the `cloud_sync` ignore entry (both intentional).

## Effort & risk

~0.5 day. **Low risk** — all targets are dead/no-op at runtime; the work is
mostly deleting code and updating the tests that pin the removed surface. No
LLM-facing or behavioral change.

## Relationship to other work

Independent of the [AI-memory architecture research initiative]
(`project_ai_memory_research` memory note) — that effort refines the *live*
KB + RecallStore stack; this issue just removes the *dead* `workspace.md`
plumbing the live stack replaced. Either can land first.
