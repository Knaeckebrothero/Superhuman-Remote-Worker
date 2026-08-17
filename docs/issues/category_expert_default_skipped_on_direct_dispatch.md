---
tags:
  - issue
  - officer
  - experts
  - backlog
  - dispatch
status: open
priority: P1
created: 2026-08-17
aliases:
  - officer slots cannot name an expert
  - EXECUTOR defaults to developer but never applies
  - hand-dispatch skips the category expert default
related:
  - "[[experts_one_catalogue_two_selection_paths]]"
  - "[[officer_per_job_model_choice_is_silently_discarded]]"
  - "[[deliverable_contract_satisfied_by_a_note_about_failure]]"
---

# The category→expert default exists, staffs executors with `developer`, and never runs on hand-dispatch

**Status:** OPEN. Diagnosed 2026-08-17 against live dev.

## Read this first — the framing I filed this under originally was wrong

This ticket was going to be called "officer slots cannot name who staffs them", proposing an
`expert` key on `officer.slots`. **That would have broken the design on purpose.** The record
of the wrong idea is kept here so nobody re-derives it.

`orchestrator/services/work_categories.py` states the model in its own docstring:

```
category = a property of the WORK  — what shape the deliverable takes,
                                      what counts as evidence, when to stop
expert   = a property of the WORKER — what skills it brings
```

> The two are many-to-many on purpose. A developer given "compare rendering A vs B" is a
> *researcher* on that ticket: throwaway code, a decision note with numbers. The same developer
> given "implement the winner" is an *executor*. One expert, two contracts, because the
> contract belongs to the ticket.

A slot already pins a `category`. Pinning an expert to it as well would collapse that
many-to-many into 1:1 and re-create exactly the role-shaped thinking the module was written to
remove. **`officer.slots` is a capacity seat, not a job description. Leave it alone.**

## The actual defect

Expert assignment has a designed three-tier precedence — officer's explicit argument, then the
ticket's `expert:` pin, then the category default:

```python
# orchestrator/services/work_categories.py
CATEGORY_DEFAULT_EXPERT: dict[str, str] = {
    RESEARCHER: "scholar",
    TESTER:     "product-qa",
    EXECUTOR:   "developer",     # <- the shell-capable builder
}

def resolve_expert(classification):        # ~:236
    return classification.expert or default_expert(classification.category)
```

`KNOWN_EXPERTS` validates the pin before dispatch, deliberately, "rather than at agent boot,
where the failure would look like a job failure and count toward the pool's breaker".

All of it is implemented and correct. **It runs on exactly one path.** `classify_ticket` and
`resolve_expert` are consumed only by `orchestrator/services/officer_backlog.py:55-60`, and by
`orchestrator/main.py:12830`, which sits inside:

```python
if job.ticket:
    ...
    from services.work_categories import classify_ticket
```

No `ticket=` → no classification → no `resolve_expert` → no `CATEGORY_DEFAULT_EXPERT`. A job
created directly, stamped `slot="build"` and `work_category="executor"`, is not staffed by a
`developer`; it falls through to the application default and gets `general-worker`.

## Observed

Project `a572e4a0` (Better Resavio), every job the officer ever dispatched:

| | |
|---|---|
| jobs dispatched | 8 |
| dispatched with a `ticket` | **0** |
| resulting expert | `general-worker` on all 8, `expert_selection.source = "application"` |
| resulting `tools.shell` | `[]` on all 8 |

Three of those jobs were stamped `executor`. The map says an executor is a `developer`, which
resolves in the deployed image with `['run_command', 'cancel_command', 'shell_read']`. None of
them got it.

The module's opening paragraph describes the consequence before it happened:

> The loop spent a month producing tested Python and no UI. The cause was not a gate rejecting
> design work; it was that no design ticket was ever *proposed*.

The cure was designed, built and validated, and lives entirely on the path that is switched
off. `auto_pull` is deliberately disabled (see the P0 index row in `BACKLOG.md` — that
instruction is load-bearing for safety), and hand-dispatch is therefore the only mode in use.
So the officer runs in the single mode where categories, expert pins and expert defaults all
silently do nothing.

## Why this matters more than it looks

It reframes "turn auto-pull on" from a scaling decision into a correctness one. The
backlog-ticket path is not merely how the officer self-feeds work; it is **where the
expert-assignment intelligence lives**. Manual dispatch is not a safe subset of the system —
it is a different, dumber system that happens to share a `create_job` call.

## Direction

Two independent moves; do the first regardless.

1. **Apply the category default on the direct path.** When a job carries a work category
   (explicitly, or via its slot) and names no expert, resolve
   `default_expert(category)` before falling through to `application_expert_defaults`. That
   makes hand-dispatch behave like ticket dispatch and is a small change now that
   `expert` is a single parameter (`916d54d4`, `src/shared/expert_reference.py`).
   Decide explicitly where it sits relative to the application default — the category default
   should almost certainly win, because it is derived from the work rather than from a
   deployment-wide fallback.
2. **Reconsider the backlog path sooner than "later"** — not for autonomy, but because the
   design's intelligence is there. This is a scheduling decision for the operator, not a code
   change, and it is gated behind the open P0s.

## Acceptance

- A job dispatched with `work_category="executor"` and no expert resolves to `developer`, and
  its worker binds a shell.
- The same holds when the category arrives via `slot` rather than explicitly.
- A job that names an expert explicitly still wins over the category default.
- `officer.slots` gains **no** new key.

## Traps

- **Do not add `expert` to `_SPEC_KEYS`** (`orchestrator/services/officer_slots.py:43`). See
  the first section; the many-to-many is deliberate.
- `CATEGORY_EXPERTS` membership is warn-not-forbid by design — "the officer may dispatch
  outside it, and the mismatch is named in the kickoff rather than refused". Do not harden it
  into a rejection while wiring the default.
- Executors are serialized (`_SERIALIZED_CATEGORIES`) because they write shared project state.
  Anything that increases executor throughput must respect that.
