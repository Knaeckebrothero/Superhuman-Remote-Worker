---
tags:
  - issue
  - agent
  - config
  - tooling
---

# Two removed tool names in `worker_base.yaml` make every worker job fail its batch tool load and fall back to loading tools one at a time

**Filed:** 2026-07-28, split out of
`docs/done/session_tool_group_checkbox_disagrees_with_the_agent.md` (the session
half is fixed; this is the worker half).
**Status:** FIXED 2026-07-29 — removed from `worker_base.yaml` and all four
expert configs; every shipped config now resolves with zero unknown tool names.
Not yet deployed, so dev worker jobs still log the warning until the next
rollout. Verified locally: the merged tool list for `worker_base`, `developer`,
`designer` and `scholar` contains no unknown names, so the batch load no longer
raises.
**Severity:** low-to-medium — no functional loss today (the fallback loads every
*valid* tool), but it burns the batch-validation safety net on every single
worker job and hides real bind failures at DEBUG. It is a latent-severity bug:
the day a tool genuinely fails to instantiate, nothing will say so.
**Component:** `config/worker_base.yaml:106-107`,
`config/experts/{developer,scholar,designer,designer-interactive}/config.yaml`;
`src/agent.py:2840-2850`; `src/tools/registry.py:372-378`.

## Summary

`browse_website` and `download_from_website` were removed from `TOOL_REGISTRY`
when the direct `browser_*` tools replaced them
(`src/tools/research/__init__.py:27` documents the removal, and
`tests/tools/research/test_browser_tools.py:504` pins them as gone). They are
still listed under `tools.research` in the worker base config and four expert
configs.

`load_tools` validates **all** requested names up front and raises for the whole
batch if any is unknown (`src/tools/registry.py:372-378`). So every worker job
takes this path in `src/agent.py:2840-2850`:

```python
try:
    self._tools = load_tools(tool_names, context)
except ValueError as e:
    logger.warning(f"Tool loading warning: {e}")
    for name in tool_names:
        try:
            implemented_tools.extend(load_tools([name], context))
        except ValueError:
            logger.debug(f"Tool not implemented: {name}")   # <- swallowed
```

The per-tool retry succeeds for every valid name, so the job runs normally. The
cost is that the `except ValueError` in the loop is now load-bearing for
correctness rather than a fallback: a tool that fails to instantiate for a *real*
reason (missing dependency, unavailable adapter, bad context) is silently
dropped at DEBUG, and the job proceeds with a quietly smaller toolset.

## Live evidence (dev, 2026-07-28)

Worker agent `srw-agent-j-66721ced`, job `4119f03c`:

```
{"level":"WARNING","logger":"src.agent","file":"agent.py:2843",
 "message":"Tool loading warning: Unknown tools:
            ['browse_website', 'download_from_website']. Available tools: …"}
```

Note the diagnostic trap this creates: the later `Loaded N tools: [...]` INFO
lines look perfectly healthy, so sampling the tail of a log suggests the batch
load succeeded. The warning fires once, ~30 minutes earlier in that job's log.

## Fix (as applied, 2026-07-29)

Removed the names from `config/worker_base.yaml` and the `developer`, `scholar`,
`designer` and `designer-interactive` configs. Updated the reference prose in
`config/README.md` (which listed them as available — copy-paste bait, and
plausibly how they spread), `docs/advanced_websearch.md` (capability table **and**
an example config block), `docs/dev_workflow.md` and `docs/memories_mechanism.md`.
`docs/browser_use.md` was **annotated, not rewritten** — it is explicitly "a
living design doc, not a spec" and is the record of the analysis that led to
removing these very tools, so a status note at the top preserves the reasoning
instead of erasing it.

`tests/test_config_tool_names_are_registered.py` now parametrizes over both base
configs and **every** expert discovered by globbing `config/experts/*/config.yaml`
— 12 configs, so a newly added expert is covered on arrival rather than when
someone remembers. Configs are checked in their **merged** form, which matters:
`general-worker` has no stale name of its own and was only reachable through
`$extends: worker_base`. Confirmed to have teeth by re-adding a stale name and
watching both `worker_base` and `general-worker` fail.

## Verification

- 18 test cases green; full suite 11606 passed / 2 failed (both needing a live
  local Postgres, unrelated).
- Merged tool lists resolve clean: `worker_base` 64 names, `developer` 44,
  `designer` 47, `scholar` 64, zero unknown in each.
- **Owed:** confirm on dev after the next rollout that a worker job's log no
  longer contains `Tool loading warning: Unknown tools:` from `agent.py:2843`,
  and that the toolset arrives via a single `Loaded N tools: [...]` line rather
  than the per-tool fallback. Grep the **whole** log, not the tail — the healthy
  looking `Loaded N tools` lines come *after* the warning, which is what made
  this look fixed when it wasn't.

## Why it was left out

Scope decision when fixing the session-side checkbox bug: that change was
already spanning orchestrator, cockpit and config, and touching the worker
dispatch path in the same commit would have widened the blast radius for a
defect with no user-visible symptom. Splitting it keeps the worker change
independently revertable.
