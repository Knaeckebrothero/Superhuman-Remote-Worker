---
tags:
  - feature
  - architecture
  - workspace
  - datasource
  - knowledge
  - loop
  - orchestrator
related:
  - "[[loop_repo_compounding]]"
  - "[[loop_repo_compounding_v2]]"
  - "[[project_knowledge_base]]"
  - "[[knowledge_base_substrate_decision]]"
  - "[[repo_datasource]]"
  - "[[multi_datasource_support]]"
  - "[[datasource_redesign]]"
  - "[[project_cloud_folders]]"
  - "[[workspace_simplification]]"
  - "[[projects]]"
---

# Workspace and change records — the project repo as index, datasources as destinations (2026-08-01)

## 1. The idea

Stated by the requester, verbatim, because every design choice below serves one of these
four sentences:

> All I wanted was just to have all jobs work together to build something, each of them
> being like a PR to a main branch. The knowledge base changes should show which job
> changed what note when. Agents should do pull requests on external repos for code
> changes. The agents could use their subbranches as scratchpads and would only merge the
> final results/changes to the main branch.

With one generalisation added afterwards, which turns out to be the keystone:

> Changes don't even have to be a repo. They could be changes to any other datasource
> including the project's cloud folder or a SQL DB added as a datasource. So the agents
> just record their changes done and we merge that.

That last sentence is the unifying abstraction. **The thing that merges to `main` is not the
work — it is the record of the work.** Code lands in the customer's repository. Notes land
in the knowledge store. Files land in the cloud folder. Rows land in the database. What
merges is one immutable record per job saying what changed, where, and pointing at it.

`main` stops being a filesystem and becomes an index.

## 2. Why now — the tangle this resolves

Traced on 2026-08-01 while investigating job `d1894a91` (developer/MiniMax-M3, project
Better Resavio), which burned 15.7 hours without delivering. The proximate cause is a
separate issue (`docs/issues/shell_cwd_drifts_and_the_anchor_is_unreachable.md`), but the
investigation exposed that one directory tree is currently asked to be four different
things at once:

- the agent's scratch space (41 files at the workspace root: `out.txt`, `scope2.txt`,
  `result1.txt`–`result4.txt`, `spec.yaml.probe-write`, `wc.txt`, `grep.txt`, four rival
  `plan*.md` files, …)
- the job's bookkeeping (`plan.md`, `todos.yaml`, `archive/`, `notes/`, `retros/`)
- a checkout of the project under `repo/` — with `repo/.venv/` (`bin/`, `lib/`,
  `pyvenv.cfg`) committed into git, because `.gitignore` excludes `repos/` (plural) while
  the checkout lands in `repo/` (singular)
- the accumulating shared project history that other jobs merge into

Because all four share one tree and one branch, "merge only the final results" is not
expressible: merging the branch merges the scratch, the virtualenv and the bookkeeping too.
Every path problem found that day is downstream of this.

## 3. What already works — do not rebuild

Verified against code and cluster state on 2026-08-01. This is the majority of the design
and it is already in production.

**Branch-per-job with PR merge into a shared project repo.** From `d1894a91`'s own history:

```
098bf3fe retro: iter 002 critic (b75a6745) — merged
9de8f34c Loop iter 2 · CRITIC: select & prioritise the next improvement … (#149)
81f77ec8 retro: iter 001 scholar (edd06963) — merged
dbe1974f Loop iter 1 · SCHOLAR: research the domain & propose … (#148)
```

Fourteen iterations of jobs branching, merging as numbered PRs, and accumulating toward one
goal. Requirement one is done.

**A per-job record already merges to `main`, including when nothing else does.**
`write_loop_retro` (`orchestrator/services/project_loops.py:1099`) writes
`retros/NNN-<role>-<jobid8>.md` onto `main` with frontmatter carrying `job`, `branch`,
`status`, `merge_status`, `merge_sha`. It is written by the **orchestrator after the merge
outcome is known**, deliberately, so critics read mechanical truth rather than an agent's
self-report from a destroyed workspace (F40). Live proof of the empty case:
`330b8ae2 retro: iter 009 developer (a8befbd5) — skipped`.

**Per-directory git already exists.** `GitManager` accepts `remote_cwd` —
*"Relative path within the backend root to use as the git working directory. Used for
auxiliary repos cloned into subdirectories"* (`src/managers/git_manager.py:78`), and
`clone()` takes it too (`:919`). Nested repos with their own history are a supported shape,
currently used read-only.

**The knowledge base is already tool-mediated and store-backed.** Every read path —
`kb_search`, `kb_read`, `kb_list`, `kb_related`, `kb_provenance`, `kb_contradictions`,
`kb_unanswered` — goes through `knowledge_store` against Postgres/pgvector. No agent ever
reads a note off disk. `knowledge_index` already carries `job_id` ("which job created this
note") and `phase`.

**Agents can already drive git and `gh` from the shell.** No new tool is required to open a
pull request; what is missing is credentials scoped to allow it and a place to record that
it happened.

**A deliverable contract exists.** `required_deliverables` + the seal, and
`deliverable_path_variants` (`src/core/deliverables.py:90`) already tolerates two spellings
of a path.

## 4. The model

| Thing | Role | Lifetime |
|---|---|---|
| **Project repo** | versioned default workspace **and** the index of everything the project ever did | permanent |
| **Job branch** | scratchpad — messy by design, never merged wholesale | disposable |
| **Datasources** | where real change lands: external git, knowledge base, cloud folder, SQL, … | external, permanent |
| **Change record** | the one artifact every job merges to `main` | permanent |

The project repo remains the default workspace, so a job with no attached datasource still
has somewhere to work and something to version — that path is unchanged and keeps working.
What changes is that the branch becomes disposable and the merge becomes curated.

## 5. The change record

One file per job on `main`. A generalisation of today's retro, not a replacement — same
path convention, same orchestrator-writes-it discipline, extended frontmatter:

```yaml
---
type: job_record
job: 9f2c…
project: 68137e29-…
role: developer
branch: job/9f2c-line-recovery
status: completed
merge_status: skipped          # merged | skipped | failed
merge_sha: ~
created: 2026-08-01T09:14:22Z
changes:
  - datasource: customer-api          # datasource name
    kind: git
    action: pull_request
    ref: https://github.com/cust/api/pull/42
    summary: "3 files, +180/-12"
    verified: true                    # orchestrator confirmed the PR exists
  - datasource: project-kb
    kind: knowledge
    action: upsert
    ref: [chose-jwt-over-oauth, api-rate-limit-experiment]
    summary: "2 notes written, 1 superseded"
    verified: true                    # confirmed against knowledge_index
  - datasource: hotel-cloud
    kind: cloud
    action: write
    ref: /Projects/HotelRheinland/Q3-report.pdf
    summary: "1 file written"
    verified: false                   # agent claim, not machine-checked
---

# developer · line recovery

<job description>

## Agent completion notes
<freeze_data.notes>
```

**The uniform shape is the point.** `kind` distinguishes git / knowledge / cloud / sql /
file; everything else reads the same regardless of destination. A reviewer, a critic, or a
future job can answer "what has this project actually done" by reading `main` alone,
without querying four stores.

### 5.1 Who writes what — and why it must stay split

Today's retro is orchestrator-written specifically so agents cannot fabricate it. That
property must survive, because this project has already produced a job with 1,645 audit
entries and zero deliverables. The split:

- **The agent declares** what it changed. It has to — only the agent knows the PR URL it
  just created.
- **The orchestrator verifies and stamps.** Merge status and SHA it knows first-hand.
  Agent claims get checked where checking is cheap: does that PR URL resolve, do those note
  slugs exist in `knowledge_index`, does that cloud path exist.
- **Unverifiable claims are recorded as claims** (`verified: false`), never silently
  promoted. A record that cannot distinguish "did it" from "said it did" is worth less than
  no record.

## 6. Components

### 6.1 Knowledge base as a default-attached datasource

Every project gets a KB datasource created and attached by default, inherited by all its
jobs. Behaviour is unchanged for users — every project has a knowledge base today too — but
it decouples *"this project has knowledge"* from *"the agent's workspace is a checkout of
something"*, which are currently fused.

This also settles the file question. The KB's authoritative store is Postgres; the only
filesystem dependency is that `kb_write` also materialises `knowledge/<slug>.md` on the
workspace (`src/tools/knowledge/knowledge_tools.py:521`), and `kb_export` is documented as
*"one-way export for human browsing or migration"* (`:1931`). Nothing reads back. So the
markdown mirror should become an explicit export, not an automatic dual-write on every note
— removing a consistency surface that has already produced a vault-corruption incident and
a `kb: dissolve the .md-shaped export directories` cleanup commit in this very project.

### 6.2 Attributable knowledge history

Required by *"the knowledge base changes should show which job changed what note when"*,
and currently impossible. `knowledge_index` is one row per `(project_id, note_id)` and the
write is an upsert that overwrites the body in place:

```sql
ON CONFLICT (project_id, note_id) DO UPDATE SET
    ... content = EXCLUDED.content, ...
```

(`src/services/knowledge_store.py:418`, plus a second overwrite path at `:886`.) There is
no revisions table in any vector migration. An agent that re-writes an existing slug
destroys the prior body with no recovery. `superseded_by` / `status='superseded'` exist but
express *note A replaced by note B* — they do not preserve prior content of the same note.

**Proposal: `knowledge_note_revisions` + a `BEFORE UPDATE` trigger on `knowledge_index`
copying the OLD row.** The trigger form matters — it captures every write path
automatically, including both existing overwrite sites and any added later, with no
application changes. Omit the embedding from the revision (regenerate on restore) to keep
history cheap.

This was weighed against making git the source of truth for knowledge. Rejected, because
hybrid search (`vector(4096)` dense + tsvector sparse + HNSW + RRF) requires Postgres
regardless, so git-as-truth does not remove Postgres — it demotes it to a derived index and
adds a sync pipeline. That pipeline is exactly what has already failed twice here: the
reindex watermark that never advances, and the vault corruption that needed manual repair.
Concurrency is the second reason: multiple loop roles write notes to one project
simultaneously, which Postgres handles and per-note git commits do not.

**Caveat, recorded honestly:** git would additionally buy *human review of knowledge
changes* — a PR someone approves before a note lands. Postgres revisions do not give that.
If review, rather than recovery, becomes the requirement, this decision should be revisited.

### 6.3 Change-capable datasources

Today repository datasources are read-only reference material, gitignored precisely because
they are not output. Making them destinations is the largest new build here, and the work
is mostly *not* in the agent:

- **Credentials.** Write-scoped tokens per datasource, distinct from the read path. Subject
  to the standing rule that internal credentials never live in the workspace.
- **Policy.** Push to a branch and open a PR; never push to a protected default branch.
  Per-datasource configuration of what the agent is permitted to do.
- **Capture.** The resulting PR URL flows into the change record.

The same three concerns generalise to non-git datasources, which is why the record's shape
is `kind`-tagged rather than git-specific:

- **Cloud folder** — files written/moved, recorded as paths.
- **SQL** — statements or migrations applied, recorded as migration name or statement
  digest. Needs a policy decision on whether agents may write at all, and if so whether
  through migrations only.
- **Others** (email, WebDAV, MCP-backed) — same record, different `kind`.

Not all of these need to ship together. The record schema needs to accommodate them all
from the start so it does not have to be re-cut per datasource type.

### 6.4 Curated merge — making the branch a real scratchpad

Today merging a job branch merges everything on it. The change: **merge the contracted
deliverables and nothing else.** `required_deliverables` already defines the contract and
the seal already checks it; the merge should draw from the same list rather than taking the
whole tree.

Consequences, all good: scratch files stop accumulating into permanent shared history; a
stray `.venv` never reaches `main` whether or not `.gitignore` catches it; and the branch
becomes genuinely free to be messy, which is what the requester asked for.

This also removes most of the pressure behind the `repo/` vs `repos/` layout question. If
only contracted paths merge, where the agent scribbles matters much less.

### 6.5 Generalise the job record beyond loop jobs

`write_loop_retro` lives in `project_loops.py` and is invoked from
`_merge_and_retro_loop_job` (`orchestrator/main.py:13539`) — it is scoped to loop jobs. The
invariant this design depends on is **every job leaves exactly one record on `main`,
always**, including jobs that merged no files, jobs whose only output was an external PR,
and failed jobs (a failure is information, and the honest-floor culture depends on it being
recorded).

Lift the writer out of the loop service, call it from the general job-completion path, and
keep the loop's iteration/role fields as optional frontmatter.

## 7. Sequencing

Ordered so each step is independently useful and nothing blocks on the largest piece.

0. **Prerequisite — `docs/issues/shell_cwd_drifts_and_the_anchor_is_unreachable.md`.**
   Small, already specified, and it makes path drift visible *before* directories start
   moving. Doing layout work while the drift is silent is how today's mess was produced.
1. **Generalise the job record** (6.5) + extend the schema with `changes` (5). No behaviour
   change for existing loops; establishes the invariant everything else writes into.
2. **KB revisions** (6.2). One migration plus a trigger. Delivers the attribution sentence
   on its own, independent of everything else.
3. **Curated merge** (6.4). Turns the branch into a scratchpad and stops the accumulation.
4. **KB as default datasource** (6.1) + demote the markdown mirror to explicit export.
5. **Change-capable datasources** (6.3), git first — credentials, branch policy, PR
   capture. Then cloud, then SQL as separate decisions.

## 8. Open questions

- **Does anything read the `knowledge/*.md` mirror?** Cockpit knowledge views, the
  deliverable seal, and project-page reads were not traced. If one reads files rather than
  the store, the mirror is load-bearing and 6.1 gets more complicated.
- **SQL write policy.** Should agents write to database datasources at all? Migrations
  only? This is a product/safety decision, not a technical one.
- **Verification depth for change records.** Resolving a PR URL is cheap; diffing its
  contents against the agent's claimed summary is not. Where is the line?
- **Retention.** One record per job on `main` forever is fine at hundreds of jobs. Worth a
  thought at tens of thousands.
- **Cross-job history recovery.** `d1894a91` recovered a file via
  `git show f8f8cfb:repo/tests/…` from the *project* repo's history, populated by earlier
  jobs. Under curated merge, code no longer accumulates there. That recovery idiom moves to
  the external repo — better, since it becomes real history rather than accidental — but it
  is a live behaviour and should not break silently.

## 9. Explicitly not in scope

- Removing the project repo. It stays as the versioned default workspace and the index; a
  job with no attached datasource must still have somewhere to work.
- Making persistent shell tabs stateless (env vars, virtualenvs). Separate issue.
- Changing any model family's `shell_mode`.
- The phase-model work in `docs/issues/phase_model_overhead_amnesia_loop.md`, which is
  orthogonal and in flight.
