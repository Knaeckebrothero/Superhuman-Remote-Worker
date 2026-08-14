---
tags:
  - status
  - projects
  - project-loop
  - knowledge-base
  - git-integration
status: in-progress
created: 2026-08-14
related:
  - "[[project_jobs_repo_retirement]]"
  - "[[knowledge_base_repo_separation]]"
  - "[[job_review_delivery_links_and_review_session]]"
  - "[[loop_unified_engine]]"
  - "[[project_self_improvement_loop]]"
---

# Better Resavio restart — running status

**Design:** `docs/superpowers/specs/2026-08-13-better-resavio-restart-design.md` (decisions,
corpus split, migration sequence). **This doc** is the live status: what is done, what is
live, what remains, and the traps found on the way. Read the spec for *why*; read this for
*where we are*.

Last updated 2026-08-14.

## 1. Why this exists

The Better Resavio loop stopped producing. The last run (loop `17e257b3`, 08-06→08-08) was
the cleanest in the project's history — 12 jobs, all `completed`, zero failures — and
**delivered nothing**: all 11 `job_change_records` read `delivery_status = no-changes`,
including every developer turn.

Cause: after the 2026-08-04 jobs-repo retirement, delivery reads only `projects/<slug>/`, and
`job_cloud_baseline.py:559` drops any diff path outside that prefix **silently**. The agents
wrote to `repo/`, because their `spec.yaml`, their test oracles and 3,111 knowledge notes all
said so. Real work — `job-f0403dca` has `repo/src/` (11 files), `repo/tests/` (6),
`pyproject.toml` across a proper TDD sequence — was committed to job repos and abandoned.

## 2. Current topology

| what | where | state |
|---|---|---|
| Project | `a572e4a0-d97a-4103-91fd-92a980d6717d` "Better Resavio" | active |
| Archived predecessor | `68137e29` "Better Resavio (pre-split archive)" | archived, 3,111 notes retained |
| Code | `github.com/Knaeckebrothero/KurortEngine` (private) | live, `main` @ `0de25e0` |
| Code connector | `2991589e` type `repository`, `config.forge=github`, `auto_attach=true` | live |
| Live vault | Gitea `project-a572e4a0-knowledge`, native KB | **488 notes** indexed |
| History vault | Gitea `srw/better-resavio-history`, external `kb` connector `e66708b2` | **2,635 notes** indexed |
| Cloud Space | Nextcloud id 8 | provisioned, non-code deliverables only |

The code clones into every workspace at **`repos/KurortEngine/`** — verified in agent logs
(`Cloned repository datasource 'kurortengine' into repos/KurortEngine`), with the PAT
redacted (`oauth2:***@`).

## 3. Migration sequence — status

| # | step | state |
|---|---|---|
| 0 | Fix `test_ac6` recursion | ✅ done |
| 1 | Create GitHub repo, push code | ✅ `0de25e0`, 205 files |
| 2 | PAT on the repository connector | ✅ encrypted at rest |
| 3 | Archive `68137e29`, create new project | ✅ `a572e4a0` |
| 4 | Seed 476 live notes, reindex | ✅ 7m04s, ~1.12 notes/s |
| 5 | `kbGitAllowedHosts` deploy | ✅ live: `git.srw.works,srw-gitea:3000` |
| 6 | History repo + external connector | ✅ 2,635 notes indexed |
| 7 | Verify index counts | ✅ see §4 |
| 8 | Link code connector to project | ✅ + `auto_attach=true` |
| 9 | Convention note into live vault | ✅ `where-the-code-lives-repos-kurortengine` |
| 10 | HEAD-comparison delivery guard | ❌ **not built** — see §6 |
| 11 | Manual developer job, watched | ✅ **passed** — see §5 |
| 12 | Start the loop | ❌ not started |

## 4. Verification results

**Live vault** (476 seeded, now 488 with agent additions): all notes carry `path`,
`search_doc` and an embedding; 476 distinct slugs, zero collisions; 1,539 links extracted;
3,104 chunks; **0** indexed under the connector's own UUID (the `native_project_id` marker
holds); zero duplicate slugs. `search_knowledge` returns real design notes.

**History vault**: 2,635 notes under connector UUID `e66708b2`. This also validates the
self-hosted external-KB path (`values.example.yaml` ships that configuration and it had never
been run).

**Convention note** verified on the *agent's* retrieval path (chunk hybrid), which matches it
for "where does the code live", "where do I write code", "do not write to repo" and "how is
work delivered". Note-level FTS does **not** rank it for looser phrasings — which is why the
same fact is also carried in the project description, where no retrieval is involved.

## 5. The manual developer job — step 11 passed

Job `29c28492`, model `gpt-5.6-sol`, project `a572e4a0`.

**Result: PR #1** on `KurortEngine`, branch `design/hotel-rheinland-theme`, commit `5e08d4f`,
**+1348 −0 across 2 files**, open and mergeable with 2/2 checks passing. Job status
`pending_review`.

The full seam works: **clone → branch → commit → push → PR**. That path had produced nothing
since 08-04.

Independently verified rather than accepted:

- Both contracted deliverables present (`docs/design/theme.md` 26 KB,
  `docs/design/theme-preview.html` 34 KB).
- Preview genuinely self-contained: **0** external refs, 0 `<script src>`/`<link href>`.
- Only the two intended files touched — the `src/`/`tests/`/`spec/` fence held.
- **Eight contrast ratios recomputed from the hex values**, all matching the claims within
  rounding (16.04 vs 16.00, 7.44 vs 7.44, 6.87 vs 6.87, …). It also flagged `#6E8189` at
  4.06:1 as *not* usable for body text — correct, since that is below 4.5.

**A first attempt failed usefully.** Job `26bceac2` found no `repos/KurortEngine/` and
**refused to improvise**: *"I will not create the forbidden top-level `repo/` substitute."*
That is the exact failure mode that started this work, prevented. Cause was operator error:
the connector was linked to the project but `auto_attach=false`, and `datasource_ids` was
omitted, so the code never reached the job. Fixed at source (`auto_attach=true`).

Also observed: the **deliverable gate bounced once** and the agent corrected — the contract
mechanism works.

## 6. What remains

### 6a. The delivery guard (step 10) — and a design correction

**This run changed the design.** `main` did **not** move, and that is *correct* — the work is
on a branch under review. A guard that compares `main` before/after would have recorded this
successful delivery as **nothing landed**, recreating the exact false signal it exists to
prevent.

The guard must treat **branch pushed + PR open** as delivery. §3c of
`job_review_delivery_links_and_review_session.md` shipped a live PR-status read, which is the
signal it should consume.

Second open question, unresolved: **do not overload `delivery_status`.** For this project it
is legitimately `no-changes` on every code turn forever, because code does not go to the
cloud folder. One column carrying two unrelated truths is how the original failure happened.
Give the git outcome its own field.

### 6b. Starting the loop (step 12)

Blocked on 6a. When it starts: `workspace_backend=container` (**not** `vm` — all six recorded
loops used `vm`; `docs/issues/vm_reliability_assessment.md` puts VM infra-failures at 2.2×
container, and loop `3ed022a5` died on three consecutive VM provisioning failures),
`scheduling=standard`.

### 6c. Loop-era debt to re-check before the first unattended run

- **Curator bloat.** The old vault reached 3,111 notes, 73% learning/retrospective. Nothing
  yet stops the new one refilling at that rate. The flat-root inbox convention only
  segregates.
- **Iteration numbering.** Agents self-labelled `iter-27`/`loop-16`/`iter 2` in one run;
  `retros/` numbering was the only ground truth, and `retros/` is retired under the new flow.
- **Outcome-aware rotation.** A failed critic is skipped and the developer proceeds on a
  stale verdict.

## 7. Defects found and filed

| doc | summary |
|---|---|
| `kb_sweep_indexes_archived_projects_and_starves_connectors.md` | Sweep has no project-status filter, so archived projects are reindexed forever; natives sweep before externals with no budget, so a slow native starves external connectors. Observed: archived 3,137-note project stuck `indexing` 37+ min, `last_success_at` a month stale. |
| `kb_connector_token_auth_over_http_fails_invisibly.md` | Token auth is correctly refused over http, but the `ValueError` is raised during source construction — before anything writes `kb_index_watermark`. The connector retried and failed every tick for over an hour while the cockpit showed a stale, unrelated error. |
| `job_review_delivery_links_and_review_session.md` | Job review linked only the scratch job repo; PRs were never persisted; no PR read path. **§3a/b/c now shipped.** |

## 8. Instrumentation lessons (three failures, same root cause)

Every monitor I built during this work watched a signal the failure path does not write:

1. Watched **note count + pod logs** — the failure was on `kb_index_watermark`.
2. Watched **`kb_index_watermark`** — the failure escaped before anything wrote it; only the
   sweep's per-tick log line was truthful.
3. Watched terminal states **`completed|failed|cancelled`** — the real set includes
   `pending_review`, `paused`, `waiting`, `waiting_for_reply`, so the monitor ran straight
   past the job finishing.

**Rule:** before arming a watch, ask which surface the *failure* writes to, and enumerate the
real state set instead of the expected one (`SELECT DISTINCT status FROM jobs`).

## 9. Operational notes

- Archived project `68137e29` was removed from the KB sweep by deleting
  `project_repositories` row `4e7c6a46`. Its 3,111 notes remain indexed and searchable —
  `knowledge_index` is keyed by `kb_id`, not by repo. Reversal SQL was captured before
  deletion. This is a per-project workaround, not a fix.
- A Gitea token `kb-history-connector-readonly` (id 2, scope `read:repository`) backs the
  history connector. Revocable; nothing else uses it.
- **Gitea is internet-reachable** (`git.srw.works` → Cloudflare + public ingress). Treat any
  Gitea credential as externally exploitable; do not paste one into a transcript.
