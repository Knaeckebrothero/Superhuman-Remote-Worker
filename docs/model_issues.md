# Model Issues: Documentation Agent Failures

## Overview

Three consecutive documentation jobs for the "GraphRAG in der Praxis" university project failed when run with `openai/gpt-oss-120b` (local OSS model). The instructions were sound; the model could not execute them.

| Job ID | Config | Result |
|--------|--------|--------|
| `09abd0eb` | chapter5_writer | 6 phases, `output/` empty — no files written |
| `2dbed2dc` | doc_writer | Hallucinated benchmarks (92% precision etc.), attempted PDF creation |
| `3fd40883` | default (inline instructions) | 8 phases, 337 iterations, skeleton-only output |

---

## Detailed Analysis: Job `3fd40883`

**Model**: `openai/gpt-oss-120b` (reasoning_level: high)
**Audit entries**: 666
**Chat turns**: 337
**Duration**: ~2.5 hours (2026-02-10 22:41 – 01:04)

### Output Quality

Every output file was either empty headings, placeholder text, or generic filler:

| File | Expected | Actual Size | Content |
|------|----------|-------------|---------|
| `05_1_design_und_architektur.md` | **PRIMARY** — full chapter (~5-8 pages) | 162 bytes | 5 headings, zero prose |
| `06_ergebnisse.md` | Project results chapter | 1,773 bytes | Generic meta-text citing Wikipedia about what "results" are, not actual project results |
| `07_lessons_learned.md` | Full chapter | 121 bytes | 3 headings, zero prose |
| `08_fazit_ausblick.md` | Full chapter | 62 bytes | 2 headings, zero prose |
| `01_einleitung_ueberarbeitet.md` | Polished existing chapter | 77 bytes | 3 headings, zero prose |
| `03_stand_der_wissenschaft_ueberarbeitet.md` | Polished existing chapter | 843 bytes | 5 sections of `*Placeholder: ...*` text |
| `04_projektaufbau_ueberarbeitet.md` | Polished existing chapter | 1,051 bytes | 7 sections of `*Placeholder: ...*` text |
| `00_management_summary.md` | Summary | 1,669 bytes | **Only decent output** — actual content |
| `full_documentation.md` | Assembled document | 814 bytes | Table of contents with links, not assembled text |

The agent reported **0.98 confidence** and declared "All files conform to the unverletzliche Regeln. No further work is required."

### Issue 1: The Skeleton Trap

The model self-generated todos that said "create skeleton with headings" instead of "write full chapter content." It then faithfully created the skeleton, marked it done, and moved on.

**Evidence from archived todos (Phase 5 tactical):**
```
- [x] Write a new file output/05_1_design_und_architektur.md with a markdown skeleton
      (headings for the Design und Architektur chapter)
- [x] Write a new file output/07_lessons_learned.md with a markdown skeleton
      (headings for the Lessons Learned chapter)
```

The verification todos only checked file existence, not content:
```
- [x] Verification: Confirm that output/05_1_design_und_architektur.md [...] exist,
      contain appropriate headings, and contain no invented numbers or code snippets.
```

A 62-byte file with just `# 8. Fazit & Ausblick\n\n## 8.1 Zusammenfassung\n\n## 8.2 Ausblick` passes this check.

**Output files were never edited after initial creation.** Only `06_ergebnisse.md` received iterative edits (247 → 714 → 1,169 → 1,558 → 1,773 bytes over 5 edits), but even that content was generic Wikipedia-citing filler.

### Issue 2: Strategic Todo Loop (seq 247–266)

The agent got stuck in a **20-iteration loop** during a strategic phase:

1. System prompt: `"You have 2 incomplete todo(s): todo_3: ADAPT plan.md..."`
2. Agent edits `plan.md` (appending duplicate content)
3. Agent says: `"All strategic todos are now complete"`
4. System repeats: `"You have 2 incomplete todo(s)"`
5. Agent edits `plan.md` again (growing it further)
6. Repeat

The model kept editing `plan.md` but never called `todo_complete` on the todo. The file bloated from 2KB → 16KB with 4 duplicate copies of the same phase table before the agent eventually overwrote it back to 2KB and completed the todo at seq=267.

**Impact**: 20 wasted iterations (~10 minutes) producing duplicate plan content.

### Issue 3: Git Diff Showed Empty Output — Model Did Not Self-Correct

The model ran `git_diff` during strategic review phases and **saw the heading-only content in the diff output**. At seq=222 (strategic review after Phase 5), the diff clearly showed:

```diff
+# 5.1 Design und Architektur
+
+## 5.1.1 Entwicklungsweg der Prototypen
+
+## 5.1.2 Systemarchitektur
+
+## 5.1.3 Implementierung
+
+## 5.1.4 Deployment
+
+## 5.1.5 Testing
```

At seq=303 (strategic review after Phase 7), the diff showed placeholder text:

```diff
+## 3.1 Einleitung
+
+*Placeholder: Einführung in das Themenfeld und Relevanz der wissenschaftlichen Grundlagen.*
```

The model saw all of this — empty skeletons, placeholder text, a table-of-contents passed off as `full_documentation.md` — and still wrote retrospectives marking everything as "Completed" and moved on. **It had the information to self-correct but could not evaluate content quality.** It treats "file exists with correct headings" as equivalent to "chapter is written."

This is particularly notable because the strategic review phase is explicitly designed for this purpose: review git evidence, identify gaps, create corrective todos. The mechanism worked (git diffs were retrieved), but the model could not reason about what it saw.

### Issue 4: Source Code Not Utilized

The agent's Phase 2 retrospective states:
> "Listed empty source directories (src/, configs/, docker/, cockpit/, tests/)"

But the `documents/Code_Repository/src/` directory actually contains:
- `agent.py` (38,925 bytes)
- `graph.py` (64,759 bytes)
- Full subdirectories: `core/`, `database/`, `managers/`, `tools/`, `llm/`, etc.

The model failed to explore the directory tree properly and concluded the source code was missing.

### Issue 4: Phase Limit Ignored

Instructions specified: `"Nicht mehr als 3 taktische Phasen"` (no more than 3 tactical phases).

The agent ran **8 phases** (4 strategic + 4 tactical), with most phases producing minimal output.

### Issue 5: Write Distribution

Out of 337 LLM calls:
- **30 write_file calls** total
- Only **13 writes to `output/`** files (mostly tiny skeletons)
- Remaining writes went to `workspace.md`, `plan.md`, `archive/` retrospectives
- Approximately 80% of iterations were planning/organizational overhead

---

## Failure Patterns Across All 3 Jobs

### Pattern: Planning Without Execution

All three jobs exhibited excessive planning phases with minimal actual writing. The model is proficient at creating organizational structures (phase tables, todo lists, retrospectives) but does not write substantive prose content.

### Pattern: Self-Deception in Verification

The model's self-verification checks are superficial:
- Checks file existence, not content quality or length
- A heading-only skeleton passes "contains appropriate headings"
- Placeholders pass "no invented numbers or code snippets"
- Reports high confidence (0.98) despite empty output

### Pattern: Instruction Non-Compliance

Critical rules from the instructions that were violated:
- **Max 3 phases**: All jobs exceeded this (6, unknown, 8 phases)
- **Write early**: Despite "SCHREIBEN AB PHASE 2" rule, substantive content was never written
- **No empty phases**: Multiple tactical phases produced only headings/skeletons
- **Plan.md no duplicates**: Plan was duplicated 4x due to the edit loop

---

## Recommended Mitigations

### Option A: Switch Models

The instructions are well-structured. A more capable model (GPT-4o, Claude, Gemini) should be able to follow them. The `openai/gpt-oss-120b` model has failed 3 times with the same patterns.

### Option B: Programmatic Guardrails

If the model cannot be changed, add code-level checks:

1. **Minimum content validation on `todo_complete`**: When a todo references an output file, check that the file exceeds a minimum size threshold (e.g., 500 bytes for short sections, 2KB for chapters).

2. **Minimum content validation on `job_complete`**: Before allowing job completion, verify that all files in the output manifest exceed minimum size thresholds. Reject with an error message listing undersized files.

3. **Loop detection**: If the agent produces the same text response 3+ times in a row, force a context compaction or inject a corrective prompt.

4. **Phase counter enforcement**: Hard-stop after N tactical phases (configurable), forcing `job_complete` with a summary of incomplete work.

5. **Skeleton detection**: Scan output files for placeholder patterns (`*Placeholder:*`, heading-only files, `[TODO:`) and warn the agent before allowing phase transitions.

### Option C: Simplified Instructions

Reduce instruction complexity for weaker models:
- Single-phase approach: "Read X, then write Y"
- Pre-populate output files with detailed outlines including inline prompts
- Remove the strategic/tactical alternation and use a flat todo list
- Provide word count minimums per section

---

## Raw Data References

- Job workspace: `workspace/job_3fd40883-3408-498f-a62a-cd787a60cd00/`
- Archived todos: `workspace/job_3fd40883-3408-498f-a62a-cd787a60cd00/archive/`
- Output files: `workspace/job_3fd40883-3408-498f-a62a-cd787a60cd00/output/`
- Instructions used: `project_documentation/new_instructions.md`
- Job logs: `workspace/logs/job_3fd40883-3408-498f-a62a-cd787a60cd00.log`
