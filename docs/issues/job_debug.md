---
tags:
  - debugging
  - tool-development
  - context-management
  - performance
  - research
---

# Job Debug: Obsidian Tagging Test Run

**Job ID:** `4c8e1d60-a7fc-4a3c-9f1e-fe4396b9805e`
**Date:** 2026-02-19
**Model:** `openai/gpt-oss-120b` (default)
**Status:** Cancelled after ~4.5 hours
**Task:** Enrich 84 markdown documents with Obsidian-compatible tags, wiki-links, and Related sections

## Results

- **Docs enriched: 3 out of 84** (only the 3 pilot docs)
- **Tag index:** 21 tags defined in `output/tag_index.md`
- **Audit entries:** 480
- **Phases completed:** 7 (cancelled at start of phase 7 tactical — the batch enrichment phase)
- **Claude Code:** Never used (tool not enabled)

## Phase Timeline

| Phase | Type | Duration | What it did |
|-------|------|----------|-------------|
| 0 | Strategic | ~20 min | Read instructions, pinned rules, created plan, created first todos |
| 1 | Tactical | ~4 min | Inventoried vault (84 files), skimmed 4 docs, drafted 21-tag taxonomy, ran verification |
| 2 | Strategic | ~34 min | Retrospective, rewrote workspace.md, updated plan, created pilot todos |
| 3 | Tactical | ~3 min | Pilot-enriched 3 docs via Python scripts, updated tag index, verified |
| 4 | Strategic | **~2h 20min** | Retrospective, discovered `documents/` git-ignore blocker, planned fix |
| 5 | Tactical | ~14 min | Fixed `.gitignore`, git-added 84 docs, redid pilot, updated verification |
| 6 | Strategic | ~1h | Retrospective, rewrote workspace.md, updated plan, created batch todos |
| 7 | Tactical | ~1 min | **Cancelled before any work** |

## What Went Well

- **Prompt system worked correctly.** Strategic/tactical alternation followed properly. All todos in all phases completed (100% across 8 phases).
- **Workspace.md discipline.** Stayed under 50 lines, rewritten from scratch each strategic phase as instructed.
- **Self-correction.** Discovered the `.gitignore` blocker on its own, correctly identified root cause, planned and executed the fix.
- **Quality of enriched docs.** The 3 pilot docs passed all automated verification checks (YAML validity, tag format, link resolution).
- **Git versioning worked.** 7 phase boundary tags, commits traceable per-todo, retrospectives backed by git evidence.

## Issues Found

### 1. Claude Code tool not available

The `claude_code` tool is commented out in `config/defaults.yaml`:
```yaml
coding:
  - run_command
  # - claude_code  # Requires: Claude Code CLI installed + authenticated
```

The kickoff message explicitly told the agent to use Claude Code for batch processing, but the tool wasn't in its toolset. It fell back to `run_command` with inline Python scripts — fine for 3 pilot docs but would be extremely slow for 84.

**Fix:** Enable `claude_code` in the config or via `config_override` in the UI.

### 2. Strategic overhead is disproportionate

4 strategic phases produced **zero deliverable progress** — only planning, review, retrospectives, and workspace maintenance. The ratio of planning to execution is brutal:

- **Strategic phases:** 4 (each has 4 mandatory todos: REVIEW, REFLECT, ADAPT, PLAN-OR-COMPLETE)
- **Tactical phases:** 3 (actual work)
- **Time in strategic:** ~4 hours
- **Time in tactical:** ~21 minutes

For a batch task like "tag 84 files," the agent shouldn't need a full review/retrospective cycle after every 5-todo tactical phase. The strategic overhead is designed for complex multi-phase research/writing jobs, not repetitive batch operations.

**Ideas:**
- Allow configuring strategic phase frequency (e.g., review every N tactical phases instead of every 1)
- Lighter strategic template for batch/repetitive tasks
- Expert configs could override the strategic todo template

### 3. Phase 4 strategic took 2+ hours

The Phase 4 strategic review (12:42 → 15:03) took over 2 hours for a phase that produced 3 edited files. Root cause unclear — possibly slow model inference, context management overhead, or the model getting stuck in verbose reasoning. This needs investigation via the MongoDB audit trail.

### 4. `.gitignore` detour cost a full cycle

The `documents/` git-ignore issue was a real problem (deliverables wouldn't be committed), but the fix was one line in `.gitignore`. It consumed an entire strategic phase (discovery + planning) and an entire tactical phase (5 todos including redo of pilot enrichment). Could have been a single todo within an existing phase.

### 5. Tag taxonomy is too narrow

21 tags for 84 documents about a complex software project is probably insufficient. Tags like `agent-initialization`, `workspace-initialization`, `job-initialization` are too specific to the codebase internals. Missing broader categories like `deployment`, `security`, `ui`, `testing`, `documentation`, `performance`, `research`, `architecture`.

The agent skimmed only 4 docs before defining the taxonomy — it needed more sampling.

### 6. No `## Related` section on most enriched docs

Even the 3 pilot docs had only 1-3 Related links each (target was 3-6). The enrichment script filtered out non-existent link targets but didn't try harder to find related docs. The `## Related` section was present in only 5 of 84 docs in the workspace (some pre-existing).

## Recommendations for Next Run

1. **Enable Claude Code** — add `claude_code` to the coding tools list or pass via config_override
2. **Use a faster/better model** — `openai/gpt-oss-120b` is slow; try `groq/moonshotai/kimi-k2-instruct-0905` or a Claude model
3. **Skip the pilot phase** — for a simple batch task, go straight to batch enrichment. The pilot added 2 extra phases (pilot tactical + pilot review strategic) for minimal value.
4. **Bigger tactical phases** — instead of 5 todos per phase, use 10-15 for batch work. Process 10-15 docs per tactical phase.
5. **Consider reducing strategic frequency** — not every tactical phase needs a full REVIEW/REFLECT/ADAPT/PLAN cycle. For batch repetitive work, a lightweight check every 2-3 tactical phases would suffice.
6. **Pre-build the tag taxonomy** — provide a starter set of tags in the instructions to save the agent from having to derive them from scratch.

## Related

- [[vm]]
- [[context_management]]
- [[tool_issues]]
- [[agent_architecture]]
