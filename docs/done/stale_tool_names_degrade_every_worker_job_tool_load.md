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
**Status:** OPEN. Fixed in `config/session_base.yaml` only.
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

## Fix

Delete the two names from `config/worker_base.yaml` and the four expert configs.
Prose references in `config/README.md:221-222` and several `docs/` files should
go at the same time.

Then extend `tests/test_config_tool_names_are_registered.py` — it currently
guards `session_base` only — to parametrize over `worker_base` and
`config/experts/*/config.yaml`. That test exists precisely to pin this class of
bug and was deliberately scoped narrow when the session half shipped.

## Why it was left out

Scope decision when fixing the session-side checkbox bug: that change was
already spanning orchestrator, cockpit and config, and touching the worker
dispatch path in the same commit would have widened the blast radius for a
defect with no user-visible symptom. Splitting it keeps the worker change
independently revertable.
