---
tags:
  - issue
  - officers
  - datasources
  - jobs
status: open
priority: P2
created: 2026-08-16
aliases:
  - datasource_ids [] de-arms the worker
  - omitted_compat propagates an empty selection
related:
  - "[[cloned_repo_checkout_cannot_reach_non_default_refs]]"
  - "[[deliverable_contract_satisfied_by_a_note_about_failure]]"
  - "[[srw_grants_two_read_paths]]"
---

# An officer can commission a worker with no connectors, two different ways, without noticing

**Status:** OPEN as a latent trap. The observed instance **resolved itself** on the
officer's respawn (2026-08-16 08:20) — but neither cause was fixed, so it can recur.

## Observed

Better Resavio, overnight run. Two commissioned jobs reached their workers with
zero connectors attached:

| job | `datasource_selection.origin` | ids | `job_datasources` rows | outcome |
|---|---|---|---|---|
| `29c28492` (2026-08-14, manual) | explicit | KB + KurortEngine | 2 | opened PR #1 |
| `2fbe1f99` (officer) | explicit | `[]` | **0** | no repo, no repo tools |
| `fcda6532` (officer) | explicit | `[]` | **0** | no repo, no repo tools |
| `c4849fa1` (after respawn) | default | KB + KurortEngine | 2 | repo present |

Without a repository connector the worker gets no checkout **and no
`repo_clone` / `repo_commit` / `repo_push` / `repo_open_pr` tools** — those are
granted by the connector (`src/core/datasource_setup.py:130` read, `:136` write).
The worker reported exactly that, and it was correct.

## Two independent paths to empty

**1. The officer passed `[]` explicitly.** Verbatim from the `create_job` tool call
at 2026-08-15T20:16:51:

```
datasource_ids: []
```

`orchestrator/main.py` distinguishes this from omission on purpose:

```python
# not truthiness — distinguishes an explicit [] from omission.
selection_was_supplied = "datasource_ids" in job.model_fields_set
```

The tool docstring documents the tri-state correctly ("Omit to inherit …; pass []
to attach none"). But nothing in the centurion persona or config mentions
connectors at all — `grep -ri "datasource\|connector" config/experts/centurion/`
returns nothing. An officer that fills in every parameter it is shown will send
`[]`, and that silently removes the worker's ability to do repository work.

**2. Omitting the field would have failed too.** Because the job carries a
`thread_id`, omission routes to the `inherited` branch, which reads the officer
thread's own selection — and that is also empty:

```
threads.metadata.datasource_selection = {
  "origin": "omitted_compat", "creation_path": "persistent_thread_rest",
  "datasource_ids": []
}
```

Inheritance treats presence as authoritative (`orchestrator/main.py:5913`):

> Presence is authoritative, including an explicit empty list. Falling through on
> `[]` would resurrect the parent job's connectors and make a deliberate opt-out
> impossible.

That rule is right in itself. The consequence is that **a session created with
`omitted_compat` sterilises every job it will ever commission**, and the officer
has no way to see that from inside.

## Why it matters

The failure is invisible from every surface the officer can read. The job is
created successfully, the project shows the connector attached and auto-attach on,
and the worker simply lacks tools it never knew it should have had. The officer
diagnosed it correctly only because the worker reported its own missing toolset.

The self-heal makes it worse, not better: the same officer, same project, produced
both shapes within twelve hours, so the failure is intermittent and will not be
reproducible on demand.

## Direction

- **Teach the persona about connectors** — an officer cannot arm what it does not
  know exists, and cannot be blamed for a parameter it was never briefed on.
- **Refuse rather than sterilise.** A commission whose ticket or deliverables name
  a repository, submitted with zero repository connectors, should be refused at
  creation with the reason — the same shape as the `repos/…` deliverable refusal.
- **Surface the effective connector set in the SITREP.** "build 0/1 BELOW FLOOR"
  told the officer about capacity and nothing about capability.

## Acceptance

- An officer commissioning repository work either gets connectors or gets a refusal
  naming the missing one.
- A session's own empty selection cannot silently become every child job's.
