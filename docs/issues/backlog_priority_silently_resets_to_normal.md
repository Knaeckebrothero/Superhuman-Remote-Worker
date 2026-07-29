---
tags:
  - issue
  - loops
  - backlog
  - knowledge-base
related:
  - "[[project_backlog_pipeline]]"
  - "[[okf_knowledge_base]]"
---

# Two paths silently reset a backlog ticket's priority to `normal`

**Filed:** 2026-07-29. Raised by the project-backlog pipeline's final whole-branch review
(as "M10" and "M11") and **not recorded at the time** — this doc recovers two dropped
findings, re-verified against develop @ 2026-07-29. Combined because they share one
failure mode, not one mechanism.

## Why they belong together

Priority is a **non-binding label** by design: nothing gates, refuses, or reorders work
on it, so a wrong value can never raise, fail a check, or turn a job red. It only changes
which tickets the loop is shown first. That makes silent-reset defects invisible by
construction — the only way to find one is to go looking, which is what this doc is for.

---

## Path 1 — `rebuild_from_notes` drops priority entirely

`KnowledgeStore.rebuild_from_notes` (`src/services/knowledge_store.py:1620`) is the cold
start / drift-recovery path: it deletes a project's whole index and rebuilds it from
Neo4j.

```python
await self.db.execute("DELETE FROM knowledge_index WHERE project_id = $1", ...)
for note in notes:
    await self.upsert_note(
        note_id=..., project_id=..., title=..., note_type=..., content=...,
        status=..., confidence=..., tags=..., keywords=..., job_id=...,
        phase=..., retrieval_messages=..., created_at=..., modified_at=...,
    )                                    # <- no priority
```

`priority` is not passed, so every rebuilt row takes the column default `1` (`normal`).
Neo4j never carried the field — it is a pgvector column introduced by migration
`vector/0013` — so the data is not merely unpassed, it is **unavailable on that path**.
Running this against a project with a triaged backlog flattens the whole queue.

**Reachability — verified:** there are currently **no production callers**. A repo-wide
grep finds `rebuild_from_notes` only in `tests/test_knowledge_store.py`. So this is a
dormant landmine, not an active bug: it fires the day someone wires the recovery path to
an endpoint or a runbook, which is exactly when a human is already having a bad time.

**Suggested fix:** either read priority from the surviving index rows before the DELETE
and pass it through, or make the function refuse to run against a project holding ticket
types until the field can be preserved. Doing nothing is defensible *if* the docstring
says plainly that it destroys priority — silence is what makes it a trap.

---

## Path 2 — three `.get("priority", 1)` defaults, one of them load-bearing

Three sites read priority off a row with a silent default instead of the key:

| line | function | note |
| --- | --- | --- |
| `112` | `KnowledgeRecord.from_row` | |
| `1196` | `get_note_by_slug` | **feeds `kb_update`'s preserve-on-None** |
| `1255` | `list_notes` | comment states it uses `dict(r)` to tolerate fake rows in tests |

The `:1255` comment is explicit that the shape exists for test convenience. That is
production code bent around a test double, and it costs a real guarantee: if any of these
queries ever stops selecting `priority`, the code does not raise — it reports **every
ticket as `normal`**.

`get_note_by_slug` is the one that matters. It supplies the existing priority to
`kb_update`'s "omitted means leave unchanged" behaviour — the exact contract that needed
two fix rounds to close during the pipeline build, because the failure mode is a silent
reset of a `high` ticket. A dropped column in that SELECT would reintroduce that bug
with the defensive default acting as camouflage.

**Suggested fix:** index the key (`row["priority"]`) so a missing column fails loudly at
the boundary, and fix the test doubles to supply the field rather than shaping production
code around their absence. If a default must stay for genuine legacy rows, make it
explicit and narrow — a `COALESCE` in the SQL, where the intent is visible — rather than
a Python `.get` that cannot distinguish "legacy row" from "I forgot to select it".

---

## Verification

For Path 1: build a project with mixed priorities, call `rebuild_from_notes`, assert the
ranks survive. It should fail today.

For Path 2: remove `priority` from `get_note_by_slug`'s SELECT and run the `kb_update`
preserve-on-None tests. They currently **pass** — that is the defect. After the fix they
should fail.
