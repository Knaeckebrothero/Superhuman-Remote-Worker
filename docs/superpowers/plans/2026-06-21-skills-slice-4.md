# Agent Skills — Slice 4 (Script-Bearing Skills) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a skill's bundled `scripts/` runnable end-to-end — directly on shell-capable tiers, and via the already-built workspace-tier-upgrade HITL flow on lite (`virtual`) tiers — with a graceful-degradation note so a shell-less agent still gets the skill's prose.

**Architecture:** The skill *directory* runtime (Slices 1–3) already materializes `skills/<name>/scripts/*` into the workspace, and a shell-holding agent can already `run_command` them. The **only** net-new behavior is teaching `use_skill` that, on a `supports_shell == False` backend, a script-bearing skill's scripts can't run *here* and the remedy is the existing `request_workspace_upgrade` tool. The gate is the existing `shell_tools` semantics (shell tiers) + the existing upgrade HITL flow (lite tiers) — **no new grant key, no new execution tool, no orchestrator/upgrade changes.** We also ship one bundled, stdlib-only script-bearing example skill that doubles as the k3d DoD fixture.

**Tech Stack:** Python 3.12 (CI gate), LangChain `@tool`, pytest. Files under `src/tools/workspace/`, `config/skills/`, `tests/`.

---

## Background the engineer needs

- A **skill is a directory** `skills/<name>/` with `SKILL.md` (required) + optional `references/` (read) + `scripts/` (executed). The open `SKILL.md` standard; see `docs/features/agent_skills.md`.
- **Progressive disclosure:** L1 menu (`name`+`description`, always in the system prompt) → L2 body (`use_skill` reads `SKILL.md`) → L3 files (refs via `read_file`, **scripts via `run_command`** — source never enters context, only output does).
- **`use_skill`** (`src/tools/workspace/skills.py`) is a `workspace`-category tool. It loads on any backend with `supports_file_tools == True` — which includes the `virtual` lite tier (file IO over object store, but **no shell**). So on `virtual`, the agent *can* call `use_skill` and read a body, but *cannot* run scripts.
- **`request_workspace_upgrade(reason)`** (`src/tools/core/upgrade.py`) already exists. It's a `core`-category tool exposed only on lite tiers; it records a `workspace_upgrade_required` freeze (HITL — a human approves, then a sandbox is provisioned, the backend hot-swaps, and the agent's files carry over via the seed copy). It only *requests*; it never flips the tier.
- **Backends & `supports_shell`:** base default `False` (`src/core/workspace_backend.py:479`); `VirtualWorkspaceBackend` returns `False` (`src/core/backends/virtual.py:172`); `RemoteBackend` (sandbox/vm) returns `True`. `tests/_fs_backend.py::FilesystemTestBackend` inherits the base → `False`.
- **Detection rule:** a skill is *script-bearing* iff it has a `scripts/` directory. At runtime that is exactly `workspace.exists("skills/<name>/scripts")` — `VirtualWorkspaceBackend.exists` and `FilesystemTestBackend.exists` are both dir-aware (`is_file or is_dir`).
- **Both job types covered for free:** `use_skill` is shared by sessions and worker jobs, and `request_workspace_upgrade` is exposed on lite tiers for both (`src/api/persistent_session.py:658`, `src/agent.py:2298`). So fixing `use_skill` covers sessions *and* jobs.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/tools/workspace/skills.py` | Modify | `use_skill` L2 loader — append a script-availability note on shell-less tiers; refresh the stale module docstring. |
| `tests/test_skill_tool.py` | Modify | Unit-test the note: fires on lite + script-bearing; suppressed with shell; suppressed for prompt-only. |
| `config/skills/word-count/SKILL.md` | Create | Bundled script-bearing **example** skill (the idiom: prose decides, script computes). |
| `config/skills/word-count/scripts/wordcount.py` | Create | Stdlib-only, output-only script (exact line/word/char counts). Runs anywhere — the k3d DoD fixture. |
| `tests/test_bundled_skills.py` | Create | Validate the bundled example parses + is script-bearing + body points at the script. |
| `docs/features/agent_skills.md` | Modify | Flip the Slice 4 entry + status banner to SHIPPED with as-built/DoD notes (after k3d verify). |

**Explicitly NOT touched:** `src/core/capability_grants.py`, the orchestrator upgrade endpoints, `src/api/persistent_session.py` / `src/agent.py` upgrade handlers, `src/tools/core/upgrade.py`. The slice rides that built infrastructure unchanged.

---

## Task 1: `use_skill` script-availability note on shell-less tiers

**Files:**
- Modify: `src/tools/workspace/skills.py`
- Test: `tests/test_skill_tool.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_skill_tool.py`:

```python
# --- Slice 4: script-availability note on shell-less tiers ---


class _ShellFsBackend(FilesystemTestBackend):
    """A test backend that reports a shell (mirrors RemoteBackend/sandbox)."""

    @property
    def supports_shell(self) -> bool:
        return True


def _use_skill_on(backend, tmp_path):
    ws = WorkspaceManager(job_id="t", base_path=tmp_path, backend=backend)
    ctx = ToolContext(workspace_manager=ws)
    tools = {t.name: t for t in create_skill_tools(ctx)}
    return ws, tools["use_skill"]


def _make_script_skill(ws):
    ws.backend.mkdir("skills/word-count/scripts")
    ws.write_file(
        "skills/word-count/SKILL.md",
        "---\nname: word-count\n---\nRun skills/word-count/scripts/wordcount.py",
    )
    ws.write_file("skills/word-count/scripts/wordcount.py", "print('x')")


def test_script_skill_on_lite_tier_appends_upgrade_note(tmp_path):
    # FilesystemTestBackend.supports_shell == False (the virtual-tier case)
    ws, use_skill = _use_skill_on(FilesystemTestBackend(tmp_path), tmp_path)
    _make_script_skill(ws)
    out = use_skill.invoke({"skill_name": "word-count"})
    assert "Run skills/word-count/scripts/wordcount.py" in out  # body still delivered
    assert "request_workspace_upgrade" in out  # the affordance is named
    assert "cannot be executed" in out.lower()


def test_script_skill_with_shell_has_no_note(tmp_path):
    ws, use_skill = _use_skill_on(_ShellFsBackend(tmp_path), tmp_path)
    _make_script_skill(ws)
    out = use_skill.invoke({"skill_name": "word-count"})
    assert "wordcount.py" in out  # body still delivered
    assert "request_workspace_upgrade" not in out  # shell present → no nag


def test_prompt_only_skill_on_lite_tier_has_no_note(tmp_path):
    ws, use_skill = _use_skill_on(FilesystemTestBackend(tmp_path), tmp_path)
    ws.backend.mkdir("skills/hello-skill")
    ws.write_file(
        "skills/hello-skill/SKILL.md", "---\nname: hello-skill\n---\nJust guidance."
    )
    out = use_skill.invoke({"skill_name": "hello-skill"})
    assert "Just guidance." in out
    assert "request_workspace_upgrade" not in out  # no scripts/ → no note
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_skill_tool.py -v -k "note or shell or prompt_only"`
Expected: the 3 new tests FAIL (`test_script_skill_with_shell_has_no_note` and `test_prompt_only...` may pass trivially since no note exists yet; `test_script_skill_on_lite_tier_appends_upgrade_note` FAILS on the missing `request_workspace_upgrade` / `cannot be executed` substrings).

- [ ] **Step 3: Implement the note**

In `src/tools/workspace/skills.py`:

(a) Replace the stale docstring sentence in the module docstring:

```python
# OLD (lines ~6-8):
# References (skills/<name>/references/) are read with read_file; scripts are
# a Slice-4 concern (capability-grants gated).

# NEW:
# References (skills/<name>/references/) are read with read_file; scripts are
# executed with run_command on a shell-capable tier. On a lite (virtual) tier
# use_skill notes that the scripts need a workspace upgrade first (Slice 4).
```

(b) Inside `create_skill_tools`, right after `workspace = context.workspace_manager`, add the helper:

```python
    def _script_availability_note(skill_name: str) -> str:
        """When a skill bundles scripts but this tier has no shell, tell the
        agent the scripts can't run here and how to unlock them (Slice 4).

        A skill is script-bearing iff it has a ``scripts/`` directory — a single
        dir-aware ``exists`` probe. The note fires only on a shell-less tier
        (``virtual``); on a sandbox/vm the agent just runs the scripts via
        run_command, so no note. Detection must never break use_skill.
        """
        try:
            if getattr(workspace.backend, "supports_shell", True):
                return ""  # has a shell → agent runs scripts directly
            if not workspace.exists(f"skills/{skill_name}/scripts"):
                return ""  # prompt-only skill → nothing to gate
        except Exception:
            return ""
        return (
            "\n\n---\n"
            "[scripts need a workspace] This skill bundles runnable scripts under "
            f"`skills/{skill_name}/scripts/`, but you are on a virtual filesystem "
            "with no shell, so they cannot be executed here. The guidance above "
            "still applies without them. To run the scripts, call "
            "`request_workspace_upgrade(reason=...)`: a human approves, a sandbox "
            "with a shell is provisioned, your files carry over, and you can then "
            "run the script with `run_command` on a later turn."
        )
```

(c) Append the note to the successful return in `use_skill`:

```python
            body = workspace.read_file(skill_md)
            context.record_file_read(skill_md)
            return f"[skill: {skill_name}]\n\n{body}{_script_availability_note(skill_name)}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_skill_tool.py -v`
Expected: all tests PASS (the 4 original + 3 new). The original `test_use_skill_returns_body` still passes — its skill has no `scripts/`, so no note.

- [ ] **Step 5: Commit**

```bash
git add src/tools/workspace/skills.py tests/test_skill_tool.py
git commit -m "feat(skills): use_skill notes script-bearing skills need a workspace upgrade on lite tiers"
```

---

## Task 2: Bundled `word-count` script-bearing example skill

**Files:**
- Create: `config/skills/word-count/SKILL.md`
- Create: `config/skills/word-count/scripts/wordcount.py`
- Test: `tests/test_bundled_skills.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_bundled_skills.py`:

```python
"""Bundled skill fixtures must be valid and (where claimed) script-bearing."""

from pathlib import Path

import yaml

_SKILLS = Path(__file__).resolve().parents[1] / "config" / "skills"


def test_bundled_word_count_skill_is_valid_and_script_bearing():
    root = _SKILLS / "word-count"
    md = (root / "SKILL.md").read_text(encoding="utf-8")

    # Frontmatter parses and carries the two required fields.
    assert md.startswith("---\n")
    frontmatter = yaml.safe_load(md.split("---\n", 2)[1])
    assert frontmatter["name"] == "word-count"
    assert isinstance(frontmatter.get("description"), str)
    assert frontmatter["description"].strip()

    # It is genuinely script-bearing, and the body points at the script.
    assert (root / "scripts" / "wordcount.py").exists()
    assert "scripts/wordcount.py" in md
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_bundled_skills.py -v`
Expected: FAIL with `FileNotFoundError` (the `word-count` skill does not exist yet).

- [ ] **Step 3: Create the bundled skill**

Create `config/skills/word-count/SKILL.md`:

```markdown
---
name: word-count
description: Use when you need exact word, line, and character counts for a text file — runs a bundled script so the numbers are exact instead of estimated by reading.
icon: calculate
color: #94e2d5
---

# Word Count

Counts words, lines, and characters in a text file **exactly**, using a bundled
script — don't estimate the counts by reading the file yourself.

## How to use

This skill bundles a script at `skills/word-count/scripts/wordcount.py`. Run it on
the target file and read the printed result:

    python skills/word-count/scripts/wordcount.py <path-to-file>

It prints one line: `lines=<N> words=<N> chars=<N>`. Use those numbers in your
answer. The script reads only the file you pass and writes nothing — its output
is the only thing that matters.
```

Create `config/skills/word-count/scripts/wordcount.py`:

```python
#!/usr/bin/env python3
"""Exact line/word/char counts for a text file (Agent Skills script example).

The idiom: deterministic, stdlib-only mechanical work the model shouldn't do by
eye. Read the file, count, print one line. Output-only — nothing is written.
"""

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: wordcount.py <path-to-file>", file=sys.stderr)
        return 2
    with open(argv[1], "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    print(f"lines={len(text.splitlines())} words={len(text.split())} chars={len(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_bundled_skills.py -v`
Expected: PASS.

- [ ] **Step 5: Sanity-check the script runs (stdlib-only, output-only)**

Run: `printf 'one two\nthree\n' > /tmp/wc.txt && python config/skills/word-count/scripts/wordcount.py /tmp/wc.txt`
Expected: `lines=2 words=3 chars=14`

- [ ] **Step 6: Commit**

```bash
git add config/skills/word-count/SKILL.md config/skills/word-count/scripts/wordcount.py tests/test_bundled_skills.py
git commit -m "feat(skills): bundle word-count, the first script-bearing example skill"
```

---

## Task 3: k3d end-to-end DoD + flip docs to shipped

**Files:**
- Modify: `docs/features/agent_skills.md`

This task has no unit code — it is the live-cluster proof (mirrors how Slices 2 & 3 were signed off) followed by the as-built doc flip. Requires the local tilt/k3d stack (cluster `srw`); see `docs/features/local_tilt_dev_stack_stinkpad.md` and the dispatch playbook in `k3d_verify_runtime_in_deployed_images`.

- [ ] **Step 1: Bring up local k3d with the new image**

Ensure the agent/orchestrator images include this branch (tilt rebuild), and that an LLM provider is seeded (readiness gate). Confirm `config/skills/word-count/` is in the agent image.

- [ ] **Step 2: Drive the full loop on a `virtual` session**

1. Start a persistent session pinned to the `virtual` tier (thread `workspace.backend = virtual`).
2. In the session: write a short text file with the file tools, then ask for its **exact** word count.
3. Expected agent behavior: `use_skill("word-count")` → response includes the body **and** the `[scripts need a workspace]` note → the agent calls `request_workspace_upgrade(reason=...)`.
4. Approve the upgrade the way the tier-upgrade smoke test does — cockpit "Upgrade workspace", or send the session WS `upgrade-to-workspace` message / hit the orchestrator `upgrade-to-workspace` endpoint — simulating the human click.
5. After the hot-swap: the agent runs `python skills/word-count/scripts/wordcount.py <file>` via `run_command` and reports the counts.

- [ ] **Step 3: Assert the evidence**

Confirm (cockpit transcript or Mongo `srw_logs` per the playbook):
- the `use_skill` result carried the degradation note (the `request_workspace_upgrade` affordance);
- a `workspace_upgrade_required` freeze was raised and approved;
- post-swap the seeded prefix contains `skills/word-count/scripts/wordcount.py` and the agent's earlier text file;
- the `run_command` output line `lines=… words=… chars=…` appears and the agent used it.

If any step fails, STOP and fix before flipping the docs (per executing-plans: don't force through a red DoD).

- [ ] **Step 4: Flip the feature doc to shipped**

In `docs/features/agent_skills.md`:
- Status banner: change the "Next: Slice 4 …" sentence to mark Slice 4 **shipped** with the date and a one-line as-built.
- Slice 4 entry: change "design settled 2026-06-21" to "**✅ SHIPPED (`develop`, <date>)**" and append the DoD-met note (the k3d evidence from Step 3, plus: net-new code was the `use_skill` note + the bundled `word-count` example; no grants/upgrade/orchestrator changes).

- [ ] **Step 5: Commit**

```bash
git add docs/features/agent_skills.md
git commit -m "docs(skills): Slice 4 (script-bearing skills) shipped — as-built + k3d DoD"
```

---

## Verification / CI notes

- Local `pytest` is env-noisy (Py3.14, missing optional deps); **CI (Py3.12) is the authoritative gate.** The two new test files are self-contained (only `yaml` + the fs test backend) and should run clean locally: `pytest tests/test_skill_tool.py tests/test_bundled_skills.py -v`.
- The push workflow auto-runs ruff and may rewrite subagent SHAs — expect a lint pass on push.

## Deferred / out of scope (do NOT build here)

- **L1 menu "runs scripts" marker** — a `has_scripts` flag on menu entries so the agent can pre-emptively upgrade *before* `use_skill`. The note already delivers the core UX (the agent learns at load time); the marker is a one-turn optimization with orchestrator-menu touch-points. Revisit only if the extra turn proves costly.
- **Per-family script variants** — the deferred per-family-skills slice (restores the `todo_guide_gpt_oss` capability for scripts/bodies). Separate slice.
- **Auto-grant of the upgrade** (no human click) — Phase 4 of `workspace_tier_upgrade.md`; stays HITL here.
- **A dedicated `run_skill_script` tool or a new `run_scripts` grant key** — explicitly rejected: scripts run through the existing shell (`run_command` / `shell_tools`), matching the SKILL.md standard and YAGNI.
