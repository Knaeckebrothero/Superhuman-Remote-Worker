# KB sweep reindexes archived projects, and a slow native KB starves external connectors

**Status:** **Open, observed live on dev 2026-08-13.** Worked around for one project by deleting
its `project_repositories` row; no code fix yet.
**Severity:** **Medium-high** — an archived project consumes embedding calls indefinitely, and
because native KBs are swept before external ones in the same tick, one slow native KB can
block every external KB connector from ever indexing.

## Two defects, one symptom

### 1. Archiving a project does not stop its KB being reindexed

`kb_sweep_tick` (`orchestrator/services/kb_reindex.py:974`) picks its work with:

```sql
SELECT DISTINCT project_id
FROM project_repositories
WHERE role = ANY($1::text[])     -- ('knowledge', 'jobs')
ORDER BY project_id
```

There is no join to `projects` and no status predicate. A project's KB is swept forever, for as
long as it has a `knowledge`- or `jobs`-role repository row — regardless of `projects.status`.

The docstring says the query "only enumerates candidate projects; it deliberately does not apply
the [repo resolution] rule", which is about *which repo* to use. Project lifecycle was simply
never considered.

### 2. Natives are swept before externals, with no time budget

The same tick body handles native project KBs first and only then iterates external `kb`
datasources (`:1035`). Both share the tick. Nothing bounds how long the native phase may take,
so a native KB that cannot finish within a tick indefinitely delays everything behind it.

## Observed

Project `68137e29` (Better Resavio) was archived at ~11:05 on 2026-08-13. It kept a
`role='jobs'` repository row, so it stayed in the sweep set. Its watermark
(`srw_vector.kb_index_watermark`) read:

| field | value |
|---|---|
| `status` | `indexing` |
| `last_attempt_at` | 2026-08-13 10:50:05 |
| still running at | 11:27:31 — **37+ minutes** |
| `notes_done / notes_total` | 2250 / 3137 |
| `last_success_at` | **2026-07-07** |

3,137 notes at the ~1.1 notes/s measured on this cluster needs ~47 minutes, against
`KB_REINDEX_SWEEP_SECONDS = 900` (15 min) and frequent redeploys. It therefore never stamps
success, and re-processes ~3,100 notes on every cycle. It had not completed successfully in
**over a month**.

Downstream: a newly created external `kb` connector (`e66708b2`, the Better Resavio history
vault, 2,635 notes) sat unindexed. It was queued behind the archived project in the same tick
and never reached.

## Why this is easy to miss

The failure surfaces only on `kb_index_watermark`. It is not in the pod logs as an error — the
archived project logs *progress*, which reads as healthy activity — and the note count for the
starved connector simply stays at zero, which is indistinguishable from "not started yet".

A monitor that polls the note count and greps pod logs will sit silent through this
indefinitely. The cockpit's connector list is what surfaced it, via the row's `status` +
`last_error`.

## Workaround applied

Deleted `project_repositories` row `4e7c6a46` (`project-68137e29-jobs`, role `jobs`), which is
the only thing that kept the archived project in the sweep set. Verified afterwards:

- archived project no longer selected by the sweep query
- its **3,111 indexed notes retained** and still searchable — `knowledge_index` is keyed by
  `kb_id`, not by repository
- the Gitea repo untouched
- the new project's own `knowledge` row unaffected

Reversible by re-inserting the row. This is a per-project workaround, not a fix.

## Suggested fix

1. **Exclude non-active projects from the sweep.** Join `projects` and filter on `status`, or
   check status per project inside the loop. An archived project's KB should keep its existing
   index and simply stop being refreshed.
2. **Do not let the native phase starve the external phase.** Options, roughly in order of
   preference: give each phase its own budget within a tick; alternate phases across ticks; or
   sweep externals first when any native KB is mid-`indexing`.
3. **Investigate why a 3,137-note reindex never completes.** `last_success_at` a month stale
   with `status='indexing'` suggests the run is being cut short (next tick, pod roll, or an
   unhandled error near the end) rather than genuinely needing 47 minutes every time. The
   watermark should be able to resume rather than restart.
4. **Consider surfacing watermark `status`/`last_error` in the sweep logs**, so a starved or
   permanently-failing KB is visible without querying the vector store.

## Related

- `docs/features/knowledge_base_repo_separation.md` — external vs native KB indexing, the
  `native_project_id` marker
- `docs/superpowers/specs/2026-08-13-better-resavio-restart-design.md` §3a — the external KB
  host allowlist, and the topology this was found while building
