# Live validation — OKF KB slice-2 straggler batch (lint rules + curator gardening)

**Type:** live / dev-cluster validation (NOT a pytest — needs the deployed image and
the Better Resavio loop producing curator archive passes). **Status:** RUN 2026-07-05
(image `sha-2e8b807`, vault @ 331 files) — results inline per section, summary:

- **§1+2 offline lint (PASS with corpus drift):** 331 files → 660 findings
  (dead-link 369, oversized-note 130, duplicate-h1 104, orphan 33,
  missing-required-key 10, missing-frontmatter 8, invalid-yaml 5, slug-forked 1).
  ALL four fixture twins + the `-351609` canary are GONE from the vault (curator
  converged/deleted them file-side) — but their pgvector rows are still `active`:
  the corpus has ~250 file-less DB ghosts (deletes/renames on main never called
  kb_delete). Dual-write gap on DELETE confirmed; resolution = slice-3 PR4
  cutover + migration-hygiene audit (ghosts are pathless → invisible to chunk
  retrieval). One NEW slug-forked pair: `iteration-27-phase-2-tactical-retrosp…`
  + hash twin. Fixture table is stale within a day — the vault churns too fast
  for named fixtures; validate by rule-shape, not by slug.
- **§3 near-duplicate floor (DECIDED: raise 0.9 → 0.97):** pair counts at
  floors: ≥0.99 → 1, ≥0.97 → 4, ≥0.95 → 40, ≥0.93 → 138, **≥0.90 → 451** —
  0.9 is unusable noise (dominated by same-iteration archive-finding/retro
  boilerplate). The kurortengine twins score **0.888** — below ANY sane floor;
  embedding near-dup cannot catch lexical twins, `slug-forked` covers them
  (complementary rules — do NOT chase them by lowering the floor). Curator
  attention is the scarce resource → precision-first: **0.97**. Revisit at PR4
  (chunk-granular changes the definition).
- **§4 URL probe (PASS):** alive→None, 404→`HTTP 404`, no-DNS→`unreachable: …`.
- **§5 curator Garden (FAIL — adherence):** audit trail since 07-04 shows
  kb_write 306 / kb_read 256 / kb_search 226 / kb_update 52 — and **zero
  kb_lint, zero kb_index** calls. Workflow step 5 is not being exercised.
  Follow-up: confirm the tools are in the agents' tool menu; if present, the
  fix is prompt emphasis (as this runbook predicted), not code.

Unit coverage remains green; the slice-3 reindex live-verification findings from
the same day (import heisenbug, pgvector codec, batch cap) are recorded in
`knowledge-base/knowledge/features/okf_knowledge_base.md` §11 PR3.1 — they came out of exercising
this corpus at reindex scale, exactly what this runbook existed to provoke.

**What it validates** (the 2026-07-05 batch, `knowledge-base/knowledge/features/okf_knowledge_base.md`
§11.1 addendum):

1. `oversized-note` flags the legacy 131 KB curator dumps (WARNING > 15 KB body).
2. `slug-forked` flags exactly the two known legacy twin pairs — and does NOT flag
   the gate's SUPERSEDE outcome (superseded base + suffixed survivor).
3. `near-duplicate` surfaces embedding twins via one pgvector self-join
   (`KnowledgeStore.find_near_duplicate_pairs`, 0.9 floor).
4. `dead-external-url` stays silent by default and flags only clear negatives when
   opted in (`check_urls=True`, 25-URL cap reported loudly).
5. Curators actually **Garden**: post-write `kb_lint` + `kb_index` calls appear in
   the audit trail, and new curator notes trend distilled (no more wholesale
   artifact dumps).

**Code:** `src/tools/knowledge/gardener.py` (rules + pure helpers),
`src/tools/knowledge/knowledge_tools.py` (`kb_lint` wiring, `_check_external_url`),
`src/services/knowledge_store.py` (`find_near_duplicate_pairs`),
`config/prompts/curation_prompt*.txt` (all five forks: Distill, Don't Dump +
workflow step 5 Garden).

---

## Prerequisites

1. The straggler batch is committed, pushed, and the CI image is deployed to the
   dev cluster (record the `sha-…` tag here when done: ________).
2. **Config/prompt freshness trap:** prompts and `resolved_config` are frozen
   per-job at CREATION time. Only loop jobs *created after* the deploy exercise
   the new curator prompts — the job running across the deploy does not.
3. The Better Resavio loop (project `68137e29`) is running, or at least one new
   curator archive pass has happened since the deploy.

## Known-answer fixtures (from the live corpus, verified 2026-07-05)

The legacy corpus doubles as a fixture set — expected outcomes are known:

| Rule | Must flag | Must NOT flag (false-positive canary) |
|---|---|---|
| `slug-forked` | `kurortengine-red-phase-test-exploits-ignored-param` + `…-ab0ae0` (both active, 06-27); `iteration-9-red-phase-bootstrap-decision-package-skeleton-in-red-vs-green` + `…-ef33b5` (both active, 07-02) | `iteration-9-phase-0-strategic-archive-finding…` + `…-351609` — base is `superseded` (the gate's SUPERSEDE outcome working as designed) |
| `oversized-note` | the run-8 131 KB / 60 KB learning/retrospective dumps (exact slugs: SQL below) | ordinary notes; reserved `index.md` / `log.md` |
| `near-duplicate` | whatever the SQL ground truth (§3) returns | pairs where one side is superseded/archived (query is active-only) |

---

## 1+2. Engine rules, offline (no cluster dependency)

The deterministic rules are pure — run them from a dev-box checkout of the loop
repo, no DB needed. Clone the loop jobs repo (creds live in
`project_repositories.repo_url` — do **not** print/commit the embedded password),
then from this repo's root:

```bash
python3 - <<'EOF'
import pathlib
from src.tools.knowledge.gardener import lint_kb

root = pathlib.Path("/path/to/project-68137e29-jobs/knowledge")   # <- adjust
notes = [{"path": f"knowledge/{p.name}", "text": p.read_text()}
         for p in sorted(root.glob("*.md"))]
report = lint_kb(notes)
for f in report.findings:
    if f.rule in ("slug-forked", "oversized-note", "duplicate-h1"):
        print(f"{f.rule:15} {f.path}  {f.message[:80]}")
EOF
```

**Expect:** the two `slug-forked` pairs from the fixture table; `oversized-note`
on the known dumps; NO `slug-forked` line for the `…-351609` survivor (its base's
frontmatter must carry `status: superseded` — if the file does NOT carry that
frontmatter, that's a real dual-write gap: the pgvector row is superseded but the
canonical file wasn't updated → file a finding, don't shrug).

## 3. `near-duplicate` ground truth (pgvector, read-only)

```bash
kubectl --context=k3d-srw exec -n srw srw-pgvector-0 -- sh -c \
  'psql -U "$POSTGRES_USER" -d srw_vector -c "
   SELECT a.note_id, b.note_id, round((1 - (a.embedding <=> b.embedding))::numeric, 3) AS sim
   FROM knowledge_index a JOIN knowledge_index b
     ON b.project_id = a.project_id AND a.note_id < b.note_id
   WHERE a.project_id::text LIKE '\''68137e29%'\''
     AND a.status = '\''active'\'' AND b.status = '\''active'\''
     AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
     AND 1 - (a.embedding <=> b.embedding) >= 0.9
   ORDER BY sim DESC LIMIT 50"'
```

Oversized ground truth while you're there:

```bash
kubectl --context=k3d-srw exec -n srw srw-pgvector-0 -- sh -c \
  'psql -U "$POSTGRES_USER" -d srw_vector -c "
   SELECT note_id, length(content)/1024 AS kb FROM knowledge_index
   WHERE project_id::text LIKE '\''68137e29%'\'' AND length(content) > 15360
   ORDER BY kb DESC LIMIT 20"'
```

**Expect:** the self-join result matches what a deployed `kb_lint` reports as
`near-duplicate` (same pairs, filtered to slugs that exist as files). Judge the
**0.9 floor** here: if the list is dominated by legitimately-distinct notes,
raise the floor (or lower it if the known twins score just under). This is the
main tuning knob this runbook exists to settle.

## 4. `dead-external-url` (offline probe check)

Default OFF is unit-covered; spot-check the probe's conservatism from the dev box:

```bash
python3 -c "
from src.tools.knowledge.knowledge_tools import _check_external_url as c
print(c('https://example.com/'))                      # expect None (alive)
print(c('https://example.com/definitely-404-xyz'))    # expect 'HTTP 404'
print(c('https://no-such-host.invalid/'))             # expect 'unreachable: …'
"
```

Then one deployed run: ask a curator/agent (or a throwaway job) to call
`kb_lint(check_urls=True)` and confirm the report separates `dead-external-url`
findings from the deterministic ones and prints `url-sweep-truncated` iff the
vault has > 25 unique external URLs.

## 5. Curator Garden behavior (the real E2E)

After ≥ 2 post-deploy curator passes on the loop:

- **Tool calls:** the job's audit trail (MCP `get_audit_trail`, or `agent_audit`
  on `srw-auditdb-0`) shows `kb_lint` and `kb_index` calls in the archive phase
  *after* the `kb_write`/`kb_update` calls — the new workflow step 5.
- **Distillation trend:** new curator notes stay far below 15 KB (SQL from §3
  scoped to `created_at > <deploy time>`); no new wholesale artifact dumps.
- **Twin convergence:** the curator, prompted by `slug-forked`/`near-duplicate`
  findings, merges or supersedes the two legacy pairs — active same-title twins
  go 2 → 0 (first psql query from §Known-answer, or:
  `GROUP BY title HAVING count(*) > 1` over active rows).
- **`index.md` exists** on the loop repo's `main` under `knowledge/` and carries
  the gen-markers with grouped `[Title](slug.md) - description` bullets.
- **Model-fork coverage:** at least one gated pass per prompt fork in use by the
  loop (check the job's model; MiniMax-M3 uses the base fork). Adherence is the
  question — weaker models may skip step 5; if so, the fix is prompt emphasis,
  not code.

## Success criteria

- [ ] Offline `lint_kb` over the loop vault reproduces the fixture table exactly
      (both twin pairs flagged, `…-351609` canary NOT flagged, dumps flagged).
- [ ] §3 self-join output ≙ deployed `kb_lint` `near-duplicate` findings; 0.9
      floor judged (keep / adjust — record the decision in the feature doc §11.1).
- [ ] URL probe: alive/404/no-DNS behave as above; cap is loud.
- [ ] ≥ 2 post-deploy curator passes show lint→garden→index in the audit trail.
- [ ] Active same-title twins on Better Resavio: 2 → 0 (curator-driven merge).
- [ ] New curator note sizes: none > 15 KB since deploy.
- [ ] Follow-ups filed for anything failed (esp. the possible dual-write gap on
      superseded-status frontmatter, §1+2).

When all boxes tick, update `knowledge-base/knowledge/features/okf_knowledge_base.md` §11.1 addendum
with "straggler batch LIVE-VERIFIED <date>" and proceed to slice 3 (Postgres
index + retrieval cutover) — which will re-run these rules at reindex scale.
