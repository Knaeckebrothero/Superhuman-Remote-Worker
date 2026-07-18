---
tags:
  - issue
  - agent
  - delegation
  - watchdog
  - loop
---

# Deep `spawn_subagent` fan-outs blow the 120 s delegation batch watchdog — mislabeled "Tool batch repeatedly timed out — workspace may be wedged" and the job fails

**Status:** FIX BUILT 2026-07-18 (all three layers, unit-tested; see
"Fix implemented" below). Awaiting commit + rollout.
**Severity:** medium — intermittent (only deep fan-outs); each hit wastes a
loop iteration, burns ~6 discarded reader LLM runs, bumps
`consecutive_failures` toward the loop cap, and emits an error that
misdirects investigation toward the drain/wedge bugs.
**Component:** `src/tools/delegation/spawn_subagent.py` +
`light_runner.py`, `src/graph.py` batch watchdog
(`_get_batch_tool_timeout`), `config/defaults.yaml`
(`tool_category_timeouts.delegation: 120`).

## Incident

Better-Resavio loop job `472ea457-54fd-4196-a97d-b7bbc4ea6ad4` (iter 10,
SCHOLAR, MiniMax, prod/homelab) FAILED 2026-07-14 15:57 with
`error_message = "Tool batch repeatedly timed out — workspace may be
wedged"`. The workspace was NOT wedged, and this is NOT the
version-upgrade-drain bug (real DB row: `freeze_data=NULL` — note
`get_frozen_job` synthesized a bogus `version_upgrade` freeze for it; trust
the DB).

## Root cause — structural timeout mismatch

`spawn_subagent` light mode runs an ENTIRE bounded ReAct reader loop INLINE
(`run_light_subagent`): up to `max_iterations=10` LLM turns / 40 k tokens
per reader (`config/defaults.yaml:417-419`), with **no wall-clock
deadline** (latency-bound only). But the graph's tool-batch watchdog caps a
`delegation`-category batch at **120 s**. A 3-way research fan-out that
actually uses its iteration budget on MiniMax blows past 120 s.

Audit sequence: LLM emits 3 `spawn_subagent` → timeout@120s →
`_reconnect_workspace()` (pointless SSH bounce that also cancels the
in-flight readers) → LLM retries the same 3 → timeout@120s →
`_TOOL_TIMEOUT_RETRIES>=1` → `raise WorkspaceUnavailableError`
(`src/graph.py:4267`) → agent error-state, job failed.

**Category error:** the batch watchdog exists to detect a dead SSH
workspace (a hung `read_file`). `spawn_subagent` isn't an SSH op — its
latency says nothing about workspace health, yet its timeout is handled by
reconnect-then-fatal.

## Fix implemented (2026-07-18, all three layers)

1. **Wall-clock deadline inside `run_light_subagent`**
   (`src/tools/delegation/light_runner.py`): new `timeout_seconds`
   parameter (0 = unbounded, preserving the pure-harness default), wired
   from `delegation.light.timeout_seconds: 240` in `config/defaults.yaml`.
   The deadline bounds every LLM turn and every tool batch via
   `asyncio.wait_for`; on overrun the reader forces the existing
   `_final_synthesis` path and returns partial results. A tool turn cut
   off mid-flight appends synthetic ToolMessages for every pending
   tool_call first, so strict providers never see dangling tool_calls.
   The forced-synthesis LLM call itself is bounded
   (`_SYNTHESIS_TIMEOUT_SECONDS = 90`) and falls back to the reader's
   last text.
2. **Backstop raised**: `tool_category_timeouts.delegation: 120 → 600`
   (`config/defaults.yaml`) — covers multi-wave fan-outs
   (fan-out > `max_parallel` runs in waves of ≤240 s each).
3. **Delegation-only batch timeouts are non-fatal**
   (`src/graph.py`, `audited_tools` timeout handler): when every call in
   the timed-out batch has registry category `delegation`, skip the SSH
   reconnect and the `WorkspaceUnavailableError` escalation entirely;
   return timeout ToolMessages carrying an adapt hint ("spawn fewer /
   narrower subagents"). `_TOOL_TIMEOUT_RETRIES` is neither incremented
   nor reset, so the SSH wedge watchdog stays armed for every other
   category.

Tests: `tests/test_light_subagent.py::TestWallClockDeadline` (slow LLM
turn cut off → synthesis; slow tool turn cut off → paired ToolMessages +
synthesis; `timeout_seconds=0` unbounded; hung synthesis falls back to
last text). Scholar/developer expert configs only set `mode`/
`allow_writes`, so the new defaults reach them via deep merge.

## Related

- Memory/topic: `project_spawn_subagent_120s_watchdog_misfire`.
- `docs/issues/agent_fast_freeze_on_dead_workspace.md`,
  `docs/done/version_upgrade_drain_masked_by_coincident_error.md` — the
  bugs this error message falsely points at.
- Distinct same-day failure: `36969384` ("timed out 627s waiting for parent
  job … workspace") is a separate provisioning issue, covered by
  `scholar_selfprovisioned_workspace_misclassified_as_inherited.md`.
