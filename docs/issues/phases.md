# Phase Boundary Freeze — Resume Loop Bug

**Status**: Open
**Found**: 2026-02-20
**Affects**: Autonomy levels `partial`, `guided`, `dependent` (any level that freezes at phase boundaries)
**Symptom**: Resuming a frozen job replays the same strategic phase and freezes again in an infinite loop

## Summary

When an agent freezes at a phase boundary (e.g. `partial` autonomy after the first strategic phase), resuming the job replays the entire strategic phase and hits the freeze condition again. This creates an infinite loop — each resume produces the same phase work and freeze.

The root cause is three interconnected bugs in the resume code path. The freeze placement itself (before the transition) is correct by design — it allows the human to reject the transition and have the agent stay in the current phase to fix things.

## Design Intent

The freeze-before-transition placement is intentional:

1. Agent finishes strategic phase (creates plan, stages todos)
2. **Freeze** — human reviews the plan and staged work
3. Human can:
   - **Approve / Resume** → agent completes the transition, enters tactical phase
   - **Resume with feedback** → agent stays in strategic phase to address feedback before transitioning

If the freeze happened after the transition, a rejection would require reverting the phase increment, unloading tactical todos, and restoring the strategic state — strictly worse.

## Root Causes

All three bugs are on the **resume side**, not in the freeze placement.

### RC1: Snapshot recovery overwrites the post-freeze checkpoint

**File**: `src/agent.py` lines 391-405, `src/graph.py` lines 1118-1136

Phase snapshots are created in the `archive_phase` graph node (graph.py:1127), which runs BEFORE `handle_transition`. The snapshot captures the state at the END of the current phase (before any transition logic).

On resume, `snapshot_manager.recover_to_phase()` (agent.py:400) copies the snapshot's `checkpoint.db` over the actual checkpoint file. This replaces the post-freeze checkpoint (at graph END) with the pre-transition checkpoint (at `archive_phase`). The agent is restored to a point where:
- The strategic phase todos are complete
- The transition hasn't happened yet
- `handle_transition` runs again → freeze fires again → loop

### RC2: Plain resume doesn't clear `should_stop` in checkpoint state

**File**: `src/agent.py` lines 491-504

Only the feedback resume path calls `aupdate_state()` to clear `should_stop` and restart the graph:

```python
# Line 491-504: only runs when feedback is provided
if resume and feedback and graph_input is None:
    await self._graph.aupdate_state(
        thread_config,
        {"resume_feedback": feedback, "should_stop": False, ...},
        as_node="__start__",
    )
```

A plain resume (cockpit "Resume" button, no feedback text) skips this entirely. The graph checkpoint still has `should_stop=True`. When `ainvoke(None, config)` is called, the graph is at END with `should_stop=True` — it either does nothing or restarts into a broken state.

### RC3: No mechanism to skip the freeze check on re-entry

Even if RC1 and RC2 were fixed (clean checkpoint, `should_stop` cleared), the agent would re-enter the graph, go through `restore_todo_state` → `execute` → `check_todos` → `archive_phase` → `handle_transition`, and hit the same freeze condition again. There is no state flag to tell `handle_transition` "this boundary was already reviewed and approved — complete the transition instead of freezing again."

## Additional Issue: `config_override` ignored on resume

**File**: `src/agent.py` (config loading logic)

`config_override` from the DB is only applied on fresh runs (`_config_from_db=False`). On resume, the `resolved_config` JSONB from the first run always wins. This means:
- Setting `autonomy: full` via config_override doesn't take effect on resume
- Direct DB update of `resolved_config` is required as a workaround

This is why the workaround of adding `autonomy: full` to `config_override` via SQL didn't fix the stuck job.

## Reproduction Steps

1. Set `autonomy: partial` in `config/defaults.yaml` (the original default)
2. Create and run a new job
3. Agent completes strategic phase 1, freezes at boundary → `job_frozen.json` written, DB status = `pending_review`
4. Resume the job (cockpit Resume button or `POST /api/jobs/{id}/resume`)
5. Agent replays the entire strategic phase 1 (same todos, same work)
6. Freezes at boundary again → stuck in loop

## Observed Behavior (from live job `8e1d3a85`)

Three consecutive resume attempts all produced the same result:

```
# Run 1 (fresh)
d97e315 → c408f65: Strategic phase 1 todos (4 todos)
c408f65: Frozen at strategic phase 0 boundary

# Run 2 (resume)
2b536dc → 73f67b8: Same strategic phase 1 todos replayed
73f67b8: Frozen at strategic phase 0 boundary

# Run 3 (resume with config_override autonomy=full)
cb4eb22 → 4c24396: Same strategic phase 1 todos replayed again
4c24396: Frozen at strategic phase 0 boundary
```

The `config_override` with `autonomy: full` had no effect because `resolved_config` (baked in on first run with `autonomy: partial`) takes precedence on resume.

## Fix Strategy

### Fix A: Skip snapshot recovery for phase boundary resumes (`src/agent.py`)

When `job_frozen.json` exists with `freeze_type: "phase_boundary"`:
1. Read the frozen data before deleting the file
2. Set a flag `is_phase_boundary_resume = True`
3. Skip snapshot recovery entirely (don't call `recover_to_phase()`)
4. Use the raw checkpoint, which has the post-freeze state

```python
if resume:
    frozen_path = workspace.get_path("output/job_frozen.json")
    is_phase_boundary_resume = False
    if frozen_path.exists():
        frozen_data = json.loads(frozen_path.read_text())
        is_phase_boundary_resume = (frozen_data.get("freeze_type") == "phase_boundary")
        frozen_path.unlink()
        # ... update DB status ...

    if is_phase_boundary_resume:
        # Use raw checkpoint (post-freeze state), skip snapshot recovery
        checkpoint_state = await self._graph.aget_state(thread_config)
        # ...
    else:
        # Normal resume: use snapshot recovery (existing code)
        latest_snapshot = snapshot_manager.get_latest_snapshot()
        # ...
```

### Fix B: Handle plain resume with `aupdate_state` (`src/agent.py`)

When resuming from checkpoint (with or without feedback), always call `aupdate_state()` to clear `should_stop` and restart from `__start__`:

```python
# After checkpoint discovery, when graph_input is None (checkpoint found):
if resume and graph_input is None:
    update = {
        "should_stop": False,
        "goal_achieved": False,
        "is_final_phase": False,
        "freeze_approved": True,  # Signal to skip freeze check on re-entry
    }
    if feedback:
        update["resume_feedback"] = feedback
    await self._graph.aupdate_state(thread_config, update, as_node="__start__")
```

On restart, `route_entry` sees `initialized=True` (no feedback) → routes to `restore_todo_state` → restores TodoManager from checkpoint → continues into graph.

### Fix C: Add `freeze_approved` state flag (`src/core/state.py`, `src/core/phase.py`, `src/graph.py`)

Add a `freeze_approved: bool` field to the agent state. This flag is set to `True` by Fix B on resume and tells `handle_transition` to skip the freeze check and complete the normal transition.

**`src/core/state.py`** — Add field to `UniversalAgentState`:
```python
freeze_approved: Optional[bool]  # Set by resume to skip freeze re-check
```

**`src/core/phase.py`** — Check flag before freeze in both transition functions:
```python
# In on_strategic_phase_complete() and on_tactical_phase_complete():
freeze_approved = state.get("freeze_approved", False)
if not freeze_approved and config and should_freeze_at_boundary(config, is_strategic=True, phase_number=phase_number):
    return freeze_for_review(state, workspace, todo_manager, "strategic", phase_number)
```

Reset the flag in the transition result so it doesn't persist into future phases:
```python
return TransitionResult(
    success=True,
    state_updates={
        # ... normal transition state ...
        "freeze_approved": False,  # Reset after use
    },
)
```

**`src/graph.py`** — Add conditional routing from `restore_todo_state`:

Currently `restore_todo_state` always routes to `execute`. When `freeze_approved=True`, route directly to `archive_phase` instead, skipping the unnecessary LLM call (the phase is already complete — there are no active todos to work on):

```python
# Change from:
workflow.add_edge("restore_todo_state", "execute")

# To:
workflow.add_conditional_edges(
    "restore_todo_state",
    lambda s: "archive_phase" if s.get("freeze_approved") else "execute",
    {"execute": "execute", "archive_phase": "archive_phase"},
)
```

Flow on approved resume: `restore_todo_state` → `archive_phase` → `handle_transition` (freeze_approved=True, skip freeze) → normal transition → `check_goal` → `execute` (next phase).

### Fix D: Apply `config_override` on resume (separate fix)

Apply `config_override` even when loading from `resolved_config`. This is a separate issue but was discovered during debugging.

## Resume Flow After Fixes

### Approve (plain resume, no feedback)

```
Resume button pressed
  → agent.py: read + delete job_frozen.json, set freeze_approved=True
  → agent.py: skip snapshot recovery, use raw checkpoint
  → agent.py: aupdate_state(should_stop=False, freeze_approved=True, as_node="__start__")
  → route_entry: initialized=True → restore_todo_state
  → restore_todo_state: freeze_approved=True → archive_phase (skip execute)
  → archive_phase → handle_transition
  → on_strategic_phase_complete: freeze_approved=True → skip freeze → normal transition
  → check_goal: should_stop=False → execute (now in tactical phase with staged todos applied)
```

### Resume with feedback (reject / request changes)

```
"Continue with Feedback" pressed
  → agent.py: read + delete job_frozen.json
  → agent.py: skip snapshot recovery, use raw checkpoint
  → agent.py: aupdate_state(should_stop=False, resume_feedback="...", as_node="__start__")
  → route_entry: resume_feedback set → restore_from_feedback
  → restore_from_feedback: injects feedback message, clears resume_feedback
  → execute: agent acts on feedback (still in strategic phase, can redo work)
  → ... agent fixes things, eventually completes phase again ...
  → handle_transition → freeze fires again (freeze_approved not set)
  → Human reviews again
```

## Test Updates

Update `tests/test_autonomy.py`:
- Add tests for `freeze_approved` flag: when True, `on_strategic_phase_complete()` skips freeze and does normal transition
- Add tests for `freeze_approved` reset: flag is False in transition result
- Existing `TestFreezeForReview` tests remain valid (freeze_for_review behavior unchanged)
- Add integration-style test: simulate freeze → resume → verify transition completes

## Current Workaround

Changed `config/defaults.yaml` from `autonomy: partial` to `autonomy: review`. The `review` level only freezes after `job_complete` (not at phase boundaries), avoiding the resume loop entirely. Phase boundary freezes remain broken for `partial`, `guided`, and `dependent` levels.

## Graph Flow Reference

```
__start__ → route_entry → {init_workspace | restore_todo_state | restore_from_feedback}
  → [restore_todo_state] → {execute | archive_phase}  (conditional: freeze_approved?)
  → execute → check_todos → {execute (loop) | archive_phase}
  → archive_phase [snapshot created here] → handle_transition [freeze check here] → check_goal → {execute | END}
```

Key timing:
- **Snapshot**: Created in `archive_phase`, BEFORE `handle_transition`
- **Freeze check**: In `handle_transition` → `on_strategic_phase_complete()`, BEFORE transition (correct by design)
- **Checkpoint**: Saved by LangGraph after every node; last checkpoint is at END with `should_stop=True`
- **`freeze_approved`**: Set on resume, checked in transition functions, reset after transition completes
