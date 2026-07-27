---
tags:
  - issue
  - fix-spec
  - agent
  - skills
  - config-resolution
  - phases
---

# A bound skill missing from the resume config blob deadlocks phase transitions — the `next_phase_todos` gate can never be satisfied

**Filed:** 2026-07-27, from job `52949749` on dev.
**Status:** Delivery-path defect CONFIRMED in code + live incident
2026-07-25. The exact hydration step that drops the content is **not yet
root-caused** (pointers below). UNFIXED.
**Severity:** **high** — hard deadlock: the agent cannot transition phases and
burns iterations in a fail-loop until a human copies a file into the
workspace.
**Component:** `src/agent.py` (~3140–3160; warning at **3149**),
`src/core/loader.py:4996`, `config/worker_base.yaml:198–201`.

## Symptom

A resumed agent logged:

```
16:34:16.960 WARNING Bound skill content missing from blob: todo-guide   (agent.py:3149)
```

`skills/todo-guide/SKILL.md` was therefore never written, and the agent then
looped (audit entries 964–976, 16:38:07 → 16:39:08):

```
next_phase_todos → "You must read `skills/todo-guide/SKILL.md` before using next_phase_todos."
read_file        → "Error: File not found: skills/todo-guide/SKILL.md"
next_phase_todos → …same refusal… (repeat)
```

It only escaped when the file was hand-copied into the workspace.

## Root cause — a mandatory gate with a single, un-fallbacked delivery path

The skill is bound as a **hard gate** (`config/worker_base.yaml:198–201`):

```yaml
instruction_files:
  - skill: todo-guide
    trigger: before_tool:next_phase_todos
    enforce: true      # passive: tool rejects until the agent reads the file
```

Delivery is single-path *by design* (`src/agent.py:3142–3160`):

```python
if entry.skill:
    # Bound skill: content from the (flag-independent) instructions channel …
    # The catalog materialization path (Slice 2) is filtered out for bound
    # skills, so this is the single delivery path.
    content = resolved_instructions.get(entry.skill)
    if not content:
        logger.warning(f"Bound skill content missing from blob: {entry.skill}")
        continue                     # ← silently skipped, no fallback
```

and the agent deliberately does no disk/DB resolution when a blob is
delivered (`agent.py:294`: *"No disk or DB resolution happens here"*).

So a gap in the blob is **unrecoverable at runtime**: the gate file can never
appear, and `enforce: true` means the tool refuses forever. The failure mode
of a missing gate file is a permanent block, not a degraded run.

## What is proven vs. what is not

**PROVEN — the content exists in the database.** For this job,
`jobs.resolved_config->'instructions'` has keys:
`todo-guide, instructions, cite-as-you-write, verify-before-done,
workspace_template, strategic_todos_resume, strategic_todos_initial,
strategic_todos_transition`.

**PROVEN — at runtime on the resume it was absent** from
`config.extra["_resolved_instructions"]` (the warning fires only on a falsy
lookup).

**NOT ESTABLISHED — which hydration step drops it.** Investigation pointers:

- The resume logs `Loaded frozen config for resumed job` (`agent.py:1597`),
  then `Applying inline config override: ['llm', 'tools', 'scholar',
  'autonomy', 'verification', 'auxiliary', 'env_keys', 'workspace']` — note
  the override channel carries **no `instructions`** (DB `config_override`
  keys for this job: `llm, tools, scholar, autonomy, verification`).
- `load_config_from_resolved` seeds the runtime map at `loader.py:4996`:
  `config.extra["_resolved_instructions"] = resolved.get("instructions", {})`.
  Compare what the first-dispatch path passes in with what the frozen-config
  resume path passes in.
- Suspect the freeze → serialize → rehydrate round-trip: the same job used
  `next_phase_todos` successfully on first dispatch, so the content reached
  the agent then.

## Why this stayed hidden for three earlier resumes

On a **reattached** workspace, `skills/todo-guide/SKILL.md` from the first
dispatch is still on the volume, so the gate passes and the missing blob entry
is invisible. The defect only bites when the workspace is **fresh** — i.e.
only in combination with
`resume_fresh_workspace_no_clone_fallback.md`. This job survived three
resumes on a reattached volume and deadlocked on the fourth, when the volume
was gone. Any fix should assume the masking, and any test should exercise
*resume onto an empty workspace*.

## Fix proposal

1. **Fall back instead of `continue`.** When a bound skill is absent from the
   blob, resolve it from disk (`config/skills/<name>/SKILL.md`) or fetch it
   from the orchestrator. A stale gate file is strictly better than a missing
   one.
2. **Escalate the signal.** A bound skill with `enforce: true` that fails to
   deploy is not a WARNING that scrolls past — it should be an ERROR, and
   arguably a startup refusal, because the agent is now guaranteed to wedge.
3. **Make the gate fail open.** If the gate file cannot be materialized, the
   `before_tool` gate should admit the call with a note rather than blocking
   forever. Deadlock is worse than staleness.
4. **Root-cause the blob gap** on the resume path (pointers above) — items
   1–3 are containment; this is the actual defect.

## Related

- `docs/issues/resume_fresh_workspace_no_clone_fallback.md` — the companion
  defect that produces the fresh workspace this one needs to surface.
- `docs/issues/feedback_resume_restricted_closure_toolset.md` — a separate
  resume-path defect in the same family (what the agent is *allowed* to do
  after a resume).
- `docs/issues/maxsessions_parallel_tools_false_workspace_death.md` — the
  incident chain this was found in.
