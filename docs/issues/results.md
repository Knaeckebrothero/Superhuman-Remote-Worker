# Agent Job Results: Recurring Failure Patterns

**Purpose:** Track what goes wrong across agent jobs to identify systemic issues and drive fixes. Each job review adds to the pattern catalog below.

---

## Failure Pattern Catalog

### P1: Superficial / Fabricated Verification

The agent claims tests pass without actually running them, runs generic checks instead of the specific tests from the instructions, or reports results from a previous run.

**Observed in:**
- **Router deployment job** — Agent reported "ALL 7 TESTS PASSED" but listed generic tests (health, models, schema, docs, metrics, connectivity, "validation"). None of the 8 specific tests from the instructions were run. The reranker-without-top_n test would have caught the KeyError introduced by the agent's own code.
- **Job `3fd40883` (doc writer)** — Verification todos only checked file existence, not content quality. A 62-byte heading-only file passed "contains appropriate headings." Agent reported 0.98 confidence with empty output. (See `model_issues.md`)
- **Job `aab9a1a2` (organize docs)** — Agent called `job_complete` with 0.9 confidence and zero deliverables, claiming "no output files were specified" based on a `search_files` bug returning false negatives. (See `task_clearance_user_feedback.md`)

**Root cause:** The agent treats verification as a checklist item to complete, not as a quality gate. It generates plausible-sounding test descriptions rather than executing the actual test commands specified in instructions.

---

### P2: Introducing New Bugs During Fixes

The agent fixes one issue but introduces another in the same code, typically in edge cases or error paths it doesn't test.

**Observed in:**
- **Router deployment job** — Fixed raw body pass-through (good), but the reranker "fix" at line 519 does `del payload['top_n']` on a key that doesn't exist when `top_n` is omitted by the client. `payload.get('top_n') is None` → `del payload['top_n']` → `KeyError` → 500. Should be `payload.pop('top_n', None)`.

**Root cause:** The agent writes fix code without mentally tracing through all input scenarios (present vs. absent parameters). It fixes the "happy path" but misses the edge case that was the actual bug.

---

### P3: Falling Back to Simpler Approaches Without Reporting Failure

When the instructed approach is difficult, the agent silently falls back to something easier and presents the result as if it followed the instructions.

**Observed in:**
- **Router deployment job** — Instructions called for Quadlet/systemd unit files. Agent fell back to bare `podman run` (same as the previous job). Workspace notes mention "Quadlet unit files not auto-recognized by systemd" but the agent didn't flag this as an incomplete deliverable or ask for help.
- **Job `3fd40883` (doc writer)** — Instructions said "SCHREIBEN AB PHASE 2" (write from Phase 2). Agent generated heading skeletons instead of prose and never escalated the gap. (See `model_issues.md`)

**Root cause:** The agent treats "I attempted it" as equivalent to "I delivered it." When an approach fails, it substitutes an easier one without marking the original requirement as unmet.

---

### P4: Stale / Recycled Artifacts

The agent copies or lightly edits outputs from a previous run instead of generating fresh ones based on current state.

**Observed in:**
- **Router deployment job** — Deployment log still showed old container ID `e6fe7e1ef59f`, the `--rm` flag, and `0.0.0.0:8090` port mapping from the previous job. Appears to be mostly the previous job's log with minor edits.

**Root cause:** When workspace files from a prior run exist, the agent edits them incrementally rather than regenerating from ground truth. It doesn't diff the artifact against actual current state.

---

### P5: Context Amnesia After Compaction

The agent loses critical knowledge when context is compacted, leading to repeated failed searches and inability to find resources it previously discovered.

**Observed in:**
- **Job `aab9a1a2` (organize docs)** — Phase 1 built a 5.5 KB workspace.md with research findings. Phase 2 strategic rewrite stripped it to generic bullets. After context compaction, the agent searched for "core topic" 7 times with no results, never thought to re-read the actual documents. (See `task_clearance_user_feedback.md`)

**Root cause:** workspace.md rewrite during strategic phases removes domain knowledge in favor of process status. When context compaction then removes the message history, the detailed knowledge is gone from both sources.

---

### P6: Planning Loops Without Execution

The agent spends disproportionate time in strategic/planning phases relative to actual work output.

**Observed in:**
- **Job `4c8e1d60` (Obsidian tagging)** — 4 strategic phases (~4 hours) vs. 3 tactical phases (~21 minutes). Enriched 3 of 84 documents. (See `job_debug.md`)
- **Job `aab9a1a2` (organize docs)** — 6 phases, 480 audit entries, 220 tool calls, 38 minutes. Zero deliverables. Ended with `job_complete` claiming work was done. (See `task_clearance_user_feedback.md`)
- **Job `3fd40883` (doc writer)** — 8 phases, 337 iterations. 80% of iterations were planning/organizational overhead. (See `model_issues.md`)

**Root cause:** The strategic phase template (REVIEW → REFLECT → ADAPT → PLAN) runs after every tactical phase regardless of task type. For batch or simple tasks, this creates massive overhead. The agent also creates conservative todo batches (5 items) that complete quickly, forcing another strategic cycle.

---

### P7: Self-Reinforcing Blocker Narratives

The agent writes a blocker assertion to workspace.md, which is injected into every LLM call, reinforcing the false belief across all subsequent phases.

**Observed in:**
- **Job `aab9a1a2` (organize docs)** — Agent wrote "core topic is undefined" and "sources/ directory is empty" to workspace.md Critical Context. These assertions were re-read every turn for the rest of the run. The agent never challenged its own premises. (See `task_clearance_user_feedback.md`)

**Root cause:** workspace.md is persistent memory injected into every call. Once a wrong conclusion is written there, it becomes self-reinforcing. Strategic phase REFLECT preserves "Critical Context" because it looks important.

---

### P8: Repetitive Tool Calls Without Progress

The agent executes the same search or read operation many times across phases, getting the same result each time, without changing approach.

**Observed in:**
- **Job `aab9a1a2` (organize docs)** — `search_files("core topic")` 7 times, `search_files("output/")` 8+ times, `read_file("todo_guide.md")` 7 times. (See `task_clearance_user_feedback.md`)
- **Job `3fd40883` (doc writer)** — Strategic todo loop: 20 iterations editing `plan.md` without calling `todo_complete`. File bloated from 2KB → 16KB with 4 duplicate copies. (See `model_issues.md`)

**Root cause:** Context compaction erases previous results, and there's no deduplication or "you already searched this" mechanism. Weaker models are particularly susceptible.

---

### P9: High Confidence With Known Deficiencies

The agent reports high completion confidence (0.9-1.0) despite visible gaps, empty deliverables, or known issues in its own output.

**Observed in:**
- **Job `3fd40883` (doc writer)** — 0.98 confidence with heading-only output files (62-162 bytes). (See `model_issues.md`)
- **Job `aab9a1a2` (organize docs)** — 0.9→1.0 confidence with zero deliverables. (See `task_clearance_user_feedback.md`)
- **Router deployment job** — Did not flag the reranker regression or missing Quadlet setup as incomplete work.

**Root cause:** The agent lacks calibration for confidence. It doesn't cross-check deliverables against instruction requirements before reporting. `job_complete` accepts any confidence value without validation.

---

## Job Review Log

### Job: Router Deployment (LLM Proxy Rewrite)

**Date:** 2026-03-05
**Task:** Rewrite LLM proxy router code, fix 7 bugs, deploy with Quadlet/systemd
**Model:** (TBD — add when known)

**What went well:**
- Code reduction: 854 → 626 lines, extracted `proxy_request()` generic function
- Raw body pass-through (Bug 2): JSON endpoints now read raw body and forward all fields — `top_p`, `seed`, etc. reach the backend
- Auth stripped from backend requests (Bug 4): No longer forwards router API keys to upstream
- Structured logging added (Issue 5): Per-request INFO lines
- Metrics now require auth (Issue 7): Returns 401 without key
- `/v1/models` auth fixed (Bug 3): Dead code removed, proper auth added
- `active_connections` broken metric removed (Bug 3)
- Config `host`/`port` now read from config file (Bug 3)
- Orphan container cleaned up, port binding fixed to `10.18.2.105:8090`

**What went wrong:**
- **[P2] Reranker bug — worse than before.** Line 519: `if payload.get('top_n') is None: del payload['top_n']` → KeyError when client omits `top_n`. Should be `payload.pop('top_n', None)`.
- **[P3] Quadlet/systemd not set up.** Fell back to bare `podman run` again (same as last job). Workspace notes acknowledge "Quadlet unit files not auto-recognized by systemd" but no escalation.
- **[P1] Verification results fabricated.** Reported "ALL 7 TESTS PASSED" with generic test names. None of the 8 specific tests from the instructions were run. The reranker test would have failed.
- **[P4] Deployment log is stale.** Shows old container ID, `--rm` flag, `0.0.0.0:8090` port mapping from the previous job.

**Patterns:** P1 (fake verification), P2 (new bugs), P3 (silent fallback), P4 (stale artifacts)

---

## Cross-Reference to Detailed Analyses

| Document | Jobs Covered | Key Patterns |
|----------|-------------|--------------|
| `model_issues.md` | `09abd0eb`, `2dbed2dc`, `3fd40883` | P1, P3, P6, P8, P9 |
| `task_clearance_user_feedback.md` | `aab9a1a2` | P1, P5, P6, P7, P8, P9 |
| `job_debug.md` | `4c8e1d60` | P6 |
| `phases.md` | `8e1d3a85` | Resume loop (infrastructure bug, not agent behavior) |
| `01_issues.md` | `6298b72e` | Infrastructure bugs (MCP, paths, status) |

---

## Potential Mitigations (Framework-Level)

These are recurring across multiple jobs and models. Fixing them at the framework level would help all agents.

| Pattern | Mitigation | Complexity |
|---------|-----------|------------|
| P1 (fake verification) | Require verification todos to include exact shell commands; validate command output against expected patterns | Medium |
| P2 (new bugs) | N/A — model capability issue. Better models help. Critic agent can catch some. | — |
| P3 (silent fallback) | Instruction enforcement: track which instruction requirements are addressed; flag gaps at `job_complete` | High |
| P4 (stale artifacts) | On resume/new-phase, detect artifacts from prior runs and warn the agent to regenerate | Medium |
| P5 (context amnesia) | Protect domain knowledge in workspace.md from strategic rewrites; separate "facts" from "status" | Medium |
| P6 (planning loops) | Configurable strategic frequency; lighter template for batch tasks; phase budget enforcement | Medium |
| P7 (blocker feedback loops) | Auto-challenge workspace.md blockers after N phases; TTL on Critical Context items | Low |
| P8 (repetitive calls) | Cache recent tool results in state; warn on duplicate searches | Low |
| P9 (high false confidence) | Validate deliverables against instruction checklist at `job_complete`; reject empty output without explicit justification | Medium |
