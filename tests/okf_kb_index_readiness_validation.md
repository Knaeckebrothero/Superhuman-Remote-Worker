# Live validation — OKF KB index-readiness surfacing

**Type:** live / dev-cluster (k3d + Tilt) validation (NOT a pytest — needs a
running orchestrator plus a `kb` datasource actively indexing in the background).
The automated layer is already green and is NOT what this runbook covers.

**Status:** PENDING. Built + automated-verified 2026-07-15 (uncommitted); the live
e2e below has **not** been run. Design/impl recorded in the plan
`~/.claude/plans/okay-then-let-s-do-golden-quail.md`; this doc is the operational
evidence that remains. Record the image `sha-…` and results inline when run.

Origin: the readiness-surfacing follow-up to `knowledge-base/knowledge/features/okf_knowledge_base.md`
(§4 "tools show status during partial/indexing/failed" + §10). Before this work,
KB indexing was fire-and-forget and near-invisible: agents got a bare "no matches"
on a not-yet-indexed KB (indistinguishable from a genuine miss), the Cockpit badge
never advanced without a manual refresh, and nothing warned a user who attached a
still-indexing KB to a new job/session.

## What it validates (three independently shippable changes)

1. **Empty-result staleness (correctness).** `kb_search`/`kb_read`/`kb_list` now
   disclose "still indexing" on a **zero-result** query when the KB's watermark
   status ≠ `ready` — for native and external KBs. A ready KB with a genuine miss
   must stay silent (no false alarm).
2. **Determinate progress + live badge.** The watermark carries per-run
   `notes_done`/`notes_total`; the Cockpit datasource list polls every 5 s while a
   KB is pending/indexing and renders `N/M` + a determinate bar that reaches Ready
   without a manual refresh.
3. **Warn-only at create (no gate).** The shared datasource picker (job **and**
   session create) shows an inline "Not fully indexed yet…" warning on any
   not-ready `kb` row; selection and Start remain allowed (the indexing/dispatch
   decoupling is deliberately preserved — no dispatch gate, no agent wait).

## Automated coverage (already green — 2026-07-15, do not re-litigate here)

- **Backend:** `pytest tests/test_knowledge_tools.py tests/test_kb_bindings.py
  tests/test_knowledge_injection.py tests/test_kb_reindex.py
  tests/test_kb_datasource_api.py` → 266 passed. `ruff check` + `ruff format
  --check` clean.
- **Migration:** `scripts/schema-snapshot.sh vector` replays 0001→0012 into a
  scratch pg15 container and regenerated `orchestrator/database/vector_schema_current.sql`
  (clean 2-line diff: `notes_done`/`notes_total` on `kb_index_watermark`).
- **Cockpit:** the datasource-list, datasources-group, api.service, job-create and
  session-create specs pass (65 tests); `npm run build` is clean.

## Code

- Correctness: `src/tools/knowledge/knowledge_tools.py` (`_index_readiness_notice`
  + the three zero-result branches), `src/core/knowledge_injection.py`
  (`external_watermarks` filter relax).
- Progress: `orchestrator/database/migrations/vector/0012_kb_watermark_progress.sql`,
  `src/services/knowledge_store.py` (`KbWatermark`, `update_index_progress`),
  `orchestrator/services/kb_reindex.py` (`_PROGRESS_BUMP_EVERY`, reset/bump/final),
  `orchestrator/services/kb_datasources.py` (`index_status_payload`).
- Cockpit: `cockpit/src/app/core/models/api.model.ts`,
  `cockpit/src/app/views/datasources/datasource-list.component.ts` (poll + bar),
  `cockpit/src/app/views/agent-settings/datasources-group.component.ts` (warning),
  i18n `en.json` / `de-DE.json`.

---

## Prerequisites

1. Uncommitted changes are live on the cluster — either committed + CI-deployed
   (record `sha-…`: ________) or synced via `tilt up` (orchestrator uvicorn
   `--reload` + cockpit `ng serve`). **Migration 0012 applies at orchestrator
   startup** (`migrate.run_migrations()`), so confirm the orchestrator restarted
   after the code landed (see §0).
2. A `kb` datasource pointing at a Git repo with **many** `knowledge/*.md` notes,
   so indexing takes long enough to observe. The loop's own jobs repo is a
   ready-made large vault (project `68137e29`, hundreds of interlinked OKF notes) —
   or create a throwaway Gitea repo with a `knowledge/` tree of markdown.
3. k3d context/namespace: `--context=k3d-srw -n srw` (local). On homelab prod
   substitute `--context main -n superhuman-remote-worker` and `srw-pgvector-0`.

## 0. Migration + payload shape (fast pre-check, no large KB needed)

Confirm the columns exist and the status endpoint serializes them:

```bash
# columns present on the running vector DB
kubectl --context=k3d-srw -n srw exec deploy/srw-postgres-vector -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d kb_index_watermark"' \
  | grep -E 'notes_done|notes_total'   # expect two integer columns

# status endpoint carries the new keys (any existing kb datasource id; null when idle)
kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator -c orchestrator -- \
  curl -sf http://localhost:8085/api/datasources/<kb-id>/index-status \
  | python3 -m json.tool   # expect notes_done / notes_total keys present
```

**Expect:** both columns exist; the payload contains `notes_done`/`notes_total`
(null when no run is in flight). If the columns are missing, the orchestrator has
not applied 0012 yet — restart it and recheck.

## 1. Empty-result staleness (agent-facing — the correctness fix)

The window is between attaching a fresh KB and its first clean commit. Immediately
after creating a large `kb` datasource (status `pending`/`indexing`), have an agent
(or a throwaway job/session scoped to that KB) run `kb_search` for anything.

**Expect:** the tool returns the "No knowledge notes match …" line **followed by**
`⚠️ Still indexing — results may be incomplete: [alias] indexing — …`, NOT a bare
miss. Repeat once the KB is `ready`: a query with no hits returns the bare miss
with **no** indexing notice (no false alarm). `kb_read` (missing note) and
`kb_list` (empty) behave the same during the indexing window.

Reading the tool output: MCP `get_audit_trail` for the job, or the agent transcript.

## 2. Determinate progress + live badge (Cockpit)

1. Open `https://localhost/` → Data Sources. Create the large `kb` datasource (or
   trigger a **full** rebuild on an existing one — the confirm dialog warns re:
   embedding cost).
2. Watch the row's badge **without touching the page**: `Pending` → `Indexing`
   with a live `N/M` count and a filling bar → `Ready @ <sha>`. The 5 s poll must
   advance it on its own (no manual refresh / reindex click).
3. Cross-check the numbers against the endpoint while it runs:
   ```bash
   watch -n2 'kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator \
     -c orchestrator -- curl -sf http://localhost:8085/api/datasources/<kb-id>/index-status \
     | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[\"status\"], d[\"notes_done\"], \"/\", d[\"notes_total\"])"'
   ```

**Expect:** `notes_done` climbs toward `notes_total` (bumped every ~25 notes +
a final write), status ends `ready`, the bar disappears at Ready. On an
**incremental** reindex of a small change, `notes_total` is the small changed set
(not the whole vault) and it settles back to Ready quickly.

## 3. Warn-only at job/session create

While the KB from §2 is still `pending`/`indexing`:

1. **New Job** → attach that KB in the datasource picker. An inline "Not fully
   indexed yet — the agent may see incomplete results" note appears under the KB
   row; the checkbox stays selectable and **Start is allowed** (no gate).
2. **New Session** → repeat. Same shared picker, same warning (one code path
   covers both flows).
3. Once the KB reaches `ready`, reopen either dialog: **no** warning on that row.

## Success criteria

- [ ] §0: `notes_done`/`notes_total` exist on `kb_index_watermark`; status endpoint
      serializes them (null when idle).
- [ ] §1: a not-yet-indexed KB's empty `kb_search`/`kb_read`/`kb_list` shows the
      "Still indexing" advisory; a **ready** KB's genuine miss shows **no** advisory.
- [ ] §2: the badge advances Pending → Indexing `N/M` (bar) → Ready with **no**
      manual refresh; `notes_done` climbs to `notes_total`; incremental run shows
      the changed-set total, not the whole vault.
- [ ] §3: attaching a not-ready KB at job **and** session create shows the warning
      and still allows Start; a ready KB shows none.
- [ ] Follow-ups filed for anything failed.

When all boxes tick, note "index-readiness surfacing LIVE-VERIFIED <date>" in
`knowledge-history/done/okf_knowledge_base.md` (Status block). **If any box fails**, move
`knowledge-history/done/okf_knowledge_base.md` back to `knowledge-base/knowledge/features/` (reopen) and record the
gap here.
