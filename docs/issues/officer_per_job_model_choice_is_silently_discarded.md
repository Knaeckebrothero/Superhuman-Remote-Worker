---
tags:
  - issue
  - officers
  - jobs
  - config
status: open
priority: P2
created: 2026-08-16
aliases:
  - slot pinning overrides the officer
  - commissioned model is not the model that runs
related:
  - "[[officer_commission_can_silently_de_arm_its_workers]]"
  - "[[session_agent_job_config_override]]"
  - "[[officer_roster_patch_cannot_remove_or_drain_a_slot]]"
---

# The model an officer commissions is not the model that runs, and it is never told

**Status:** OPEN. Observed on both overnight commissions, Better Resavio,
2026-08-15.

## Observed

| job | officer asked for | job actually ran |
|---|---|---|
| `2fbe1f99` | `gpt-5.3-codex-spark` | `MiniMax-M3`, `backend: sandbox` |
| `fcda6532` | `gpt-5.6-sol` | `MiniMax-M3`, `backend: sandbox` |

Both `create_job` calls carried an explicit `config_override`. Both jobs persisted
a different one.

The source is the officer post's own roster
(`project_officers.config_override.officer.slots`):

```json
"build":    {"count": 1, "model": "MiniMax-M3", "backend": "sandbox", "category": "executor"},
"research": {"count": 1, "model": "MiniMax-M3", "backend": "sandbox", "category": "researcher"},
"test":     {"count": 1, "model": "MiniMax-M3", "backend": "sandbox", "category": "tester"}
```

Slot pinning is deliberate and correct — a roster slot describes a worker pool, and
the pool's model is a property of the pool. The defect is that the officer's
explicit choice is **discarded without a word**. The `create_job` result read
`Job created successfully` with no mention that two of the parameters it passed
had been replaced.

## Why it matters

The officer reasons about model choice as a real lever — it picked a codex model
for a build task and a reasoning model for a research task, which is exactly the
judgement the role is for. Because the substitution is silent, that reasoning is
unfalsifiable from inside the loop: the officer will keep spending tokens deciding
something that has no effect, and will attribute the resulting work quality to a
model that never ran.

It also breaks post-hoc analysis. A reader comparing "what the officer chose" to
"what the job produced" is comparing against the wrong model unless they know to
check the roster.

## Direction

Pick one, either is fine:

- **Refuse**: reject a commission whose `config_override` contradicts the slot, with
  a message naming the slot's pinned values. Costs the officer one retry and teaches
  it the roster is authoritative.
- **Report**: accept the slot's values but say so in the `create_job` result — "slot
  `build` pins model=MiniMax-M3 backend=sandbox; your llm.model was not applied".

What must not continue is silent replacement. If the roster is authoritative, the
`config_override` model parameter should not be reachable from a commission at all.

## Acceptance

- An officer either cannot set a per-job model that the slot will override, or is
  told at commission time that it did not take effect.
