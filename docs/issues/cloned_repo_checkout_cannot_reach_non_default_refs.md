---
tags:
  - issue
  - workspace
  - git
  - datasources
  - tools
status: fixed
priority: P1
created: 2026-08-16
aliases:
  - shallow repo datasource checkout
  - repo_pull cannot fetch another branch
  - git_show answers for the wrong repository
related:
  - "[[verification_ticket_cannot_reach_another_jobs_candidate_commit]]"
  - "[[deliverable_contract_satisfied_by_a_note_about_failure]]"
  - "[[session_restore_drops_repo_checkouts]]"
---

# The git read tools silently answer for the wrong repository, and attached repos have no read path at all

**Status:** **FIXED on develop 2026-08-16, not yet deployed.** Three changes, below
under "What shipped". Observed live on job `c4849fa1` (2026-08-16 08:24–09:24, project
Better Resavio), which paged the Legate claiming its workspace was broken.

> **Correction (2026-08-16, same day).** The first revision of this document accepted the
> worker's diagnosis — "the clone carries only `origin/main`, the named commit is not in the
> object store, this is clone depth/refspec". **Two of those three claims are wrong.** The
> clone was healthy and contained everything the ticket asked for. What failed was the
> *instrument*, not the workspace. The corrected analysis follows; the original framing is
> preserved at the bottom so the reasoning error stays visible.

## The workspace was fine

Three independent checks, all against the live repo and the shipped code:

1. **The clone is not shallow and not single-branch.** `GitManager.clone` issues a plain
   `git clone <url> <target>` — `src/managers/git_manager.py:1536`. No `--depth`, no
   `--single-branch`, no `--filter`. It fetches every branch the remote has.
2. **The remote has exactly one branch.** `github.com/Knaeckebrothero/KurortEngine` carries
   only `main` at `aafad4ac`; `design/hotel-rheinland-theme` was deleted when PR #1 merged
   (2026-08-15 16:09). A local clone of the same repo shows the identical ref set the worker
   reported — `origin/HEAD` and `origin/main`, nothing else. **`packed-refs` containing only
   `origin/main` is what a correct clone of a one-branch repo looks like.**
3. **The "missing" commit is present, and so are its files.**
   `5e08d4fa06da12a9ec00bbffd78225c6faefbe55` is an ancestor of `aafad4ac`
   (`git merge-base --is-ancestor` passes), so it is in the object database of any clone at
   `main`. The two files it added — `docs/design/theme.md` and `docs/design/theme-preview.html`
   — are in `main`'s tree. That is why the job's own early audit reported the theme files
   present in `repos/KurortEngine/`. **The worker was standing on the deliverable it said it
   could not reach.**

The connector attached correctly too: `context.datasource_selection.origin = "default"`, both
datasources materialized, and the repository datasource is configured `default_branch: "main"`.

## What actually failed: `git_show` cannot address an attached repository

`create_git_tools` binds every git read tool to one repo and one repo only:

```python
git_mgr = context.workspace_manager.git_manager      # src/tools/git/git_tools.py:85
```

That is the **job's own workspace repository**. `git_show(commit_ref=...)`
(`git_tools.py:110`), `git_log`, and `git_diff` take no `repo` parameter and have no path to
`workspace_manager.source_repos`, where cloned datasources live.

So `git_show("5e08d4fa…")` asked the job repo about a KurortEngine commit. `fatal: bad object`
is the only answer it could ever return — **for a healthy clone and a broken one alike**. The
worker took a tool that cannot see the repository as proof that the repository was empty.

`repo_pull` then failed twice for a second, unrelated reason: the ticket named
`design/hotel-rheinland-theme`, and `git pull origin design/hotel-rheinland-theme --ff-only`
correctly fails against a branch that no longer exists upstream. (`repo_pull` *does* accept a
branch argument — `src/tools/repo/repo_tools.py:147` — contrary to the first revision here.)

## The real gap: attached repositories are write-only

The complete repo toolset is five tools (`src/tools/repo/repo_tools.py`):

| tool | line | direction |
|---|---|---|
| `repo_commit` | 99 | write |
| `repo_push` | 124 | write |
| `repo_pull` | 147 | write (fast-forward, current branch) |
| `repo_open_pr` | 163 | write |
| `repo_pr_status` | 240 | read — forge API, not the repo |

There is **no `repo_log`, `repo_show`, `repo_diff`, `repo_checkout`, or `repo_fetch`**. An
agent cannot inspect the history of an attached repository, check out a ref in it, or fetch a
ref into it. The only read affordance in reach is `git_show`, which silently answers for a
different repository.

`repo_clone` does not exist either. The worker's own re-dispatch plan asks the operator to
"bind `repo_clone` or `shell_execute`" — half of that request names a tool that has never
existed in this codebase. The grant table in `src/core/datasource_setup.py:130,136` lists
`repo_clone` as a granted capability name with no implementation behind it.

## Why this is expensive

The failure is silent at provisioning, wrong at diagnosis, and costs a full worker run plus an
operator page to reach a conclusion that is false. Job `c4849fa1` burned an hour, paged the
Legate (1 of 3 daily pages), and asked for a platform repair that is not needed — while the
files it was commissioned to publish sat in its working tree.

It also produced the fourth instance of
[[deliverable_contract_satisfied_by_a_note_about_failure]]: the job sealed `completed` with
`deliverable_gate.passed: true` against
`kb:reception-cockpit-demo-publication-report-2026-08-16`, a note explaining that nothing was
published.

## What shipped

1. **The git read tools resolve per call instead of once at closure-creation.**
   `git_log`, `git_show`, `git_diff`, `git_status`, and `git_tags` take an optional
   `repo="<clone-dir>"` and read `workspace_manager.source_repos[repo]` when given it,
   the job's own repo otherwise. No new capability was needed — `source_repos` already
   held full `GitManager` instances with every method the tools call; they were simply
   bound to the wrong one.
2. **An unresolved ref now names the repos that might hold it.** A `fatal: bad object`
   with attached repos present is appended with: *"This searched the job's OWN workspace
   repository. Attached repository datasources are separate checkouts under repos/ and
   are not searched by default: KurortEngine. Retry with `repo="KurortEngine"` to read
   one of them."* That sentence is what would have ended this incident in one more tool
   call rather than an hour and an operator page.
3. **The git group is suppressed when the agent has shell tools**
   (`ToolsConfig.__post_init__`, `src/core/loader.py`). A shell can run git against any
   repository including `repos/<name>/`; granting both gave the agent two ways to ask
   one question, the weaker of which answered about a different repo without saying so.
   Suppression happens at config resolution, not in the tool registry, so
   `resolved_config.agent.tools.git` matches what the pod actually binds. The eight
   shell-having configs dropped their now-dead `git:` blocks, and the developer, scholar
   and critic prompts were rewritten to run `git ...` through `run_command`.
   `repo_*` is deliberately untouched: it carries the `read_only` enforcement on attached
   datasources, which a shell cannot replace.

Measured before the change, over the 500 most recent jobs: 83.0% had git **and** shell
(pure redundancy), 16.4% had git with no attached repo (harmless), and **0.4% — 2 jobs —
had git as the only read path with a repository attached.** Both were this project's.
No job in the sample had shell without git.

Not fixed here, and still open: the ticket named a branch and commit that a merged PR had
already deleted. See "Re-anchor refs on merge" below.

## Direction

Cheapest first:

- **Make `git_show`/`git_log`/`git_diff` refuse what they cannot answer.** If `commit_ref`
  does not resolve in the job repo *and* attached repositories exist, say so: "not found in
  the job repository; attached repositories (KurortEngine) are not readable by this tool."
  A wrong answer that reads as authoritative is worse than a refusal.
- **Add a read path for attached repos** — `repo_log` / `repo_show` at minimum, mirroring the
  `_resolve(repo)` pattern the write tools already use. This is a small, mechanical addition.
- **Add `repo_fetch` / a ref argument to checkout**, so a worker can reach a ref it can name
  rather than being limited to whatever the clone landed on.
- **Re-anchor refs on merge.** The ticket named a branch and commit that were already merged
  and deleted when it was written. Nothing in the chain noticed that a merged PR invalidates
  the refs of tickets written against its branch. (This one is unchanged from the first
  revision and still stands.)

## Acceptance

- A git read tool asked about a commit it cannot see either answers correctly or names the
  reason — never `fatal: bad object` for a repository it was never bound to.
- An agent can read the history of an attached repository datasource with a bound tool.
- A ref that genuinely cannot be resolved fails at dispatch with a precise message, not after
  a full worker run.

## Superseded framing (first revision, kept deliberately)

The original text read the worker's report as three walls: (1) the clone carries only
`origin/main`, therefore clone depth/refspec; (2) the named commit is not in the object store;
(3) no repair path is bound. Only (3) survives, and it understates the problem — there is no
*read* path either.

The error worth remembering: **the worker's evidence was accurate and its inference was not,
and I adopted the inference.** `packed-refs` really did contain one ref; `git_show` really did
say `fatal: bad object`. Both observations are consistent with a perfectly healthy clone, and
checking the upstream repo directly — three commands — would have shown that immediately.
