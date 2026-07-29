---
tags:
  - issue
  - loops
  - backlog
  - knowledge-base
  - prompts
related:
  - "[[project_backlog_pipeline]]"
  - "[[okf_knowledge_base]]"
---

# The backlog block's "use `kb_list`" escape hatch cannot see part of the pool it points at

**Filed:** 2026-07-29. Found by the project-backlog pipeline's final whole-branch review
(as "M9") and **not recorded at the time** — this doc recovers a dropped finding,
re-verified against develop and the live dev index @ 2026-07-29.

Originally intended to be folded into [[kb_reindex_duplicate_key_on_legacy_notes]], but
that issue was resolved and moved to `docs/done/` before the fold happened, and the
residual split out of it ([[kb_duplicate_frontmatter_ids_collide_on_reindex]]) is a
different mechanism. Filed standalone instead.

## Symptom

The injected backlog block caps its list and offers an escape hatch:

```
  … N more (use kb_list to see them)
```
(`orchestrator/services/project_backlog.py:187`)

But `kb_list` cannot return part of what the pool contains, so an agent that follows the
instruction gets a **different, silently smaller** set than the counts line promised.

## Mechanism

The two queries disagree on one predicate:

| | filter | order |
| --- | --- | --- |
| pool (`fetch_backlog`) | `status='active'` + ticket types | `priority, created_at, note_id` |
| `kb_list` → `list_notes` (`src/services/knowledge_store.py:713`) | `kb_id = $1` **AND `path IS NOT NULL`** | `modified_at DESC` |

A note written through the agent path (`upsert_note`) lands **pathless** — it only gains
a `path` when a reindex adopts it. `fetch_backlog` does not filter on `path`, so those
rows *are* in the pool and *are* counted in the totals line; `list_notes` excludes them.

Measured on the live dev index, 2026-07-29: **812 pathless rows** against 3286 pathed.

The result is worst exactly where it matters most: a ticket an agent filed this cycle is
in the pool, contributes to the counts, and is invisible to the tool the block tells the
next agent to use for the tail. The ordering differs too (`modified_at DESC` versus
priority-then-age), so even the visible subset comes back in a different sequence.

## Why it matters

The counts line exists specifically so a hard cap cannot hide the pool's tail — that was
the reasoning recorded in the design spec. This defect puts a hole in the other half of
that guarantee: the reader is told a tail exists, then handed a tool that cannot show all
of it. An agent concluding "the remaining N do not exist" is behaving reasonably on bad
information.

## Suggested directions

1. **Point at something that shares the pool's filters** — a listing that keys on the
   same predicates, so the tail the block promises is the tail the agent can fetch.
2. **Relax `list_notes`' `path IS NOT NULL`** for this use, if pathless rows are
   legitimately listable (check what that predicate protects against before touching it —
   it is documented at `knowledge_store.py:1162` as guarding against pathless ghost rows).
3. **Make the sentence honest** — the cheapest fix. If no tool can currently return the
   tail, say the tail is not directly listable rather than naming one that cannot.

Whichever route, keep the counts line: it is the part that works.

## Note

Pathless rows have their own open question — whether adoption is expected to claim them
on a later run or whether they accumulate — tracked alongside the reindex identity work.
This issue stands regardless of that answer: as long as *any* row can be in the pool and
absent from `kb_list`, the fallback sentence is wrong.
