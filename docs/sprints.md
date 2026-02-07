# Sprint Limit for Tactical Phases

## Problem

The agent can spend thousands of iterations in a single tactical phase brute-forcing its way through difficult todos (e.g., 10k steps verifying citations) without ever pausing to reconsider its approach. While it eventually succeeds, the lack of forced reflection leads to inefficient execution. The agent never truly gets "stuck" — but it doesn't realize when it's being wasteful.

## Concept

Add a configurable **sprint limit**: a maximum number of LLM iterations per tactical phase. When the limit is reached, the agent is forced back into strategic phase for self-reflection. This is not a punishment or hard stop — it's a mandatory retrospective.

During the forced strategic phase, the agent:
- Reviews what it accomplished vs. what's still open
- Reflects on whether the approach is working
- Can split large todos into smaller, achievable ones
- Can reprioritize or adjust strategy
- Can consciously decide to spend another sprint on the same work

The key principle: **the only thing that matters is that the agent is periodically self-reflecting**.

## Design

### Sprint Limit Check

- New config: `phase_settings.sprint_limit` (integer, default `0` = disabled)
- Track `phase_start_iteration` in state — set when entering a tactical phase
- In `check_todos` node: if tactical phase AND `iteration - phase_start_iteration >= sprint_limit`, force `phase_complete = True`
- The existing `archive_phase -> handle_transition -> on_tactical_phase_complete` path handles the rest

### Incomplete Todos

Incomplete todos are already archived properly by `TodoManager.archive()` (marked as not-completed in the archive file). They are NOT automatically carried over — the agent must consciously re-create them during the next strategic phase's PLAN step. This forces deliberate decision-making about what continues vs. what gets dropped or restructured.

### Sprint-Aware Transition Message

When a sprint limit triggers the transition (vs. natural completion), the `[PHASE_TRANSITION]` message includes:
- How many iterations were used
- How many todos were completed vs. total
- Reflection prompts: Why did remaining todos take longer? Should they be split? Is the approach working?
- Reminder that archived incomplete todos can be re-created (modified or as-is)

The existing strategic todos template (REVIEW, REFLECT, ADAPT, PLAN) already covers the right workflow — no prompt changes needed.

### State Fields

| Field | Type | Purpose |
|-------|------|---------|
| `phase_start_iteration` | `int` | Iteration count when current tactical phase started |
| `sprint_limit_reached` | `bool` | Whether phase ended due to sprint limit (vs. natural completion) |

### Config

```yaml
phase_settings:
  min_todos: 5
  max_todos: 20
  sprint_limit: 0  # 0 = disabled. E.g. 500 for forced reflection every 500 iterations.
```

## Files to Modify

| File | Change |
|------|--------|
| `src/core/state.py` | Add `phase_start_iteration` and `sprint_limit_reached` fields |
| `src/core/loader.py` | Add `sprint_limit` to `PhaseSettings`, update both config parsers |
| `config/defaults.yaml` | Add explicit `phase_settings` section |
| `config/schema.json` | Add `phase_settings` schema with `sprint_limit` |
| `src/graph.py` | Sprint limit check in `check_todos` node |
| `src/core/phase.py` | Set `phase_start_iteration` on entering tactical; sprint-aware message on leaving |
| `tests/test_sprint_limit.py` | Unit tests |
