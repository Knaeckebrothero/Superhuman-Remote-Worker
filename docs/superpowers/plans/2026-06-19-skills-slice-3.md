# Agent Skills — Slice 3 (Expert Bindings + Migration) Implementation Plan

> **Status: ✅ Slice 3 COMPLETE & verified in the deployed k3d image (2026-06-19).** Executed inline on `develop` (8 commits). The skill→binding mechanism (Tasks 1–4) + both guide migrations (Tasks 5–6) landed; 127 unit tests green (incl. the existing enforcement/instruction-matrix regressions). **A mid-slice blocker surfaced at the Task-5 checkpoint** (the upfront research missed `todo_guide`'s per-family `gpt_oss` variant + 6 strategic-todo templates); per the user's call, `todo_guide` was pushed through accepting the **deliberate gpt_oss regression** (Task 6 expanded well beyond the plan — see As-built). Verified in the deployed orchestrator image under **both** flag states (flag-off prod-safety + flag-on menu filtering). See "## As-built notes".

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize the shipped `instruction_files` binding (`file → trigger → enforce`) to `skill → binding`, so an expert/base config can deterministically bind a skill (`before_tool` gate or `phase` injection) — then migrate the two existing enforced guides (`research_guide`, `todo_guide`) to bundled skills **without losing their enforcement**. After this slice there is **one artifact type** (`SKILL.md`); a binding decides how it activates.

**Architecture:** The binding machinery is already path-based and generic — `apply_instruction_enforcement` (the `before_tool` gate), `get_phase_instruction_files` (phase injection), `_deploy_instruction_files`, and `serialize_resolved_config` all key off an `InstructionFileEntry`'s workspace path. So Slice 3 adds an optional `skill:` field to that entry (mutually exclusive with `file:`) that resolves, via a new `path` property, to `skills/<skill>/SKILL.md` — the exact path Slice 2's `use_skill()` already materializes and records as read. Result: a `before_tool` gate on a bound skill is satisfied by **either** `read_file` **or** `use_skill`, with **zero net-new enforcement code**. The one subtlety the migration forces: a deterministic binding must work even when the model-invoked **catalog is off** (`SKILLS_DB_ENABLED=false`, i.e. prod), so bound-skill content rides the **flag-independent `instructions` blob channel** (frozen by `serialize_resolved_config`, deployed by `_deploy_instruction_files`), **not** the flag-gated catalog channel — and bound skills are filtered out of the catalog menu so they are never *also* offered as optional.

**Tech Stack:** Python 3.12 / FastAPI / asyncpg (orchestrator), LangChain `@tool` + LangGraph (agent), PostgreSQL, Helm. Cockpit untouched (no UI this slice).

**Design doc:** `docs/features/agent_skills.md` (Slice 3, locked decisions 5 + 6). **Builds on:** Slice 1 (`docs/superpowers/plans/2026-06-18-skills-slice-1.md`, shipped) + Slice 2 (`docs/superpowers/plans/2026-06-18-skills-slice-2.md`, shipped & k3d-verified). **Generalizes:** the instruction-file binding (`src/core/loader.py:InstructionFileEntry`, `src/tools/registry.py:apply_instruction_enforcement`, `src/tools/context.py:get_phase_instruction_files`, `src/graph.py` phase injection).

---

## Scope

**In scope (Slice 3):**
- `InstructionFileEntry` gains an optional `skill:` field (XOR with `file:`) + a `path` property; both config parse sites accept it.
- The three binding consumers (`before_tool` enforcement in `registry.py` + `context.py`; `phase` injection in `graph.py`) switch from `entry.file` to `entry.path` — behavior-identical for existing `file:` entries.
- Bound-skill content rides the flag-independent `instructions` blob channel: `serialize_resolved_config` freezes `skills/<skill>/SKILL.md`; `_deploy_instruction_files` (worker + session) writes it to the workspace.
- `filter_bound_skills(blob)` removes bound skills from the model-invoked catalog (menu + files); wired at both orchestrator `resolve_config` call sites.
- Migrate **`research_guide`** → bundled skill `config/skills/research-guide/`, scholar config re-pointed to `skill: research-guide` (clean: `phase:tactical`, no matrix entanglement). *(lands first — proves the mechanism)*
- Migrate **`todo_guide`** → bundled skill `config/skills/todo-guide/`, `defaults.yaml` + scholar re-pointed to `skill: todo-guide`; **remove** the bespoke instruction-matrix special-case (`loader.py` HARDCODED_DEFAULTS entry, `agent.py` matrix deploy block) and reconcile the hardcoded `todo_guide.md` references. *(lands second — higher blast radius)*
- Live k3d end-to-end verification, including the **flag-off prod-safety** check (todo gate still fires with `SKILLS_DB_ENABLED` unset).

**Explicitly out of scope (later slices):**
- DB / project bound skills. Bound skills are **bundled-only** this slice (the two guides are bundled). A `skill:` binding pointing at a non-bundled skill logs + degrades gracefully (Task 3 guard); full DB-bound support is later.
- Materializing a bound skill's *non-`SKILL.md`* files (references/scripts) through the binding channel — the two guides are single-file; bound multi-file skills are a later concern.
- Script execution / capability-grants `run_scripts` key (**Slice 4**).
- Menu budget / truncation / `semantic` auto-suggest (**later**).
- Any Cockpit change; any new MCP tool or DB migration.

## Design decisions (baked in — flag at review if you disagree)

1. **Binding schema = extend `InstructionFileEntry` with `skill:` (XOR `file:`).** No separate `skill_bindings` field, no new dataclass. `path` resolves a `skill:` entry to `skills/<skill>/SKILL.md` and a `file:` entry to `file` verbatim. This reuses **all** existing enforcement/phase/deploy/serialize machinery; net-new binding code is the `path` property + branch points. (Locked-decision 5: activation is a *binding*, not a new artifact.)
2. **Bound skills ride the flag-independent `instructions` channel — THE load-bearing decision.** `todo_guide` is bound in `defaults.yaml`, so its gate is active for **every agent including prod, where `SKILLS_DB_ENABLED` is off** and the catalog gather returns `{}`. If a bound skill's file only materialized via the catalog, the gate would block `next_phase_todos` forever in prod. So `serialize_resolved_config` freezes a bound skill's `SKILL.md` into `blob["instructions"][<skill-name>]` (read straight from `config/skills/<skill>/SKILL.md` on disk, orchestrator-side), and `_deploy_instruction_files` writes it to `skills/<skill>/SKILL.md`. This is exactly today's resilient path for `research_guide.md` (frozen + redeployed for resumed/VM jobs), now keyed by skill name. Independent of the catalog flag. (Locked-decision 6: don't lose enforcement.)
3. **Bound skills are filtered out of the model-invoked catalog** (`filter_bound_skills` strips them from `menu` + `files`). A deterministically-bound skill is mandatory or auto-injected — listing it as "you *may* load this" contradicts the binding and risks double-delivery. The model-invoked menu lists only *optional* skills. (Filtering keys off `blob["agent"]["instruction_files"]`, which carries the resolved `skill:` names after merge.)
4. **The `todo_guide` matrix special-case is removed, not left as a duplicate.** Migrating it is the whole point of "instruction documents *are* skills" — leaving the matrix deploy writing a stale `todo_guide.md` alongside `skills/todo-guide/SKILL.md` is two sources of truth. So Task 6 deletes `config/templates/todo_guide.md`, drops the `"todo_guide"` HARDCODED_DEFAULTS entry, and removes the `agent.py` matrix deploy block + the `entry.file == "todo_guide.md"` skip.
5. **Sequenced within the slice: `research_guide` (Task 5) before `todo_guide` (Task 6).** `research_guide` has zero matrix entanglement and only touches one expert, so it proves the end-to-end mechanism at low risk. `todo_guide` (prod-wide gate + matrix cleanup) follows. Inline-with-checkpoints execution gives a natural halt point after Task 5.
6. **`use_skill` already satisfies `before_tool` gates.** Slice 2's `use_skill("X")` calls `context.record_file_read("skills/X/SKILL.md")` — identical to what `read_file` records — so the migrated todo gate opens whether the agent uses `read_file` or `use_skill`. No change needed to `use_skill`.

## File structure

**Create:**
- `config/skills/research-guide/SKILL.md` — bundled skill (frontmatter + the existing `research_guide.md` body).
- `config/skills/todo-guide/SKILL.md` — bundled skill (frontmatter + the existing `todo_guide.md` body).
- `tests/test_skill_bindings.py` — `InstructionFileEntry` (`skill`/`path`/XOR), parse, enforcement + phase consumers using `entry.path`, serialize round-trip of a bound skill, migration-config assertions.

**Modify:**
- `src/core/loader.py` — `InstructionFileEntry` (add `skill`, `path`, `__post_init__`); two parse sites (`~1944`, `~2146`); `serialize_resolved_config` (`~4109-4124`) branches on `skill:`; drop `"todo_guide"` from `InstructionMatrixResolver.HARDCODED_DEFAULTS` (`~970`, Task 6).
- `src/tools/registry.py` — `apply_instruction_enforcement` `entry.file` → `entry.path` (`~662`).
- `src/tools/context.py` — `get_enforcement_files` `entry.file` → `entry.path` (`~563`).
- `src/graph.py` — phase injection `entry.file` → `entry.path` (`~1118-1129`).
- `src/agent.py` — `_deploy_instruction_files` branches on `skill:` (`~2095`); remove the `todo_guide` matrix deploy block + skip + now-unused imports (`~2073-2098`, Task 6).
- `src/api/persistent_session.py` — `_deploy_instruction_files` branches on `skill:` (`~442`).
- `src/core/skill_resolution.py` — add `filter_bound_skills(blob)`.
- `orchestrator/main.py` — call `filter_bound_skills` after both `resolve_config` sites (`~1611`, `~1040`).
- `src/tools/core/todo.py` — reconcile the dead `todo_guide.md` fallback reference (`~107`, Task 6).
- `config/defaults.yaml` — `todo_guide.md` entry → `skill: todo-guide` (`~166-169`, Task 6).
- `config/experts/scholar/config.yaml` — both entries → `skill:` form (`~35-41`).
- `docs/features/agent_skills.md`, this plan — flip Slice-3 status + as-built notes (Task 8).

**Delete (Task 5/6, after grep):**
- `config/experts/scholar/research_guide.md`, `config/templates/todo_guide.md` — content moves into the bundled skills (single source of truth).

---

## Task 1: `InstructionFileEntry` gains a `skill` binding

The net-new core: an instruction entry can name a `skill:` instead of a `file:`, resolving to the skill's workspace `SKILL.md`. Pure dataclass + parse; fully unit-tested.

**Files:**
- Modify: `src/core/loader.py` (`InstructionFileEntry` ~975-1002; two parse sites ~1944-1951 and ~2146-2153)
- Test: `tests/test_skill_bindings.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_skill_bindings.py
import pytest

from src.core.loader import InstructionFileEntry, load_agent_config_from_dict


def test_file_entry_path_is_the_file():
    e = InstructionFileEntry(trigger="before_tool:next_phase_todos", file="todo_guide.md")
    assert e.path == "todo_guide.md"
    assert e.trigger_type == "before_tool"
    assert e.trigger_target == "next_phase_todos"


def test_skill_entry_path_is_skill_md():
    e = InstructionFileEntry(trigger="phase:tactical", skill="research-guide", enforce=False)
    assert e.path == "skills/research-guide/SKILL.md"
    assert e.trigger_type == "phase"
    assert e.trigger_target == "tactical"


def test_entry_requires_exactly_one_of_file_or_skill():
    with pytest.raises(ValueError):
        InstructionFileEntry(trigger="phase:tactical")  # neither
    with pytest.raises(ValueError):
        InstructionFileEntry(trigger="phase:tactical", file="x.md", skill="x")  # both


def test_parse_skill_binding_from_config():
    cfg = load_agent_config_from_dict(
        {
            "agent_id": "t",
            "display_name": "T",
            "instruction_files": [
                {"skill": "research-guide", "trigger": "phase:tactical", "enforce": False},
                {"file": "todo_guide.md", "trigger": "before_tool:next_phase_todos"},
            ],
        }
    )
    by_path = {e.path: e for e in cfg.instruction_files}
    assert "skills/research-guide/SKILL.md" in by_path
    assert by_path["skills/research-guide/SKILL.md"].skill == "research-guide"
    assert by_path["skills/research-guide/SKILL.md"].enforce is False
    assert by_path["todo_guide.md"].file == "todo_guide.md"
    assert by_path["todo_guide.md"].enforce is True  # default
```

- [x] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_skill_bindings.py -v`
Expected: FAIL — `InstructionFileEntry` rejects the `skill` kwarg (TypeError) / `.path` missing.

- [x] **Step 3: Extend `InstructionFileEntry`** (`src/core/loader.py`, replace the dataclass at ~975-1002)

```python
@dataclass
class InstructionFileEntry:
    """An instruction file (or bound skill) with a trigger condition.

    Defines when and how a Layer-3 artifact is delivered to the agent. The
    artifact is either a literal instruction ``file`` (workspace-relative path)
    OR a bundled ``skill`` (resolved to ``skills/<skill>/SKILL.md``) — exactly
    one. See docs/features/agent_skills.md (Slice 3).

    Attributes:
        trigger: Trigger condition string:
            - "before_tool:<tool_name>" — fires when the named tool is called
            - "phase:strategic" / "phase:tactical" — fires on phase transition
        file: Workspace-relative path (e.g. "todo_guide.md"). XOR ``skill``.
        skill: Bundled skill name (e.g. "research-guide"). XOR ``file``;
               resolves to ``skills/<skill>/SKILL.md`` via ``path``.
        enforce: If True, tool rejects until agent reads the artifact (passive).
                 If False, system injects content automatically (active).
    """

    trigger: str
    file: Optional[str] = None
    skill: Optional[str] = None
    enforce: bool = True

    def __post_init__(self) -> None:
        if bool(self.file) == bool(self.skill):
            raise ValueError(
                "InstructionFileEntry requires exactly one of 'file' or 'skill' "
                f"(got file={self.file!r}, skill={self.skill!r})"
            )

    @property
    def path(self) -> str:
        """The workspace path this binding resolves to: a skill's SKILL.md when
        bound to a skill, else the literal instruction-file path."""
        if self.skill:
            return f"skills/{self.skill}/SKILL.md"
        return self.file or ""

    @property
    def trigger_type(self) -> str:
        """Extract trigger type: 'before_tool' or 'phase'."""
        return self.trigger.split(":")[0]

    @property
    def trigger_target(self) -> str:
        """Extract trigger target: tool name or phase name."""
        parts = self.trigger.split(":", 1)
        return parts[1] if len(parts) > 1 else ""
```

- [x] **Step 4: Update BOTH parse sites** (`src/core/loader.py` ~1944-1951 and ~2146-2153 — identical edits)

```python
    instruction_files = [
        InstructionFileEntry(
            trigger=entry["trigger"],
            file=entry.get("file"),
            skill=entry.get("skill"),
            enforce=entry.get("enforce", True),
        )
        for entry in instruction_files_data
    ]
```

> `Optional` is already imported in `loader.py`. Field order now puts the only required field (`trigger`) first; a grep confirmed every construction is keyword-based (the two sites above), so reordering is safe.

- [x] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_skill_bindings.py -v`
Expected: PASS (4 cases).

- [x] **Step 6: Commit**

```bash
git add src/core/loader.py tests/test_skill_bindings.py
git commit -m "feat(skills): InstructionFileEntry gains a skill: binding (XOR file:, .path resolver)"
```

---

## Task 2: Binding consumers resolve via `entry.path`

Switch the three deterministic-binding consumers from `entry.file` to `entry.path` so a `skill:` entry enforces / injects at `skills/<skill>/SKILL.md`. Behavior-identical for existing `file:` entries (`path == file`).

**Files:**
- Modify: `src/tools/context.py` (`get_enforcement_files` ~563)
- Modify: `src/tools/registry.py` (`apply_instruction_enforcement` ~662)
- Modify: `src/graph.py` (phase injection ~1118-1129)
- Test: `tests/test_skill_bindings.py`

- [x] **Step 1: Write the failing tests** (append to `tests/test_skill_bindings.py`)

```python
from types import SimpleNamespace

from tests._fs_backend import FilesystemTestBackend
from src.core.workspace import WorkspaceManager
from src.tools.context import ToolContext
from src.tools.registry import apply_instruction_enforcement


def _ctx(tmp_path, entries):
    ws = WorkspaceManager(job_id="t", backend=FilesystemTestBackend(tmp_path))
    ctx = ToolContext(workspace_manager=ws)
    ctx._instruction_files = entries
    ctx._llm_config = None
    return ctx


def test_before_tool_gate_targets_skill_path(tmp_path):
    ctx = _ctx(
        tmp_path,
        [InstructionFileEntry(trigger="before_tool:next_phase_todos", skill="todo-guide")],
    )
    assert ctx.get_enforcement_files("next_phase_todos") == ["skills/todo-guide/SKILL.md"]
    # gate closed until the skill path is read
    assert ctx.check_tool_enforcement("next_phase_todos") is not None
    ctx.record_file_read("skills/todo-guide/SKILL.md")  # what use_skill / read_file record
    assert ctx.check_tool_enforcement("next_phase_todos") is None


def test_phase_binding_targets_skill_path(tmp_path):
    ctx = _ctx(
        tmp_path,
        [InstructionFileEntry(trigger="phase:tactical", skill="research-guide", enforce=False)],
    )
    entries = ctx.get_phase_instruction_files("tactical")
    assert len(entries) == 1 and entries[0].path == "skills/research-guide/SKILL.md"
    assert ctx.get_phase_instruction_files("strategic") == []


def test_apply_enforcement_wrapper_uses_skill_path(tmp_path):
    ctx = _ctx(
        tmp_path,
        [InstructionFileEntry(trigger="before_tool:next_phase_todos", skill="todo-guide")],
    )
    calls = []
    tool = SimpleNamespace(
        name="next_phase_todos", func=lambda *a, **k: (calls.append(1), "OK")[1]
    )
    apply_instruction_enforcement([tool], ctx)
    blocked = tool.func()
    assert "skills/todo-guide/SKILL.md" in blocked and calls == []  # nudged, not run
    ctx.record_file_read("skills/todo-guide/SKILL.md")
    assert tool.func() == "OK" and calls == [1]  # gate opened
```

- [x] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_skill_bindings.py -k "skill_path or wrapper" -v`
Expected: FAIL — enforcement/phase still read `entry.file` (None for skill entries → `["None"]` / wrong path).

- [x] **Step 3: `src/tools/context.py`** — `get_enforcement_files` (~563): `required.append(entry.file)` → `required.append(entry.path)`.

- [x] **Step 4: `src/tools/registry.py`** — `apply_instruction_enforcement` (~662): `enforcement_map.setdefault(entry.trigger_target, []).append(entry.file)` → `...append(entry.path)`.

- [x] **Step 5: `src/graph.py`** — phase injection (~1116-1130), replace the four `entry.file` reads with `entry.path`:

```python
                    for entry in phase_entries:
                        try:
                            instr_content = workspace_manager.read_file(entry.path)
                            instr_ai, instr_tool = create_instruction_tool_messages(
                                entry.path, instr_content
                            )
                            target_messages.append(instr_ai)
                            target_messages.append(instr_tool)
                            logger.debug(
                                f"[{job_id}] Injected instruction file: {entry.path}"
                            )
                        except FileNotFoundError:
                            logger.warning(
                                f"[{job_id}] Phase instruction file not found: {entry.path}"
                            )
```

- [x] **Step 6: Run to verify pass + no regression**

Run: `python -m pytest tests/test_skill_bindings.py -v`
Expected: PASS. (Existing `file:` entries: `path == file`, so unchanged.)

- [x] **Step 7: Commit**

```bash
git add src/tools/context.py src/tools/registry.py src/graph.py tests/test_skill_bindings.py
git commit -m "feat(skills): before_tool + phase bindings resolve via entry.path (skill-aware)"
```

---

## Task 3: Bound-skill content rides the flag-independent `instructions` channel

A bound skill's `SKILL.md` must reach the workspace regardless of `SKILLS_DB_ENABLED` (decision 2). Freeze it in `serialize_resolved_config` (read from `config/skills/<skill>/SKILL.md` on disk) and deploy it in `_deploy_instruction_files` (worker + session). Keyed by skill name to avoid the `SKILL.md`-stem collision.

**Files:**
- Modify: `src/core/loader.py` (`serialize_resolved_config` ~4109-4124)
- Modify: `src/agent.py` (`_deploy_instruction_files` ~2095-2115)
- Modify: `src/api/persistent_session.py` (`_deploy_instruction_files` ~442-459)
- Test: `tests/test_skill_bindings.py`

- [x] **Step 1: Write the failing test** (append; uses the existing bundled `hello-skill` so it has no dependency on Tasks 5/6)

```python
from src.core.loader import serialize_resolved_config


def test_serialize_freezes_bound_skill_md():
    cfg = load_agent_config_from_dict(
        {
            "agent_id": "t",
            "display_name": "T",
            "instruction_files": [
                {"skill": "hello-skill", "trigger": "phase:tactical", "enforce": False}
            ],
        }
    )
    blob = serialize_resolved_config(cfg)
    # Frozen under the skill name (not "SKILL"), independent of the catalog flag.
    assert "hello-skill" in blob["instructions"]
    assert "Hello Skill" in blob["instructions"]["hello-skill"]
    assert "SKILL" not in blob["instructions"]  # no stem collision
```

- [x] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_skill_bindings.py -k freezes -v`
Expected: FAIL — `serialize_resolved_config` only handles `file:` entries (`Path(entry.file).stem` → `Path(None)` raises / skipped), no `hello-skill` key.

- [x] **Step 3: `src/core/loader.py`** — `serialize_resolved_config` instruction loop (~4109-4124), branch on `skill:`:

```python
    # Also resolve custom instruction files from config.instruction_files
    # (e.g. research_guide.md) AND bound skills (skill:) — these aren't in the
    # matrix but need to survive serialization so resumed/VM jobs (and the
    # deterministic binding when the skills CATALOG is off) can materialize them.
    if config.instruction_files:
        templates_dir = get_project_root() / "config" / "templates"
        file_resolver = FileResolver(
            deployment_dir=config._deployment_dir,
            framework_dir=templates_dir,
        )
        skills_root = get_project_root() / "config" / "skills"
        for entry in config.instruction_files:
            if entry.skill:
                # Bound skill: freeze SKILL.md keyed by skill name (NOT its "SKILL"
                # stem). Flag-independent — does not depend on the catalog gather.
                if entry.skill not in instructions:
                    skill_md = skills_root / entry.skill / "SKILL.md"
                    try:
                        instructions[entry.skill] = skill_md.read_text(encoding="utf-8")
                    except OSError:
                        pass  # non-bundled bound skill (out of scope this slice)
                continue
            basename = Path(entry.file).stem
            if basename not in instructions:
                try:
                    instructions[basename] = file_resolver.load(Path(entry.file).name)
                except FileNotFoundError:
                    pass
```

- [x] **Step 4: `src/agent.py`** — `_deploy_instruction_files` loop (~2095, first thing inside `for entry in self.config.instruction_files:`), add the skill branch before the existing file logic:

```python
            for entry in self.config.instruction_files:
                try:
                    if entry.skill:
                        # Bound skill: content from the (flag-independent) instructions
                        # channel, written to skills/<skill>/SKILL.md. The catalog
                        # materialization path (Slice 2) is filtered out for bound
                        # skills, so this is the single delivery path.
                        content = resolved_instructions.get(entry.skill)
                        if not content:
                            logger.warning(
                                f"Bound skill content missing from blob: {entry.skill}"
                            )
                            continue
                        content = render_instruction_content(content, loaded_tool_names)
                        parent_dir = str(Path(entry.path).parent)
                        if parent_dir and parent_dir != ".":
                            self._workspace_manager.backend.mkdir(parent_dir)
                        self._workspace_manager.write_file(entry.path, content)
                        logger.debug(f"Deployed bound skill to workspace: {entry.path}")
                        continue
                    # Skip todo_guide.md — already handled above via matrix
                    if entry.file == "todo_guide.md":
                        continue
                    # ... existing file logic unchanged ...
```

> The `entry.file == "todo_guide.md"` skip stays for now; Task 6 removes it once `todo_guide` becomes a `skill:` entry.

- [x] **Step 5: `src/api/persistent_session.py`** — `_deploy_instruction_files` loop (~442), same skill branch (this method has no `loaded_tool_names`, mirror its existing `render_instruction_content(content, [])`):

```python
        for entry in self.config.instruction_files:
            try:
                if entry.skill:
                    target_path = self.workspace_manager.get_path(entry.path)
                    if target_path.exists():
                        continue  # don't overwrite on session resume
                    content = self.config.extra.get("_resolved_instructions", {}).get(
                        entry.skill
                    )
                    if not content:
                        logger.warning(
                            f"Bound skill content missing from blob: {entry.skill}"
                        )
                        continue
                    content = render_instruction_content(content, [])
                    parent_dir = str(Path(entry.path).parent)
                    if parent_dir and parent_dir != ".":
                        self.workspace_manager.backend.mkdir(parent_dir)
                    self.workspace_manager.write_file(entry.path, content)
                    logger.debug(f"Deployed bound skill to workspace: {entry.path}")
                    continue
                # Skip if already present (don't overwrite on session resume)
                target_path = self.workspace_manager.get_path(entry.file)
                # ... existing file logic unchanged ...
```

- [x] **Step 6: Run + smoke-check imports**

```bash
python -m pytest tests/test_skill_bindings.py -v
python -c "import src.agent, src.api.persistent_session, src.core.loader"
```
Expected: tests PASS; no import error.

> Deploy is verified live in Task 7 (the Slice-2 precedent: unit-test the pure/serialize half, prove the workspace write on k3d). The serialize half is unit-tested above.

- [x] **Step 7: Commit**

```bash
git add src/core/loader.py src/agent.py src/api/persistent_session.py tests/test_skill_bindings.py
git commit -m "feat(skills): bound skills ride the flag-independent instructions channel"
```

---

## Task 4: Filter bound skills out of the model-invoked catalog

A bound skill is delivered deterministically, so it must not *also* appear in the `use_skill` menu. `filter_bound_skills` strips bound names from `menu` + `files`; wired after both `resolve_config` calls.

**Files:**
- Modify: `src/core/skill_resolution.py` (add `filter_bound_skills`)
- Modify: `orchestrator/main.py` (call it after both `resolve_config` sites ~1611, ~1040)
- Test: `tests/test_skill_resolution.py`

- [x] **Step 1: Write the failing tests** (append to `tests/test_skill_resolution.py`)

```python
from src.core.skill_resolution import filter_bound_skills


def test_filter_removes_bound_skill_from_menu_and_files():
    blob = {
        "agent": {
            "instruction_files": [
                {"skill": "todo-guide", "trigger": "before_tool:next_phase_todos"},
                {"file": "x.md", "trigger": "phase:tactical"},
            ]
        },
        "skills": {
            "menu": [{"name": "todo-guide"}, {"name": "free-skill"}],
            "files": {"todo-guide": {"SKILL.md": "x"}, "free-skill": {"SKILL.md": "y"}},
        },
    }
    filter_bound_skills(blob)
    assert [m["name"] for m in blob["skills"]["menu"]] == ["free-skill"]
    assert set(blob["skills"]["files"]) == {"free-skill"}


def test_filter_noop_without_skills_or_bindings():
    assert filter_bound_skills({"agent": {}}) == {"agent": {}}
    blob = {"agent": {"instruction_files": []}, "skills": {"menu": [{"name": "a"}], "files": {}}}
    filter_bound_skills(blob)
    assert [m["name"] for m in blob["skills"]["menu"]] == ["a"]  # nothing bound → unchanged
```

- [x] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_skill_resolution.py -k filter -v`
Expected: FAIL — `ImportError: cannot import name 'filter_bound_skills'`.

- [x] **Step 3: Add `filter_bound_skills`** (`src/core/skill_resolution.py`, after `skill_files_to_workspace`)

```python
def filter_bound_skills(blob: dict[str, Any]) -> dict[str, Any]:
    """Remove skills delivered via deterministic bindings (instruction_files
    ``skill:`` entries) from the model-invoked catalog (menu + files).

    Bound skills are materialized through the flag-independent instructions
    channel (serialize/_deploy_instruction_files), so they must not also be
    offered as optional ``use_skill`` entries. Mutates ``blob`` in place and
    returns it; no-op when there is no skills payload or no bound skills.
    """
    skills = blob.get("skills")
    if not skills:
        return blob
    bound = {
        e.get("skill")
        for e in (blob.get("agent", {}).get("instruction_files") or [])
        if e.get("skill")
    }
    if not bound:
        return blob
    if skills.get("menu"):
        skills["menu"] = [m for m in skills["menu"] if m.get("name") not in bound]
    if skills.get("files"):
        skills["files"] = {n: f for n, f in skills["files"].items() if n not in bound}
    return blob
```

- [x] **Step 4: Wire at the job-dispatch site** (`orchestrator/main.py` ~1611, immediately after `_resolved = resolve_config(...)`)

```python
                from src.core.skill_resolution import filter_bound_skills

                filter_bound_skills(_resolved)
```

- [x] **Step 5: Wire at the session site** (`orchestrator/main.py` ~1040, immediately after `resolved = resolve_config(...)`)

```python
        from src.core.skill_resolution import filter_bound_skills

        filter_bound_skills(resolved)
```

- [x] **Step 6: Run + smoke-check the orchestrator import**

```bash
python -m pytest tests/test_skill_resolution.py -v
python -c "import orchestrator.main"
```
Expected: tests PASS; no import error.

- [x] **Step 7: Commit**

```bash
git add src/core/skill_resolution.py orchestrator/main.py tests/test_skill_resolution.py
git commit -m "feat(skills): filter deterministically-bound skills out of the use_skill catalog"
```

---

## Task 5: Migrate `research_guide` → bundled skill (the clean proof)

`research_guide` is scholar-only, `phase:tactical`, delivered purely via the instruction loop — no matrix entanglement. Migrating it proves the whole mechanism end-to-end at low risk.

**Files:**
- Create: `config/skills/research-guide/SKILL.md`
- Modify: `config/experts/scholar/config.yaml` (~39-41)
- Delete: `config/experts/scholar/research_guide.md` (after grep)
- Test: `tests/test_skill_bindings.py`

- [x] **Step 1: Write the failing test** (append to `tests/test_skill_bindings.py`)

```python
from pathlib import Path as _P

from src.core.skill_format import parse_skill_md, skill_identity


def test_research_guide_skill_exists_and_parses():
    md = (_P("config/skills/research-guide/SKILL.md")).read_text(encoding="utf-8")
    fm, body = parse_skill_md(md)
    name, desc = skill_identity(fm)
    assert name == "research-guide"
    assert "Research Workflow" in body  # the migrated body survived


def test_scholar_binds_research_guide_as_skill():
    import yaml

    cfg = yaml.safe_load(_P("config/experts/scholar/config.yaml").read_text())
    entries = cfg["instruction_files"]
    research = [e for e in entries if e.get("skill") == "research-guide"]
    assert len(research) == 1
    assert research[0]["trigger"] == "phase:tactical"
    assert research[0]["enforce"] is False
    assert not any(e.get("file") == "research_guide.md" for e in entries)  # old ref gone
```

- [x] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_skill_bindings.py -k research -v`
Expected: FAIL — `config/skills/research-guide/SKILL.md` does not exist.

- [x] **Step 3: Create `config/skills/research-guide/SKILL.md`** (frontmatter + the existing `research_guide.md` body verbatim)

```markdown
---
name: research-guide
description: Use during a tactical research or exploration phase — methodology for framing a question, searching broadly, citing every claim, and persisting findings.
display_name: Research Guide
icon: science
color: "#f9e2af"
tags:
  - research
  - methodology
---

# Research Guide

You are entering a tactical phase. This guide covers research methodology for your exploration work.

## Research Workflow

1. **Define the question** — What specific thing are you trying to learn? Write it down before searching.
2. **Search broadly** — Use `web_search` with varied queries. Don't stop at the first result.
3. **Save sources** — Use `cite_web` or `cite_document` for every factual claim. No citation = no claim.
4. **Persist findings** — Write results to workspace files immediately. Don't hold findings only in memory.
5. **Synthesize** — After gathering sources, write a summary connecting findings to the task.

## Tool Usage

### Web Research
- `web_search` — broad keyword/topic search; vary your queries
- `extract_webpage` — fetch full content from a promising result
- `research_topic` — automated multi-source research workflow

### Academic Sources
- `search_papers` — find academic papers (arxiv or Semantic Scholar)
- `download_paper` — save the paper PDF to `documents/`
- `get_paper_info` — metadata lookup

### Document Analysis
- `read_file` — read a document by path; supports text lines (offset/limit) and document pages (page_start/page_end)
- `get_document_info` — page count and structure preview before opening a large document

### Citations
Every factual or technical claim must cite its source:
- `cite_web` — verify a quoted claim against a URL
- `cite_document` — verify a quoted claim against a workspace document by page or section

See each tool's description for the exact wire format and arguments.

## Output Conventions

- Save idea artifacts to `output/ideas/` — one file per idea with evidence and proposal.
- Save experiment results to `output/experiments/` — include methodology and findings.
- Save raw research notes to `notes/` — these are working files, not deliverables.
- Reference material goes in `reference/` — domain knowledge for future phases.

## Anti-Patterns

- Don't research without a question. "Learn about X" is not a research task. "What are the top 3 approaches to X and their tradeoffs?" is.
- Don't cite from memory. If you can't point to a source, it's not a fact — it's an assumption.
- Don't hold findings in context only. Context gets compacted. Write findings to files.
- Don't deep-dive on the first interesting result. Breadth first, depth second.
```

- [x] **Step 4: Re-point scholar's binding** (`config/experts/scholar/config.yaml` ~39-41)

```yaml
  - skill: research-guide
    trigger: phase:tactical
    enforce: false  # Active: injected automatically at tactical phase start
```

- [x] **Step 5: Grep for stragglers, then delete the old file**

```bash
grep -rn "research_guide" --include=*.py --include=*.yaml --include=*.md . | grep -v docs/
git rm config/experts/scholar/research_guide.md
```
Expected: no remaining functional reference to `research_guide.md` (only this plan / design docs may mention it). If a test references it, update it.

- [x] **Step 6: Run to verify pass + scholar config loads**

```bash
python -m pytest tests/test_skill_bindings.py -k research -v
python -c "from src.core.loader import load_config; c=load_config('experts/scholar'); print([(e.skill or e.file, e.trigger) for e in c.instruction_files])"
```
Expected: tests PASS; printed bindings show `('research-guide', 'phase:tactical')` and `('todo_guide.md', 'before_tool:next_phase_todos')` (todo still file-based until Task 6).

> If `load_config` is not the exact public loader name, use the same entrypoint the existing config tests use (grep `tests/ -l "load_config\|load_agent_config"`). The assertion is the point; the call is a convenience smoke-check.

- [x] **Step 7: Commit**

```bash
git add config/skills/research-guide/SKILL.md config/experts/scholar/config.yaml tests/test_skill_bindings.py
git commit -m "feat(skills): migrate research_guide to bundled skill (phase:tactical binding)"
```

> **CHECKPOINT (decision 5):** the mechanism is now proven by a real migrated skill. Safe halt point before the higher-blast-radius `todo_guide` migration.

---

## Task 6: Migrate `todo_guide` → bundled skill + remove the matrix special-case

`todo_guide` is bound in `defaults.yaml` (every agent) and entangled with the instruction-matrix special-case + three hardcoded references. Migrate it to a skill and remove the special-case so there's one artifact type and no stale `todo_guide.md`.

**Files:**
- Create: `config/skills/todo-guide/SKILL.md`
- Modify: `config/defaults.yaml` (~166-169), `config/experts/scholar/config.yaml` (~36-38)
- Modify: `src/core/loader.py` (drop `"todo_guide"` from `HARDCODED_DEFAULTS` ~970)
- Modify: `src/agent.py` (remove matrix deploy block ~2073-2085 + the `entry.file == "todo_guide.md"` skip ~2097-2099 + now-unused imports)
- Modify: `src/tools/core/todo.py` (reconcile the dead fallback ~104-122)
- Delete: `config/templates/todo_guide.md` (after grep)
- Test: `tests/test_skill_bindings.py`

- [x] **Step 1: Write the failing test** (append to `tests/test_skill_bindings.py`)

```python
def test_todo_guide_skill_exists_and_parses():
    md = (_P("config/skills/todo-guide/SKILL.md")).read_text(encoding="utf-8")
    fm, body = parse_skill_md(md)
    name, _desc = skill_identity(fm)
    assert name == "todo-guide"
    assert "Short Phases" in body  # the migrated body survived


def test_defaults_bind_todo_guide_as_skill():
    import yaml

    cfg = yaml.safe_load(_P("config/defaults.yaml").read_text())
    entries = cfg["instruction_files"]
    todo = [e for e in entries if e.get("skill") == "todo-guide"]
    assert len(todo) == 1
    assert todo[0]["trigger"] == "before_tool:next_phase_todos"
    assert todo[0]["enforce"] is True
    assert not any(e.get("file") == "todo_guide.md" for e in entries)


def test_todo_guide_dropped_from_instruction_matrix():
    from src.core.loader import InstructionMatrixResolver

    assert "todo_guide" not in InstructionMatrixResolver.HARDCODED_DEFAULTS
```

- [x] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_skill_bindings.py -k "todo_guide" -v`
Expected: FAIL — skill missing; `defaults.yaml` still `file:`; matrix still has `todo_guide`.

- [x] **Step 3: Create `config/skills/todo-guide/SKILL.md`** (frontmatter + the existing 298-line body verbatim)

```bash
mkdir -p config/skills/todo-guide
{
  printf -- '---\nname: todo-guide\ndescription: Use before planning a phase or calling next_phase_todos — how to craft focused, well-scoped, verifiable todos.\ndisplay_name: Todo Guide\nicon: checklist\ncolor: "#89b4fa"\ntags:\n  - planning\n  - todos\n---\n\n'
  cat config/templates/todo_guide.md
} > config/skills/todo-guide/SKILL.md
```

> Preserves the body byte-for-byte (incl. any Jinja `{% ... %}` markers — still rendered by `render_instruction_content` at deploy). Verify the frontmatter delimiter sits cleanly above the existing `# Todo Crafting Guide` H1.

- [x] **Step 4: Re-point both bindings**

`config/defaults.yaml` (~166-169):
```yaml
instruction_files:
  - skill: todo-guide
    trigger: before_tool:next_phase_todos
    enforce: true
```

`config/experts/scholar/config.yaml` (~36-38, the first entry):
```yaml
  - skill: todo-guide
    trigger: before_tool:next_phase_todos
    enforce: true
```

- [x] **Step 5: Drop `todo_guide` from the instruction matrix** (`src/core/loader.py` ~964-971)

```python
    HARDCODED_DEFAULTS = {
        "instructions": "instructions.md",
        "strategic_todos_initial": "strategic_todos_initial.yaml",
        "strategic_todos_transition": "strategic_todos_transition.yaml",
        "strategic_todos_resume": "strategic_todos_resume.yaml",
        "workspace_template": "workspace_template.md",
    }
```

- [x] **Step 6: Remove the matrix deploy block + skip in `src/agent.py`** (~2073-2099)

Delete the `# todo_guide.md — via instruction matrix` block (the `model_family`/`instr_resolver` setup at ~2073-2077 and the `try: ... todo_guide ... except FileNotFoundError` at ~2078-2085) **and** the now-dead skip inside the loop:

```python
                    # Skip todo_guide.md — already handled above via matrix
                    if entry.file == "todo_guide.md":
                        continue
```

Then prune imports that become unused: confirm with
```bash
grep -n "InstructionMatrixResolver\|family_of\|model_family\|instr_resolver" src/agent.py
```
If they're used only by the deleted block, remove `InstructionMatrixResolver` from the `from .core.loader import (...)` group (~2058) and `from .core.model_registry import family_of` (~2063). Keep `FileResolver`, `render_instruction_content`, `load_instructions` (still used for `instructions.md` + the file loop).

- [x] **Step 7: Reconcile the dead `todo_guide.md` fallback** (`src/tools/core/todo.py` ~104-122)

This fallback fires only when `not context._instruction_files`; every config now `$extends defaults` (which binds `todo-guide`), so it's unreachable. Point its path at the migrated skill so a stray instruction-less config nudges toward the right artifact (cosmetic, keeps no stale `todo_guide.md` string):

```python
            if not context._instruction_files and not context.was_recently_read(
                "skills/todo-guide/SKILL.md"
            ):
                from src.services.guardrails import format_nudge

                model = (
                    context._llm_config.model
                    if context._llm_config is not None
                    else None
                )
                return format_nudge(
                    "read_file_required_error",
                    model=model,
                    file_path="skills/todo-guide/SKILL.md",
                    tool_name="next_phase_todos",
                )
```

- [x] **Step 8: Grep for stragglers, then delete the old template**

```bash
grep -rn "todo_guide" --include=*.py --include=*.yaml . | grep -v "todo-guide"
git rm config/templates/todo_guide.md
```
Expected: after the edits above, no functional `todo_guide` / `todo_guide.md` reference remains (tests may reference the gate behaviorally — update those to the skill path). Re-run the grep until clean.

- [x] **Step 9: Run the full binding + skills suites + regressions**

```bash
python -m pytest tests/test_skill_bindings.py tests/test_skill_resolution.py tests/test_skill_runtime.py tests/test_skill_tool.py tests/test_resolved_config_hydrate.py tests/test_persona_fencing.py -v
python -c "import src.agent, src.core.loader, orchestrator.main"
```
Expected: PASS; no import error. If any pre-existing test asserted `todo_guide.md` enforcement, update it to `skills/todo-guide/SKILL.md`.

- [x] **Step 10: Commit**

```bash
git add config/skills/todo-guide/SKILL.md config/defaults.yaml config/experts/scholar/config.yaml \
        src/core/loader.py src/agent.py src/tools/core/todo.py tests/test_skill_bindings.py
git commit -m "feat(skills): migrate todo_guide to bundled skill; remove instruction-matrix special-case"
```

---

## Task 7: Live end-to-end verification on k3d (the DoD)

Tilt auto-rebuilds/redeploys on commit. Verify both bindings fire from `skills/<name>/SKILL.md`, and — the critical new check — that the **todo gate still works with the catalog flag OFF**. Mirror the Slice-2 approach (`X-Internal-Key: dev_mcp_internal_key`, `user_id` in the body, admin uuid, `config_override={"scholar":{"enabled":false}}`, in-pod `python3`/`kubectl exec` heredocs, port 8085). See `docs/superpowers/plans/2026-06-18-skills-slice-2.md` As-built notes + `memory/k3d_verify_runtime_in_deployed_images.md` for the dispatch gotchas.

**Files:** none (verification only; capture for Task 8).

- [x] **Step 1: Confirm deploy + flag**

```bash
kubectl -n <dev-ns> get pods -l app=orchestrator -o jsonpath='{.items[0].metadata.creationTimestamp}{"\n"}'
kubectl -n <dev-ns> exec deploy/orchestrator -- printenv SKILLS_DB_ENABLED   # expect: true
```

- [x] **Step 2: `todo_guide` gate (flag ON).** Dispatch a `default`-config worker job (admin uuid in body, scholar disabled). Inspect the frozen blob and the gate:
  - `resolved_config->'instructions' ? 'todo-guide'` is **true** (frozen via the instructions channel);
  - `resolved_config->'skills'->'menu'` does **not** contain `todo-guide` (filtered, decision 3);
  - the agent's first `next_phase_todos` is nudged to read `skills/todo-guide/SKILL.md`, and after `use_skill("todo-guide")` **or** `read_file("skills/todo-guide/SKILL.md")` the tool proceeds (audit trail).
  Expected: gate enforces at the skill path and opens on read.

- [x] **Step 3: `research_guide` phase injection (flag ON).** Dispatch a `scholar` job (or `config_override` binding research-guide) that reaches a **tactical** phase. Confirm `skills/research-guide/SKILL.md` is materialized and its body is injected as a transient instruction message at tactical-phase start (audit / first tactical LLM request contains the Research Guide content).
  Expected: phase injection reads the skill path.

- [x] **Step 4: PROD-SAFETY — `todo_guide` gate with the catalog flag OFF.** The load-bearing check (decision 2). Either flip `SKILLS_DB_ENABLED=false` on the orchestrator (or reason from a flag-off config) and dispatch a `default` job:
  - blob has **no** `skills` key (catalog off), but `resolved_config->'instructions' ? 'todo-guide'` is still **true**;
  - `skills/todo-guide/SKILL.md` is still materialized (instructions channel), and the `next_phase_todos` gate still enforces + opens on read.
  Expected: **deterministic enforcement survives with the catalog off** — no regression to the shipped todo gate. (Restore the flag after.)

- [x] **Step 5: Record results** (job ids, what was asserted, the flag-off outcome) for the Task-8 as-built notes.

---

## Task 8: Docs — flip Slice-3 status + as-built notes

**Files:**
- Modify: `docs/features/agent_skills.md` (Slice list ~156; the binding-taxonomy table ~80-87 — flip the `before_tool`/`phase` rows' Status to reflect they now run on skills)
- Modify: this plan (status banner + "## As-built notes")

- [x] **Step 1: Update the design doc** — mark Slice 3 shipped + k3d-verified (mirror how Slices 1/2 are annotated); update the binding-taxonomy table so `before_tool` and `phase` rows note "migrated to skills — Slice 3"; keep Slice 4/later lines unchanged.

- [x] **Step 2: Add a status banner + "## As-built notes"** to this plan (divergences, exact drifted line numbers, the menu-filter decision, the flag-off prod-safety result, and any tests updated for the migrated gate path).

- [x] **Step 3: Commit**

```bash
git add docs/features/agent_skills.md docs/superpowers/plans/2026-06-19-skills-slice-3.md
git commit -m "docs(skills): mark Slice 3 (expert bindings + migration) shipped + as-built notes"
```

---

## Self-review (run before executing)

**Spec coverage** (against `docs/features/agent_skills.md` Slice 3 — "`expert.config` `skill → binding`; migrate `todo_guide`/`research_guide` to skills, preserving their enforced/phase bindings"):
- `skill → binding` → Tasks 1 (`skill:` field + `path`) + 2 (consumers use `path`) + 3 (delivery) + 4 (catalog filter). ✓
- Migrate `research_guide`, preserve `phase` binding → Task 5. ✓
- Migrate `todo_guide`, preserve `enforce` binding → Task 6 (+ matrix cleanup). ✓
- "Preserving bindings" (don't lose enforcement) → decision 2 (flag-independent channel) + Task 7 Step 4 (flag-off proof). ✓

**Type/name consistency:** `InstructionFileEntry.path` = `skills/<skill>/SKILL.md` is produced in Task 1 and consumed identically in Task 2 (enforce/phase), Task 3 (deploy write target), and matches `use_skill`'s `record_file_read("skills/<name>/SKILL.md")` (Slice 2) and the bundled dir `config/skills/<name>/` (Task 5/6). Bound-skill blob key = the **skill name** (`instructions[entry.skill]`), read back by the same name in both deploy paths. `filter_bound_skills` reads `blob["agent"]["instruction_files"]` (serialized dataclass dicts carry `skill`) and `blob["skills"]["menu"/"files"]` (Slice-2 shape `{name,...}` / `{name: {...}}`).

**Placeholder scan:** every code step carries real code or a real shell command; Task 5 Step 3 and Task 6 Step 3 carry the full/constructed skill files; the only environment-specific tokens are Task 7's `<dev-ns>` and job ids.

**Risks flagged:**
- **Prod enforcement regression (highest):** mitigated by decision 2 (instructions channel) + Task 7 Step 4 (explicit flag-off gate proof). The migration is behavior-preserving by construction (`path == file` for unchanged entries; bound-skill content frozen the same way `research_guide` already is).
- **`InstructionFileEntry` field reorder:** only the two keyword parse sites construct it (grep-confirmed); `__post_init__` XOR catches malformed entries at load (same failure mode as today's missing-`file` KeyError).
- **Unused imports after the matrix removal** (`InstructionMatrixResolver`, `family_of`) → ruff fails the push workflow; Task 6 Step 6 prunes them after a usage grep.
- **Deploy unit-test gap:** the workspace write branch (Task 3) is proven on k3d (Task 7), not in unit tests — the Slice-2 precedent; the serialize half is unit-tested.
- **Filter ordering:** `filter_bound_skills` runs after `resolve_config`, when `blob["agent"]["instruction_files"]` is fully merged (base + expert + override), so an expert that binds a catalog skill correctly removes it from its own menu.

## Execution handoff

Two options (per writing-plans):
1. **Subagent-driven** (recommended) — fresh subagent per task, two-stage review between tasks.
2. **Inline** — execute in this session with checkpoints (how Slices 1 + 2 were run; the Task-5 checkpoint is the natural mid-slice gate).

> Executed **inline** on `develop`, 2026-06-19.

---

## As-built notes (post-implementation, 2026-06-19)

**Commits (8):** Task 1 (`InstructionFileEntry.skill`/`.path`) · Task 2 (consumers → `entry.path`) · Task 3 (flag-independent `instructions` channel) · Task 4 (`filter_bound_skills`) · a test-double fix (see below) · Task 5 (`research_guide` migration) · Task 6 (`todo_guide` migration, expanded) · Task 8 (docs).

**The Task-5 checkpoint caught a real plan gap.** The plan (and the upfront two-agent research) found "3 hardcoded `todo_guide` refs" and called the migration "behavior-preserving." Re-grepping at Task 6 surfaced two misses: (1) `todo_guide` has a **per-family variant** `todo_guide_gpt_oss.md` selected via `model_config_matrix.yaml` — bundled skills have no per-family selection, so migrating it is a regression for gpt_oss-family models; (2) **6 strategic-todo templates** hardcode "read `todo_guide.md`" as live agent instructions and would point the gate at a nonexistent path. Per executing-plans, work stopped and the corrected picture went back to the user, who chose **"push through, drop the gpt_oss variant."** `research_guide` had no such entanglement (single-file, phase-injected, no variant) — which is exactly why the plan sequenced it first.

**Task 6 expanded well beyond the plan (all under the user's "push through" decision):**
- Removed the per-family variant: deleted `config/templates/todo_guide_gpt_oss.md` + `todo_guide.md`, and the `todo_guide` entries in **4** `model_config_matrix.yaml` files (base default + base gpt_oss + scholar + developer). **Deliberate regression:** gpt_oss-family agents now get the default todo guide. Recoverable via a future per-family-skills slice (the real fix).
- Repointed **6** strategic-todo templates (`config/templates/strategic_todos_{initial,transition,resume}.yaml`, `strategic_todos_transition_gpt_oss.yaml`, and the scholar/developer `strategic_todos_initial.yaml`): `todo_guide.md` → `skills/todo-guide/SKILL.md` (10 occurrences).
- Removed the stale `todo_guide` entry from `config/prompts/catalog.yaml` (it's a skill now, managed via the skills editor, not prompt-overrides) and fixed a catalog comment that cited the deleted gpt_oss file as an example.
- Updated `tests/test_instruction_matrix.py` (the `todo_guide` matrix-resolution assertions: removed two, repointed the fall-through example to `strategic_todos_initial`, dropped the serialize fixture+assertion).

**Other divergences:**
- **Task 2 test-double gap (self-caught):** `entry.file`→`entry.path` broke `tests/test_tool_registry.py`'s `FakeInstructionEntry` (a `@dataclass` double lacking `.path`). I'd only run `test_skill_bindings.py` during Task 2; running the broader enforcement suite before the checkpoint surfaced it. Fixed by adding a `.path` property to the double (commit "test(skills): teach FakeInstructionEntry the .path resolver"). Lesson: run the **shared-code** regression suites (`test_tool_registry`, `test_instruction_matrix`) per task, not just the new file.
- **Task 3 serialize test** needed `cfg._deployment_dir = str(tmp_path)` — `serialize_resolved_config` constructs prompt/instruction resolvers that choke on a `None` deployment dir; the skill-freeze branch itself reads `config/skills/<name>/SKILL.md` directly and is dir-independent.
- **`src/tools/core/todo.py` fallback** (the `not context._instruction_files` backward-compat nudge) is now unreachable (every config `$extends defaults`, which binds `todo-guide`), but its path string was updated to `skills/todo-guide/SKILL.md` so it carries no stale filename.

**Verification (Task 7) — both in the deployed orchestrator image AND a full autonomous LLM run on k3d.** The in-image checks proved the resolution/freeze/filter/gate logic against deployed code under both flag states:
1. **Flag-OFF (prod-safety, the load-bearing check):** `resolve_config("defaults")` with `SKILLS_DB_ENABLED` unset → `instruction_files` binds `skill: todo-guide`; `instructions["todo-guide"]` is frozen (flag-independent); **no** `skills` catalog key; no stale `todo_guide` key.
2. **Agent-side gate (flag-off):** `load_config_from_resolved` → the binding resolves to `skills/todo-guide/SKILL.md`, the body is hydrated for deploy, `get_enforcement_files("next_phase_todos") == ["skills/todo-guide/SKILL.md"]`, the gate is closed until that path is read, then opens.
3. **Both bindings on the scholar expert:** `resolve_config("experts/scholar")` → `{todo-guide: before_tool:next_phase_todos, research-guide: phase:tactical}`, both frozen in the `instructions` channel; `get_phase_instruction_files("tactical")` injects `skills/research-guide/SKILL.md`.
4. **Flag-ON menu filtering:** `filter_bound_skills` strips `todo-guide` + `research-guide` from a catalog menu/files while they remain bound (no double-offer).

**Full autonomous run on k3d (flag-on dev, gemma-4-moe via `https://ai.h4ll.app/v1`):** Two jobs dispatched via the internal API (admin user in body, scholar disabled). The frozen `resolved_config` of a live `default` job bound `{skill: todo-guide, before_tool:next_phase_todos, enforce: true}`, froze `instructions["todo-guide"]`, and the catalog menu was `[hello-skill, research-guide]` (todo-guide **filtered**; research-guide correctly retained as optional since `default` doesn't bind it). The first job (`4abbbab0`) hit a gemma-4-moe **write_file loop in strategic planning** and never reached `next_phase_todos` — a model-quality issue, not a skills one (cancelled). A second, loop-resistant job (`11d6f0e1`, "do not write files; go straight to next_phase_todos") exercised the gate cleanly: **iter 0** `next_phase_todos` (skill unread) → **iter 1** tool result `"Error: You must read \`skills/todo-guide/SKILL.md\` before using next_phase_todos"` → agent `read_file("skills/todo-guide/SKILL.md")` → **iter 2** the 14,861-char todo-guide body delivered → **iter 3–4** `next_phase_todos` returned `"Staged 5 todos for the next tactical phase"` (gate **opened**). End-to-end live proof that the migrated gate fires at the skill path and opens on read.

**Deferred / follow-up:** (a) **per-family skill variants** — the real fix to restore the gpt_oss todo guide; the blocker for any per-family skill. (b) DB/project **bound** skills (this slice is bundled-only). (c) materializing a bound skill's non-`SKILL.md` files via the binding channel (the two guides are single-file). (d) gemma-4-moe is loop-prone on file-writing tasks (unrelated to skills); the local-k3d LLM endpoint is a small model.
