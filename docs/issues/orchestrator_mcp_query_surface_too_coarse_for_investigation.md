# The orchestrator MCP query surface is too coarse for fleet-wide investigation — no predicate/WHERE filtering, `query_table` has no filter + broken pagination, `list_jobs` can't select `waiting`, and `search_audit` is single-job-scoped

**Status:** OPEN — usability/observability gap. Confirmed 2026-07-13 while
investigating the scholar self-provision misclassification
(`scholar_selfprovisioned_workspace_misclassified_as_inherited.md`). Not a
correctness bug in the product; it's a debugging-tooling gap that forces
direct DB access (`kubectl exec … psql`) for anything beyond the happy path.

## TL;DR

There is **no way to ask the orchestrator MCP server a filtered question** like
"which jobs failed with an error matching X" or "show me jobs stuck in
`waiting`". Every read tool is either a full-table page dump, a fixed-enum
filter that omits the states you most want to inspect, or scoped to a single
job you must already know. Cross-job / signature-based investigation degrades to
"pull N rows and eyeball them client-side," or bypass the MCP entirely and query
Postgres directly.

## Concrete gaps (as of image `sha-c2fbe06`)

| Tool | Limitation | Impact |
|---|---|---|
| `query_table(table_name, limit, offset)` | **No filter/WHERE, no column select, no ORDER BY.** Per prior findings it also **ignores `offset`** (returns ~the same head rows), so you can't even page through a table. | Cannot target a row by any predicate; cannot paginate a large table. Effectively "first page only." |
| `list_jobs(status, limit≤100)` | `status` is a **fixed enum**: `created, processing, completed, failed, cancelled, pending_review`. **Missing `waiting`** (and `reviewing`). No error/config/parent/time predicate. | Jobs stranded in `waiting` (e.g. a parent whose subjob failed to unblock it) are **invisible to the API**. No "failed AND config=scholar AND error~X" query. |
| `search_audit(job_id, query)` | **Requires a `job_id`** — searches within one job only. | No cross-job content search. To find which job emitted a message you must already know the job. |
| `get_job(job_id)` | Single job by exact UUID. | Fine, but presupposes you found the ID by other means. |

## Motivating case

Question: *"Has the scholar inherit-timeout (`Timed out … waiting for parent job
… workspace to become ready (container=None, vm=None)`) happened on the dev
cluster?"*

There is no tool that answers this directly. The workaround was:

1. `list_jobs(status="failed", limit=100)` — dump the 100 most recent failures.
2. **Read every row's `error` client-side** looking for the signature.

This is O(rows) eyeballing, capped at 100, and it **structurally cannot** find
the other half of the symptom — the stranded **parent** sits in `waiting`, which
`list_jobs` cannot even select. We could only infer parent state from the
*failed scholar sibling*. For anything more precise we fell back to
`kubectl exec srw-postgres-0 -- psql … "SELECT … WHERE error_message LIKE …"`,
which the MCP is supposed to make unnecessary.

## Why it matters

- **Investigation is the primary use of these tools** (they're read-only audit
  surfaces). A read surface with no predicates forces the exact DB access it was
  meant to abstract, and that DB access isn't available to every operator.
- **The most diagnostic states are the ones you can't select.** `waiting`,
  `reviewing`, and "failed with error like X" are precisely where stuck/leaked
  work hides; the enum omits them.
- **Silent truncation reads as "clean."** `list_jobs(limit=100)` returning no
  match looks like "it never happened," when it may simply be off the end of the
  page or in a non-selectable state.

## Suggested direction (not yet scoped)

1. **A filtered job query**: `find_jobs(status_in=[…], config_name=…,
   parent_job_id=…, error_contains=…, created_after=…, order_by=…, limit,
   offset)` over an allowlisted set of columns/predicates. Covers the common
   "failed scholars with error X since date Y" question in one call.
2. **Add the missing states** to the selectable set (`waiting`, `reviewing`) — at
   minimum so stranded jobs are visible.
3. **Fix `query_table` pagination** (honor `offset`) or deprecate it in favor of
   (1); a page-1-only dump is a trap.
4. **A cross-job audit search** (`search_audit` with optional `job_id`, plus
   `config_name`/time filters) so a message can be traced back to its job.

Keep it read-only and allowlist-bounded (don't expose raw SQL) — the goal is
targeted predicates over safe columns, not an arbitrary query engine.

## References

- Motivating investigation: `docs/issues/scholar_selfprovisioned_workspace_misclassified_as_inherited.md`
- Tools observed: `mcp__orchestrator__query_table`, `list_jobs`, `search_audit`, `get_job`, `get_table_schema`
