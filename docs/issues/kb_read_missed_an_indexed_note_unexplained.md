# `kb_read` returned "not found" for a note that existed — unexplained

**Status:** **Open and UNEXPLAINED.** Observed once, live on dev, 2026-08-14. Not reproduced.
No workaround, no fix — this doc exists to stop the next person re-deriving the eliminations.
**Severity:** **Low impact, higher trust cost.** It cost nothing on the day: the agent fell
back to `kb_search`, found the same constraint, and delivered correctly. But a read path that
denies an existing note, silently and selectively, is not something to leave uncharacterised
in a system whose whole premise is that agents can rely on their knowledge base.

`file:line` references are as of `2a0d7997`. The file moved ~56 lines under `0c0c5607`
(`feat(officer): knowledge plane boundary K1-K3`) *after* the observation; that commit added
functions but did **not** touch `kb_read`, `_matching_notes` or `_read_from_binding`, so the
read path is unchanged.

## What happened

Job `29c28492` (`gpt-5.6-sol`, project `a572e4a0`) called, with five other `kb_read`s in the
same job:

```
Tool: kb_read
Args: {"kb": null, "note": "project:where-the-code-lives-repos-kurortengine"}
Result: Note 'project:where-the-code-lives-repos-kurortengine' not found.
```

That note is the project's **binding convention note** — the one that says code lives in
`repos/KurortEngine/` and must never be written to `repo/`. It is the single most important
note in the vault, and it is the one the read missed.

The other five reads in the same job, same `kb: null`, same `project:` prefix, all succeeded.

## What has been ruled out, with evidence

| hypothesis | verdict | evidence |
|---|---|---|
| The note did not exist yet | **No** | `knowledge_index.indexed_at = 2026-08-13 13:07:31`; job created `2026-08-14 00:44:33` — eleven hours later |
| The `project:` prefix is mishandled | **No** | `kb_read` resolves `alias:slug` via `_resolve_note_scope` (`:429`), and five other `project:`-prefixed reads succeeded in the same job |
| Seeded notes differ from `kb_write` notes | **No** | Rows are structurally identical. Compared against `hotel-rheinland-erp-theme-acceptance-criteria` (read OK) and `current-workspace-lacks-expected-reposkurortengine-checkout` (read OK): all three have `status` set, `path` set, `project_id = kb_id = a572e4a0`, `job_id NULL` |
| The note was pathless (the known §10 trap) | **No** | `path = knowledge/where-the-code-lives-repos-kurortengine.md` |
| The index was mid-rebuild | **No** | `_index_readiness_notice` (`:1037`) appends a readiness notice when that applies; the returned error was bare |
| Ambiguous across bound KBs | **No** | That path returns a distinct "is ambiguous across selected knowledge bases" error (`_ambiguous_note`, `:970`), not "not found" |

It reads correctly today via `GET /api/projects/{id}/knowledge/{note_id}` (unprefixed).

## Where the answer probably is

`kb_read` (`:1721`) → `_matching_notes` (`:957`) → `_read_from_binding` (`:949`), once per
binding. "Not found" means every binding returned falsy. Since sibling reads in the same job
resolved through the same bindings moments earlier, the interesting question is not *which
binding* but **what `_read_from_binding` predicates on that differs per note**. The row
comparison above says it is not `status`, `path`, `project_id`, `kb_id` or `job_id`.

Candidates not yet excluded:

- A per-note condition inside `_read_from_binding` not represented in the columns compared
  (embedding presence, chunk rows, a revision/tombstone join).
- A transient: the note being mid-rewrite by a sweep at that instant. The reindexer's
  delete-then-write shape makes brief invisibility plausible — compare
  [`knowledge_links_target_id_too_narrow_and_rewrite_is_destructive`](knowledge_links_target_id_too_narrow_and_rewrite_is_destructive.md),
  where the same non-transactional pattern destroys edges. Worth checking whether note
  content/chunk replacement has the same window.

## How to reproduce or close it

1. Drive `kb_read` from a real agent context (it is an agent tool; the MCP/API read is a
   different code path and does **not** exercise the bug).
2. Read the same slug repeatedly while a sweep reindexes that vault — if the transient
   hypothesis holds, it fails only inside the rewrite window.
3. If it cannot be reproduced, the cheap mitigation is to make the miss *loud*: when
   `_matching_notes` returns nothing but a row for that `note_id` exists in `knowledge_index`,
   say so rather than reporting a flat "not found". A reader that can prove the note exists
   and still declines to return it should not look identical to a genuine typo.

## Why it matters more than its impact suggests

The loop is about to run unattended, and its whole safety story rests on agents reading
binding constraints out of the KB. On the day, the agent recovered because it also searched.
An agent that had trusted a single `kb_read` would have concluded the convention note did not
exist — and the convention note is precisely the thing standing between this project and the
`repo/` failure that lost three developer turns of work.

## Related

- `docs/features/better_resavio_restart_status.md` §5 — the job this was found in.
- [`kb_sweep_indexes_archived_projects_and_starves_connectors`](kb_sweep_indexes_archived_projects_and_starves_connectors.md)
  — sweep behaviour, fixed in `d215e727`.
