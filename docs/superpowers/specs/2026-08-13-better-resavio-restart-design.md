---
tags:
  - spec
  - projects
  - knowledge-base
  - project-loop
  - git-integration
created: 2026-08-13
status: approved
related:
  - "[[project_jobs_repo_retirement]]"
  - "[[knowledge_base_repo_separation]]"
  - "[[loop_repo_compounding_v2]]"
  - "[[project_self_improvement_loop]]"
  - "[[loop_unified_engine]]"
  - "[[centurion]]"
---

# Better Resavio: Restart on a Split Code/Knowledge Topology

**Date:** 2026-08-13 · **Status:** Design approved; preparation executed (§9), migration not started
**Goal:** Get the Better Resavio loop producing again by giving it two separate
repositories — a code repo it can actually compound into, and a knowledge vault that
stops drowning its own retrieval — on a fresh project using the post-retirement topology.

## 1. Why the loop stopped producing

Not for the reason it looked like. The last run was the *cleanest in the project's
history* and shipped nothing.

Loop `17e257b3` (2026-08-06 → 08-08) ran 12 jobs on the post-retirement flow — all
isolated `job-<id>` repos, all `completed`, zero failures. **All 11 `job_change_records`
carry `delivery_status = no-changes`, including every developer turn (iterations 3, 6, 9).**

The mechanism: delivery reads only `projects/<slug>/`. `job_cloud_baseline.py:559`
returns `None` for any diff path outside that prefix, **silently**. The agents wrote to
`repo/`, because that is what their `spec.yaml`, their test oracles and 3,111 knowledge
notes all say. The prompt does say `projects/<project-slug>/`
(`project_loops.py:456,511,560`); accumulated project convention beat it, three turns
running.

The work was real and is recoverable: `job-f0403dca` holds `repo/src/` (11 files),
`repo/tests/` (6), `pyproject.toml` and `spec.yaml`, committed across a proper TDD phase
sequence — delivered nowhere. And no job repo contains a `projects/` directory at all,
so the cloud folder is empty, so each developer greenfield-rebuilds from nothing.

**This is F29 wearing the new flow's clothes**, and it generalises to any loop project
after the 2026-08-04 cutover.

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Fresh project**, not an in-place migration of `68137e29` | The post-retirement flow is only validated on freshly-provisioned projects; legacy migration is an untested operator path |
| D2 | Code compounds into a **git `role=source` repo**, not the cloud folder | The user's call; gives real history, PRs and CI on the product, which the cloud-folder path cannot |
| D3 | Repo hosted on **GitHub, private**, created by the user from a staging folder | User's call; overrides the Gitea default proposed during design |
| D4 | Live KB = the **476 active design notes**; everything else becomes a **read-only external `kb` connector** | 73% of the active index was curator output, against a retrieval path already measured as similarity-luck |
| D5 | Agent pushes using a **fine-grained PAT scoped to the one repo**, embedded in the stored `repo_url` | Works with today's `_clone_auxiliary_repos`; blast radius is one private repo |
| D6 | A **HEAD-comparison guard** becomes the delivery signal for execution turns | `delivery_status` is structurally `no-changes` for this project now; something must be able to say "nothing landed" |
| D7 | `repo/` → `repos/KurortEngine/` **rewritten in the live slice only** | The live slice is forward-looking guidance; the history slice is an archive and rewriting it would falsify the record |
| D8 | Workspace backend **container**, not `vm` | All six recorded loops ran `vm`; `docs/issues/vm_reliability_assessment.md` puts VM infra-failures at 2.2× container, and loop `3ed022a5` died `stop_reason=failures` on three consecutive VM provisioning failures |
| D9 | First cycle is a **single manual developer job**, not a loop start | The seam being replaced is exactly the one that failed silently; watch it once end to end |
| D10 | Project `68137e29` is **archived**, not deleted; the new project reuses the name | Its jobs repo is the only copy of the pre-cutover record, and `job-<id>` repos hold the undelivered developer work |
| D11 | History vault lives on the **internal Gitea**, not GitHub, attached as an external `kb` connector | Avoids a second GitHub repo, and exercises the self-hosted external-KB configuration `values.example.yaml` anticipates but nobody has run. Requires `orchestrator.kbGitAllowedHosts: "srw-gitea:3000"` — see §3a |
| D12 | Live vault stays the **auto-provisioned native Gitea repo** | The native KB write path is Gitea-bound and there is no way to point a new project's KB at an external repo — see §3a |

## 3. Topology

Three repositories and one connector over a new project:

| what | where | attachment | agent access |
|---|---|---|---|
| Code | `github.com/<owner>/KurortEngine` (private) | `role=source`, name `KurortEngine` | clones to `repos/KurortEngine/`; branch → push → PR |
| Live vault | Gitea `project-<id8>-knowledge`, auto-provisioned | `role=knowledge` → native project KB | `kb_write`, `search_knowledge` |
| History vault | internal Gitea, new repo `srw/<name>` | external `kb` connector (`connection_url: http://srw-gitea:3000/...`) | `search_knowledge`, read-only |
| Cloud Space | auto-provisioned with the project | — | non-code deliverables only |

The repo **name** is load-bearing: `_clone_auxiliary_repos` (`src/core/workspace.py:743`)
clones to `repos/<name>/` and dedupes by directory name, not URL. It also clones from
whatever `repo_url` sits in `project_repositories` and nothing else — there is no separate
credential path, which is why D5 puts the PAT in that URL.

A new project provisions only the knowledge repo (`orchestrator/main.py:45031`); no jobs
repo is created. `resolve_kb_repo` (`kb_reindex.py:923`) prefers `role=knowledge` and
falls back to `jobs`, so a fresh project has no ambiguity to resolve.

## 3a. Where a knowledge base can live (measured 2026-08-13)

The question "can the KB be an external repo, like the code?" was raised and the
answer is asymmetric. Verified against the source, not assumed:

**A KB can be *read* from a remote git host.** `RemoteKnowledgeGitSource`
(`kb_git_source.py:1008`) does real git operations from the orchestrator, with a
host allowlist, auth methods `public|token|password|ssh`, credentials passed to git
only through a temporary askpass — never argv or env — and credential-redacted
errors. Unit-tested in `tests/test_kb_git_source.py`.

**It cannot be *written*.** Three independent reasons:

| | |
|---|---|
| `ProjectCreate` (`main.py:9676`) has no knowledge-repo field | there is no "supply an external repo as the KB at creation" |
| external `kb` bindings are `writable=False` unconditionally (`bindings.py:31`) | an attached external KB is read-only by construction |
| `materialize_knowledge_note` takes a `gitea_client` and calls `change_files` (`kb_materialize.py:264`); the native sweep uses `GiteaKnowledgeGitSource` (`kb_reindex.py:520`) | the write path is Gitea-bound end to end |

`src/services/forge.py` does abstract github/gitea/gitlab, but implements only
`open_pull_request` — there is no multi-file commit behind it. So a GitHub-writable
vault is an implementation, not a configuration. Hence D12: the live vault is the
auto-provisioned native Gitea repo, because that is the only place `kb_write` can land.

### The allowlist, and why it matters here

`validate_git_remote_trust` (`kb_git_source.py:568`) requires an exact
`(host, port)` match — or `(host, None)` when the port is the scheme default —
before any network operation. It rejects IP literals and `localhost`. The trusted
set is four built-in public hosts (github.com, gitlab.com, bitbucket.org,
codeberg.org) plus whatever `KB_GIT_ALLOWED_HOSTS` adds.

**On dev that variable is unset.** `helm/values.yaml:150` has
`kbGitAllowedHosts: ""`, and the chart only emits the env var when the value is
non-empty (`helm/templates/orchestrator/deployment.yaml:1536`); confirmed against
the running pod. So today an external KB connector can only point at those four
public hosts, and a Gitea-hosted history vault is silently not an option.

Enabling it is one value plus a reconcile:

```yaml
orchestrator:
  kbGitAllowedHosts: "srw-gitea:3000"
```

Verified admissible: `srw-gitea` passes `_SAFE_DNS_LABEL_RE` as a single label, the
service listens on 3000, `http://` is an accepted scheme, and the `host:port`
allowlist form is supported. Credentials must **not** be embedded in the URL — the
validator raises on that — so they go in the datasource's `credentials` field,
which also means the KB credential never enters a workspace.

## 4. Corpus split

3,111 indexed notes, measured on `srw-pgvector-0`:

**Live — 476 notes** (`status='active'` and a durable design type):
decision 289 · goal 83 · plan 57 · code 33 · question 4 · feature 4 · source 3 · idea 2 · issue 1

**History — 2,635 notes:**
learning 935 · retrospective 721 · state 558 · plus the archived and superseded rows of
the live types (goal 203, plan 178, decision 21, question 19)

476 + 2,635 = 3,111, matching the index exactly. Zero slug collisions inside the live
slice, zero overlap between slices, zero notes without a file on disk. A further 27 files
exist in the vault with no index row and are staged separately for triage.

Seeding uses the out-of-band push path proved by the §9 gate on 2026-08-12 and
re-verified on-cluster 2026-08-13 after `0ef826ca` fixed `search_doc` population and
wikilink extraction. External `kb` connectors accept a `root_path`; the native vault is
fixed at `knowledge/` (`kb_reindex.py:63`).

## 5. The delivery seam, and the trap it leaves behind

Developer turns push to GitHub, so nothing lands under `projects/<slug>/` and
`job_change_records.delivery_status` will read **`no-changes` on every developer turn** —
the identical signal that hid three lost cycles.

That signal must stop being the compounding truth for this project. The delivery outcome
becomes a comparison of `KurortEngine`'s `main` HEAD before and after each execution turn,
recorded as its own status rather than left to be inferred from a field that now
structurally cannot be right.

The machinery is close to hand. `merge_loop_job_branch` is dormant rather than deleted —
it returns `skipped` at `project_loops.py:1152` because new-flow jobs have an empty
`branch_name` — and its curated-commit path (`:1485-1568`) already reads blobs from
`(repo, ref)` and writes via `change_files(repo, "main", …)` as **separate calls with
separate repo arguments**, driven by a declared `files_contract`, and already resolves the
`repo/`-prefixed spelling (the "F14 both-spellings rule", `:1494-1502`). `get_branch_head_sha`
is used at `:1205` and `:1322`.

## 6. The recurrence risk

The single most likely way to rebuild the same failure: **the agent's own memory says the
code lives at `repo/`.** That string was in `spec.yaml`, in `spec_lock.md`, in every test
oracle (`repo/tests/test_<module>.py::test_acN_...`), and in 206 of the 476 notes we are
about to seed — 1,085 occurrences.

So the seed is not a copy. D7's rewrite is a precondition, not a tidy-up, and it is not
sufficient on its own: a convention note stating where code lives has to be authored into
the live slice at high priority. The rewrite fixes existing references; only the note
tells a *new* agent where to write.

## 7. Risks

| Risk | Mitigation | Status |
|---|---|---|
| `delivery_status=no-changes` reads as success | D6 HEAD guard becomes the signal | Designed, not built |
| Agent writes to `repo/` again | D7 rewrite (done) + convention note | Partly done |
| PAT readable inside the workspace | Fine-grained, scoped to one private repo | Accepted |
| Agent pushes nothing and the turn looks clean | D6 | Designed, not built |
| 476-note cold index unmeasured at this scale | Time it during the seed | Open |
| `kbGitAllowedHosts` is empty, so the history connector silently cannot reach Gitea | Set it and deploy before step 6 (§3a) | Open |
| `test_ac6` recursion fork-bombs a workspace | Sentinel guard mirrored from `test_audit_isolation.py` | **CLOSED 2026-08-13** — full suite 177/2 in ~10 s, peak 5 procs (was 139 + timeout) |
| Curator refills the live vault with learnings | Unmitigated; the flat-root inbox convention only segregates | Open |

## 8. The suite is armed

Measured on the extracted code (§9):

| condition | result |
|---|---|
| no venv inside the repo | 169 passed, 8 failed |
| with `.venv/` at the repo root | **174 passed, 3 failed** |

Of the three real failures, two are self-invoking tests and one of them —
`test_repo_layout.py::test_ac6_full_pytest_suite_exits_zero` — shells out to
`pytest tests/ --ignore=tests/test_a11y_guest_pwa.py --ignore=tests/test_audit_isolation.py -q`.
That inner run still collects `test_repo_layout.py`, re-enters `test_ac6`, and spawns
another, without limit. Observed: **139 nested pytest processes** and a 600 s timeout.
`test_audit_isolation.py::test_ac2_full_suite_exits_zero` re-invokes the suite the same
way but is **correctly guarded** by an environment sentinel checked at `:164` and set at
`:197` — that guard is precisely what `test_ac6` lacks, so the fix already exists in the
repo and only needs copying. The third,
`test_a11y_guest_pwa.py::test_ac2`, is a genuine isolation leak — it asserts on
`AuditLog._shared_entries`, populated by a different test, and fails standalone.

It stayed hidden because `pyproject.toml` sets `addopts = "-x"`, so inner runs abort at
their first failure — and there was always a failure, because the venv assumptions could
not hold once the committed virtualenv was gone. **Repairing the suite is what arms the
recursion**, and "the entire suite becomes genuinely reachable as a true green" is the
verbatim goal of the F-12 contract the loop was last working on.

Fixing the recursion is therefore the first ticket, ahead of anything the loop would
otherwise pick up.

## 9. Preparation — executed 2026-08-13

Local staging only. **No cluster state was mutated**; nothing was pushed anywhere.

**`KurortEngine/`** — 179 files / 2.1 MB, extracted from an 8,747-file / 150 MB jobs repo
at `7e00f47a`. `repo/` was already the package root (hatchling,
`packages = ["src/kurort_engine"]`, `testpaths = ["tests"]`), so this is that tree minus
the detritus, not a re-layout. Provenance in `KurortEngine/EXTRACTION.md`.

Left behind: `knowledge/` (3,138), two committed virtualenvs nested three levels deep
(2,016 files / 76 MB), a stray duplicate KB copy `knowledge_iter6_check/` (664),
`archive/` (1,241), `documents/external/` (586), `.subagents/` (238), and 39 loose scratch
`.txt` files at the repo root.

Two divergent `kurort_engine` trees were reconciled: `repo/src/` (last touched 07-28,
"Loop iter 12") is authoritative; the jobs-repo-root `src/` (07-14) is a stale fork
written to the wrong path — the same failure mode as §1. It is preserved in
`KurortEngine-salvage/`, not discarded, because it holds `f5_q64_checkout.py` whose
private helpers have no counterpart in the live tree.

The in-flight F-12 `spec.yaml`/`spec_lock.md` pair was moved to salvage rather than
carried: its paths are written against the old layout, and it cannot simply be rewritten
because `spec_lock.md` records a SHA-256 of `spec.yaml` in its lock metadata.

**`BetterResavio-KB/`** — the vault split three ways: `live/` (476), `history/` (2,635),
`unindexed/` (27). The D7 rewrite is applied and verified: 1,085 bare `repo/` → 0, 1,085
`repos/KurortEngine/` present, 206 files touched, history slice untouched at 7,866
occurrences as the control.

All three staging folders are gitignored. This repository is public under FSL and the
content is client material for Hotel Rheinland.

## 10. Migration sequence

```
0.  Fix the test_ac6 recursion                       ← DONE 2026-08-13
1.  User creates github.com/<owner>/KurortEngine (private) from KurortEngine/
2.  User mints a fine-grained PAT scoped to that repo
3.  Archive project 68137e29 (D10); create the new project, reusing the name →
    auto-provisions project-<id8>-knowledge + kb connector
4.  Push BetterResavio-KB/live/knowledge/ into the knowledge repo; reindex; time it
5.  Set orchestrator.kbGitAllowedHosts="srw-gitea:3000"; deploy   ← §3a, BEFORE step 6
6.  Create the Gitea history repo, push history/knowledge/, attach it as an
    external kb connector (credentials in the datasource, not the URL)
7.  Verify 476 notes under kb_id=project_id, 2,635 under the connector UUID, none twice
8.  Attach KurortEngine as role=source with the PAT-bearing URL
9.  Author the "code lives at repos/KurortEngine/" convention note into the live vault
10. Build the HEAD-comparison guard (D6)
11. One manual developer job, watched end to end: clone → branch → push → PR → HEAD delta
12. Start the loop: container backend, standard scheduling
```

Steps 0–8 are additive and abandonable. Step 10 is the gate; the loop does not start
until a single job has been observed to move `KurortEngine`'s HEAD.

## 11. Acceptance criteria

1. ~~The suite runs to completion with no nested pytest process and no timeout.~~ **MET 2026-08-13** — 177 passed / 2 failed in ~10 s, nesting bounded at 2 levels, peak 5 processes.
2. `knowledge_index` shows 476 notes under `kb_id = project_id` and 2,635 under the
   connector UUID, with no note indexed twice.
3. A `search_knowledge` in the new project returns hits from both vaults — which
   also proves the remote-git external-KB path works against a self-hosted Gitea
   over plain HTTP on a non-default port, a configuration `values.example.yaml`
   ships for customers and that has never been run.
4. An agent can read a whole design doc, `kb_write` a new note into the live vault, and
   clone `repos/KurortEngine/`.
5. A manual developer job moves `KurortEngine`'s `main` HEAD, and the guard's recorded
   outcome says so. `delivery_status` staying `no-changes` is expected and correct here —
   it describes the cloud folder, which is not where code goes any more (§5). What must
   not remain true is that `no-changes` is the *only* recorded outcome.
6. A developer job that deliberately pushes nothing is flagged, not recorded as clean.
7. No bare `repo/` path reference remains in the live vault.

## 12. Open questions

1. **Where the HEAD guard lives** — inside `_advance_loop_member`, or in the completion
   path alongside the cloud delivery record. Not decided.
2. **Whether `delivery_status` gains a value** for git-delivered turns, or whether the
   guard writes a separate field. A third status is cheap; overloading the existing one
   is how §1 happened.
3. **The 27 unindexed vault files** — triage; one is `index.md`, which per
   `knowledge_base_repo_separation.md` §10c nothing writes any more.
4. **Whether `f5_q64_checkout.py` is superseded or lost work.** Its public `checkout()`
   exists in the authoritative tree; its four private helpers do not. Not verified line
   by line.
5. **Curator bloat on the new vault.** D4 cleans the corpus once; nothing stops it
   refilling at the rate that produced 935 learnings in a month.
