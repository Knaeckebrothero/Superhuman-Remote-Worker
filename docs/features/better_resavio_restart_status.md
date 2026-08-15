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
| 10 | Delivery guard | ✅ **built** `a040dd31` — see §6a |
| 11 | Manual developer job, watched | ✅ **passed** — see §5 |
| 12 | Start the loop | ❌ not started — unblocked, see §6b |

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

**The deliverable gate bounced, and this doc previously scored that backwards.** It said the
bounce proved "the contract mechanism works". It proved the opposite: the gate emitted a
**false negative**, and the agent recovered by defeating a deliberate platform invariant.

The contract required `repos/KurortEngine/docs/design/theme.md` and `…/theme-preview.html`.
Cloned repository datasources land at `repos/<name>/`, which the platform **gitignores at seed
time on purpose** (`src/core/workspace.py`, `src/core/datasource_setup.py`,
`src/tools/orchestrator/repositories.py` — "working-tree only; never versioned", guarding the
contentless-gitlink bug `b1758f38`). So the gate demanded a git-tracked path the platform
refuses to track.

The two halves of the contract read different substrates and only agree when the deliverable
sits in the job's own versioned tree:

| half | reads | verdict on `repos/KurortEngine/docs/design/theme.md` |
|---|---|---|
| agent (`resolve_workspace_deliverable`) | live filesystem | present, accepted |
| orchestrator (`evaluate_deliverable_gate`) | Gitea job tree | missing → bounce |

Both normalize the **singular** `repo/` prefix (the F14 fix, `deliverable_gate.py:86`);
neither knows the plural `repos/`. One character apart, opposite meanings.

To pass, the agent moved `repos/KurortEngine/.git` aside, un-ignored exactly those two paths,
committed them into the job repo, then restored `.git` and reverted `.gitignore` — git keeps
tracking files once added. Surgical and honestly documented in `output/job_frozen.json`, with
no `src/`/`tests/`/`spec/` leakage. It was cornered, not sloppy.

**This is the same blind spot as §6a, in a different module.** `job_delivered_nothing` was
taught on 08-14 that a pushed branch plus an open PR is delivery. The gate never got that
lesson: `deliverable_gate.py` contains no reference to `pull_request` and was last modified
08-07. `context.pull_request` is sitting right there — orchestrator-written, `verified: true`,
fails closed on malformed records — and the gate does not look at it.

Filed as `docs/issues/deliverable_gate_cannot_see_cloned_repo_deliverables.md`.
**Unfixed, and it blocks a clean step 12**: every loop code turn has this shape, costing two
resume cycles and then either the `.gitignore` workaround or demotion to `pending_review` for
work that actually shipped.

Not a risk here, checked: the full-squash-merge fallback in `merge_loop_job_contribution`
(none-of-the-contracted-files-present → full merge) is **unreachable for loop jobs** —
`should_merge_job_contribution` returns early with "loop job (the loop advance owns its
merge)" (`completion.py:939`).

## 6. What remains

### 6a. The delivery guard (step 10) — **built** `a040dd31`

**This run changed the design.** `main` did **not** move, and that is *correct* — the work is
on a branch under review. A guard that compares `main` before/after would have recorded this
successful delivery as **nothing landed**, recreating the exact false signal it exists to
prevent.

Built as three changes, all under TDD with a negative control (reverting the predicate to its
pre-change form fails exactly the four behavioural tests and leaves the rest green):

| where | change |
|---|---|
| `job_records.derive_changes` | Emits a `kind: pull_request` entry from the record `repo_open_pr` persists into `jobs.context`. |
| `job_records.job_delivered_nothing` | Replaces `delivery_status == "no-changes"` as the loop's F29 alarm; `main.py:_record_loop_job_outcome` consumes it and now also emits a positive action line naming the PR. |
| `project_loops.render_loop_job_history` | Names the PR alongside `delivery=no-changes`, so the next iteration is not told the last one shipped nothing. |

**Two rulings worth keeping.**

*The signal is the persisted record, not a live status read.* §3c's `repo_pr_status` is the
right tool for a **reviewer** asking "is it still open?". It is the wrong one for the guard:
this is a best-effort audit path that must never block a loop advance, and making it depend on
a third-party forge call would put a network dependency inside `write_loop_retro`'s
swallow-everything `except`. The persisted record already proves the PR was created — the
orchestrator wrote it itself, on success, at tool-call time.

*The PR entry is `verified: true`, and that does not weaken §5.1.* The rule that agent claims
are never promoted stands. This record is not an agent claim: the orchestrator persisted it,
and reading it back fetches nothing. Prose parked under the same key is **dropped**, not
downgraded — `parse_job_pull_request` fails closed on a malformed record, so the guard cannot
report a delivery that may not exist.

**`delivery_status` was deliberately left alone**, as the open question required. It is
legitimately `no-changes` on every code turn here, and one column carrying two unrelated
truths is how the original failure happened. The git outcome lives in the `changes` array,
which already had the right shape and the verified/unverified distinction.

Not covered: a job that pushes a branch **without** opening a PR still reads as no delivery.
That is the correct default under a review-based flow — an unreviewed branch is not delivered
— but it means an agent that pushes and forgets the PR is flagged, loudly, which is the
intended direction of failure.

### 6b. Starting the loop (step 12)

Unblocked by 6a, and the orchestrator image carrying it (`sha-d215e72`) is deployed.

**Use `workspace_backend="sandbox"`, not `"container"`.** The loop-start endpoint validates
against `_LOOP_WORKSPACE_BACKENDS = {"sandbox", "vm", "virtual", "none"}`
(`orchestrator/routers/project_loops.py:37`) and rejects anything else with a 400.
`"container"` is legacy spelling, still accepted by the *job* config path
(`main.py:6160`) but not by this one. Blank/None also resolves to the default sandbox.

When it starts: `workspace_backend="sandbox"` (**not** `vm` — all six recorded
loops used `vm`; `docs/issues/vm_reliability_assessment.md` puts VM infra-failures at 2.2×
container, and loop `3ed022a5` died on three consecutive VM provisioning failures),
`scheduling=standard`.

### 6c. Loop-era debt to re-check before the first unattended run

- **Curator bloat.** The old vault reached 3,111 notes, 73% learning/retrospective. Nothing
  yet stops the new one refilling at that rate. The flat-root inbox convention only
  segregates.
- **Iteration numbering.** Agents self-labelled `iter-27`/`loop-16`/`iter 2` in one run;
  `retros/` numbering was the only ground truth, and `retros/` is retired under the new flow.
- ~~**Outcome-aware rotation.**~~ **Fixed** `d616caa9`. Rotation advanced unconditionally, so
  a failed critic handed the developer its slot and the developer built on the previous
  iteration's verdict as if it were fresh. Worse, the failure was *invisible*:
  `consecutive_failures` resets on any turn that is not wholly failed, so a critic could fail
  every cycle and never trip `max_consecutive_failures` — the successful developer behind it
  always reset the counter, and the loop would spend its whole budget on unjudged work and
  stop reporting `budget`, no failures. A wholly-failed turn now re-runs its own stage, which
  puts failures back-to-back so the existing stop trips. Also fixed a latent TTL bug it
  exposed: the KB convergence tick fired on `next_index == 0`, which a *retry* of stage 0
  satisfies without completing a cycle.

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
