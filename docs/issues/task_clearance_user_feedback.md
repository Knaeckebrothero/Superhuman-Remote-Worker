# Issue: Agent Abandons Solvable Tasks via job_complete Escape Hatch

**Observed in:** Job `aab9a1a2-bfc1-4748-987d-edb6f8e648ed` ("Organize docs (oss 120b)")
**Model:** `gpt-oss-120b`
**Date:** 2026-02-15
**Status:** Needs fix

## Summary

The agent spent 6 phases (480 audit entries, 220 tool calls, 38 minutes) on a document organization task with ~92 input documents available. It produced zero deliverables, then called `job_complete` with 0.9-1.0 confidence claiming the work was done. It rationalized stopping by deciding the task description was too vague and that it needed "user clarification" — but no user feedback mechanism exists during execution.

## What the Agent Had Available

The job description was "Organize docs" and the agent received the project's entire `docs/` folder uploaded into `documents/docs/`:

- **55 markdown files** (active): architecture docs, issue trackers, research notes, feature designs
- **31 files in `done/`**: completed planning docs, migration guides, the project masterplan (102 KB)
- **6 PDF research reports**: prompt optimization, citation workflows, autonomous agents, cloud workspaces, etc.
- **6 GPU log files**: performance benchmarks
- **Total: ~92 files, ~12 MB** of content

The task was straightforward: organize these documents. The agent had everything it needed.

## Root Cause Analysis

Multiple independent failures compounded into a death spiral. Below is the full reconstruction from the 220-tool-call audit trail.

### Issue 1: Context Compaction Erased Critical Knowledge (Phases 1→3)

**Phase 1 (tactical, 5/5 todos completed)** did real work:
- Web-searched "autonomous AI agents overview" (step 83)
- Read documents in `documents/docs/`: `01_issues.md`, `advanced_websearch.md`, `metamodel-compliance-architecture.md`, `advanced_job_configuration.md`, PDFs (steps 42-54)
- Built a comprehensive `workspace.md` with Research Summary, Document Insights, and Task Requirements sections
- workspace.md grew to 5,553 bytes — well over the 50-line target

**Phase 2 (strategic)** then rewrote workspace.md for compaction. The rewrite stripped the Research Summary, Document Insights, and Task Requirements — the actual substance from Phase 1 — down to a few bullet points under "Key Decisions":
```
- Consolidated research findings into a concise summary in Phase 1.
```

When context compaction later ran (step 479, 117→11 messages), the detailed knowledge from Phase 1 was gone from both messages AND workspace.md. The agent had amnesia.

**Phase 3 (tactical)** started with todos that referenced content that no longer existed:
- Todo 1: "Web search for up-to-date information on **the core topic**" — but workspace.md no longer said what the topic was
- Todo 2: "Read relevant source documents from **sources/**" — `sources/` was always empty; documents were in `documents/docs/`

The agent searched for "core topic" **7 times** (steps 264, 270, 278, 280, 284, 286, 288) — each time getting "No matches found." It never thought to re-read the documents in `documents/docs/` because it no longer remembered they contained anything useful.

### Issue 2: Wrong Directory in Todo Templates (Phase 3 & 5)

The tactical todos repeatedly referenced `sources/` as the document location:
```
"Read relevant source documents from sources/ and summarize key points"
```

But documents were actually in `documents/docs/`. The `sources/` directory was never populated — it doesn't even exist. The agent created todos based on the generic template in `instructions.md` (line 79: `read_file(path="sources/document.pdf")`) rather than from its Phase 1 discovery that documents lived in `documents/docs/`.

When Phase 3 checked `sources/`:
```
[128] list_files | path="sources" → No files found in: sources
```

This became the second pillar of the "can't proceed" narrative.

### Issue 3: search_files Bug Reinforced False Beliefs (Steps 205, 207, 445, 447)

At steps 205 and 207 (phase 6), the agent searched for `"output/"` with `path="instructions.md"`:
```
[205] search_files | query="output/", path="instructions.md" → No matches found
[207] search_files | query="output/", path="instructions.md" → No matches found
```

But `instructions.md` contains "output/" on **9 separate lines** (lines 30, 39, 110, 114, 117, 118, 119, 122, 123). The bug: `WorkspaceManager.search_files()` calls `search_path.rglob("*")` on the resolved path. When `path` is a file (not a directory), `rglob("*")` yields nothing, so the search silently returns zero results.

**Impact:** The agent used these false negatives to conclude "No required output files were specified in instructions.md" — the final rationalization for calling `job_complete` with empty deliverables at step 475, confidence 1.0.

**Location:** `src/core/workspace.py:585` — `for file_path in search_path.rglob("*"):` should check if `search_path` is a file first.

### Issue 4: Repetitive Tool Calls Without Progress (Throughout)

The audit trail shows severe repetition patterns:

| Pattern | Count | Steps |
|---------|-------|-------|
| `search_files("core topic")` | 7 | 264, 270, 278, 280, 284, 286, 288 |
| `search_files("output/")` | 8+ | 83, 151, 170, 246, 250, 257, 346, 447, ... |
| `search_files("Required Output Files")` | 5 | 203, 205, 207, 363, 695 |
| `search_files("deliverable")` | 5 | 199, 246, 346, 415, 477 |
| `read_file("todo_guide.md")` | 7 | 106, 136, 178, 294, 312, 349, 351 |
| `read_file("instructions.md")` | 6 | 4, 65, 124, 150, 196, 334 |

The model re-searched the same queries across phases because context compaction erased the previous results. There is no deduplication or "you already searched this" mechanism.

### Issue 5: Self-Reinforcing Blocker Narrative (Phases 3→6)

Once the agent concluded "core topic is undefined" in Phase 3 (step 137), it wrote this to workspace.md as "Critical Context":
```
- Core topic for the deliverable is undefined; no user clarification received.
- sources/ directory is empty; no source documents available.
```

Because workspace.md is injected into every LLM call, this blocker assertion was **re-read on every turn** for the rest of the run. Each strategic phase dutifully reviewed it and re-confirmed the blocker. The agent never challenged its own premises because the workspace memory kept reinforcing them.

**Phase 5 (tactical)** received todos to write deliverables but had the blocker narrative baked in. It did 13 `search_files` calls looking for outlines and research summaries that no longer existed, then used `todo_rewind` after completing 0/5 todos. This produced the Phase 5 Retrospective:
```
The tactical phase completed without any work, confirming that the prior
strategic decisions correctly halted execution.
```

The agent treated its own inaction as *validation* of the blocker.

### Issue 6: job_complete Accepted Empty Deliverables

At step 369, the agent first tried `job_complete` but was **correctly rejected** because it still had staged todos:
```
ERROR: Cannot mark job as complete - you have staged todos for the next phase.
```

At step 470, after the staged todos were consumed in the empty Phase 5, `job_complete` succeeded:
```json
{
  "summary": "Completed strategic planning, reflection, and adaptation phases.",
  "deliverables": "[]",
  "confidence": 0.9
}
```

At step 475, called again with confidence bumped to 1.0:
```json
{
  "notes": "No required output files were specified in instructions.md;
            therefore no deliverables are pending."
}
```

This claim — "no required output files were specified" — was directly caused by the search_files bug (Issue 3) returning false negatives when searching `instructions.md`.

### Issue 7: Strategic Phase Todos Are Identical Templates

Every strategic phase (2, 4, 6) used the exact same 4-todo template:
1. REVIEW (git evidence)
2. REFLECT (rewrite workspace.md)
3. ADAPT (update plan.md)
4. PLAN OR COMPLETE

These are loaded from `config/templates/strategic_todos_transition.yaml`. The PLAN OR COMPLETE todo contains a stop condition that checks for deliverables in `output/`. When there are none, it's supposed to create new tactical todos. But if the agent has convinced itself it CAN'T create tactical todos (because "core topic is undefined"), the only exit is `job_complete` — even though nothing was produced.

The template doesn't account for the "stuck in a loop with no progress" scenario. Todo 4 is binary: either call `job_complete` or `next_phase_todos`. There's no third option like "request help" or "attempt with best interpretation."

## Full Phase Timeline

| Phase | Type | Todos | Completed | Key Actions |
|-------|------|-------|-----------|-------------|
| 0 | Strategic | 4 | 4/4 | Read instructions, set up workspace, created plan, staged Phase 1 |
| 1 | Tactical | 5 | 5/5 | Web search, read 6+ documents, built comprehensive workspace.md (5.5 KB) |
| 2 | Strategic | 4+4 | 8/8 | Two rounds. Rewrote workspace.md (lost Phase 1 content). Staged Phase 3 with wrong `sources/` reference |
| 3 | Tactical | 5 | 1/5 | Searched "core topic" 7 times. Wrote blocker to workspace.md. `todo_rewind` |
| 4 | Strategic | 4 | 4/4 | Wrote retrospective. Tried `job_complete` (rejected — staged todos). Staged Phase 5 |
| 5 | Tactical | 5 | 0/5 | 13 `search_files` calls finding nothing. `todo_rewind`. 0 work done |
| 6 | Strategic | 4 | 4/4 | `search_files` bug on instructions.md. `job_complete` ×2. Job frozen |

## Concrete Issues to Fix

### Bug: search_files Silently Returns Empty on File Paths

**File:** `src/core/workspace.py:585`
**Problem:** When `path` parameter points to a file (not directory), `search_path.rglob("*")` yields nothing.
**Fix:** Check `search_path.is_file()` and search within it directly instead of recursing.
**Impact:** The agent concluded "no output files specified in instructions.md" based on this false negative, which directly caused the final `job_complete` with empty deliverables.

### Design: workspace.md Compaction Loses Critical Knowledge

**Problem:** The strategic REFLECT todo tells the agent to rewrite workspace.md to under 50 lines. After Phase 1 built a 5.5 KB workspace with research findings, the Phase 2 rewrite stripped it to generic bullets. When context compaction later ran, the agent had no way to recover the specific knowledge.

**Fix options:**
- Allow workspace.md to be longer (100-150 lines) for content-heavy tasks
- Add a `workspace_archive.md` for detailed findings that survive compaction but aren't injected every turn
- The REFLECT template should say "preserve domain knowledge and file locations; only remove process status"

### Design: Tactical Todos Reference Non-Existent `sources/` Directory

**Problem:** The `instructions.md` template uses `sources/document.pdf` as an example path. The agent (especially after context loss) takes this literally and creates todos referencing `sources/`. Documents are actually in `documents/`.

**Fix:** Update `instructions.md` to reference `documents/` consistently, or better yet, include the actual document inventory in the initial workspace setup (Phase 0) in a way that survives compaction.

### Design: No Circuit Breaker for Planning Loops

**Problem:** The strategic template is binary — `job_complete` or `next_phase_todos`. When the agent believes it can't do either (no deliverables for `job_complete`, no viable work for `next_phase_todos`), it loops through empty tactical phases until it gives up.

**Fix options:**
- Add a `request_clarification` tool that freezes the job with a specific question — distinct from `job_complete`
- Inject a system nudge after 2+ tactical phases with 0 deliverables: "You have completed N phases without producing output files. Attempt the task using your best interpretation of the requirements. The documents in documents/ ARE your source material."
- Add stall detection in `handle_transition`: if the last N tactical phases completed 0 todos or produced 0 files, inject a corrective message

### Design: job_complete Should Validate Deliverables

**Problem:** `job_complete` accepted empty deliverables with 0.9 confidence. The only existing guard (staged todos check at step 369) was bypassed after the empty Phase 5 consumed them.

**Fix:** `job_complete` should warn or require explicit `no_deliverables_reason` when the deliverable list is empty. Check `output/` directory for actual files and compare against the claimed list.

### Design: No Deduplication of Repeated Searches

**Problem:** The agent executed `search_files("core topic")` 7 times, `search_files("output/")` 8+ times, and `read_file("todo_guide.md")` 7 times. Each repetition burned tokens and iteration budget without new information.

**Fix options:**
- Cache recent search results in state and return "You searched this N turns ago. Previous result: ..."
- Track per-phase tool call patterns and warn on excessive repetition
- This is partially a model quality issue (oss-120b) but the framework should still guard against it

### Design: Blocker Persistence in workspace.md Creates Feedback Loops

**Problem:** Once the agent wrote "core topic is undefined" to workspace.md Critical Context, this was injected into every subsequent LLM call, reinforcing the false belief. The strategic REFLECT step rewrites workspace.md but preserves Critical Context because it's "important."

**Fix:** Critical Context items should have a TTL or be automatically challenged after N phases. Alternatively, the REFLECT template should include: "Re-evaluate all items in Critical Context. If a blocker has persisted for 2+ phases without resolution, consider whether the premise is wrong."
