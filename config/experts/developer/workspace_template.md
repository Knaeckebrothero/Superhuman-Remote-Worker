# Workspace Memory

This file is your persistent memory. It survives context compaction and is always in your system prompt.

**COMPACT, don't append.** Rewrite sections to remove redundancy. Target: under 4000 tokens (~80 lines).

**Don't duplicate plan.md.** Phase status and completion tracking belong in plan.md. The spec lives in spec.yaml; only the locked AC block is mirrored here.

## Pinned Instructions

Rules from instructions.md and task_brief.md that must persist across context compaction.
Extract and place here during the spec phase.

(PROTECTED — preserve verbatim during workspace rewrites unless provably wrong.)

## Acceptance Criteria

(PROTECTED — copied verbatim from spec.yaml at the end of the spec phase. Do NOT rewrite to match what was built. If an AC turns out to be wrong, emit `ABORT:` and revise in a deliberate strategic phase, recording the reason via kb_write type=decision tag=spec-revision.)

```yaml
feature: <kebab-case-id>
intent: "<one-sentence purpose>"
acceptance_criteria:
  - id: AC-1
    ears: "<EARS statement>"
    test_oracle: tests/<file>::<test_name>
  # ... more AC ...
not_included:
  - "<scope boundaries>"
done_when:
  - "<exact command>"
```

Spec hash (sha256 of spec.yaml at lock time): `<fill in during spec phase>`

## Traceability Matrix

Status of each AC. Update after every tactical phase.

| AC ID | Test Oracle                        | Status (not_started / red / green / refactored / blocked) | Notes |
|-------|------------------------------------|-----------------------------------------------------------|-------|
| AC-1  | tests/<file>::<test_name>          | not_started                                               |       |

Rules:
- Every AC in `## Acceptance Criteria` MUST have a row here.
- A test with no AC parent is scope creep — add the AC (via deliberate revision) or remove the test.
- An AC marked `blocked` MUST have a corresponding kb_write blocker note (type=state, tag=blocker).

## Repository

Framework, conventions, and key paths discovered during exploration:
- **Stack**: (language, framework, version)
- **Test framework**: (pytest, vitest, jest, etc.)
- **Test command**: (e.g., pytest tests/ -x -v, npm test)
- **Lint command**: (e.g., ruff check, eslint)
- **Source dir**: (e.g., repo/src/, repo/app/)
- **Test dir**: (e.g., repo/tests/, repo/test/)
- **Entry points**: (key files relevant to the task)
- **Conventions**: (naming, patterns, style observed)

(Update as you discover more. Keep compact.)

## Key Decisions

Architectural decisions AND their reasoning. Without the WHY, you may revisit unnecessarily.

(Keep only decisions that affect future work. Remove resolved ones.)

## Status

Current position (update each strategic phase):

- **Phase**: (name from plan.md)
- **tdd_phase**: (spec / red / green / refactor / integration — current or next)
- **TDD cycle position**: (e.g., "AC-1, AC-2 at red; AC-3 not_started")
- **Branch**: (current working branch)
- **Blocked**: (active blockers, or "none")

(This section can be freely rewritten during strategic phases.)

## Failed Approaches

Approaches that were tried and did NOT work, with the reason. This prevents retrying the same failed strategy after context compaction.

(Example: "Tried mocking the database connection in the AC-2 test — produced a test that always passed because the mock was over-broad. Switched to a real in-memory SQLite fixture.")

(PROTECTED — only remove entries when the underlying issue is confirmed resolved.)
