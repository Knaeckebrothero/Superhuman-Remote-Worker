---
name: repo-contribution
description: Use when a job's deliverable is a change to a git repository attached as a repository datasource — the branch, commit, push, and pull-request mechanics of landing that change for human review. Covers working inside repos/<name>/ instead of the workspace, setting a commit identity, cutting a job branch off the default branch, pushing, and writing the PR description your reviewer will actually use. Does not cover how to write the change itself (that's test-driven-development) or how to prove it works before claiming done (that's verify-before-done) — this is the delivery mechanics wrapped around them. For contributing to an external repo you hold write access to; not for the internal per-job workspace repo, which the system versions for you automatically.
display_name: Repository Contribution
icon: commit
color: "#89b4fa"
tags:
  - git
  - github
  - pull-request
  - development
  - delivery
---

# Repository Contribution

Your job's output is a change to someone's real repository — one with history,
collaborators, and a maintainer who did not watch you work. That changes what
"done" means. The code being correct is necessary and not sufficient: it has to
arrive on a branch they expect, in commits they can read, with a description that
lets them judge it without re-deriving your reasoning.

The reviewer is the bottleneck, not you. A perfect change described as "fixed the
thing" costs them the entire investigation you already did. Your PR description is
the deliverable just as much as the diff is.

Two structural facts to internalize before you start. First, **the repository is
cloned under `repos/<name>/`, not in your workspace root** — edits outside it are
invisible to the push and will be silently lost at teardown. Second, **you almost
certainly cannot push to the default branch.** Protected branches are enforced by
the forge, not by your tooling, so a push to `main` or `develop` will be rejected
by the server. That rejection is a correct outcome, not a bug to route around: it
means the guardrail is working. Cut a branch.

## The contribution

**1. Find the clone and enter it.** The repo lives at `repos/<name>/` — check
`datasources.md` for the exact directory name; it is derived from the upstream URL
and **preserves case**, so it may not match the lowercase name you expect. Every
git command in this skill runs with `-C repos/<name>` or from inside that
directory. If you edit files anywhere else, they are not part of your change.
That same `<name>` is the `repo` argument every `repo_*` tool below expects —
copy it exactly, case included.

**2. Set a commit identity.** The default identity is a generic placeholder. Set
something honest that identifies the agent:

```bash
git -C repos/<name> config user.name  "<agent name>"
git -C repos/<name> config user.email "<agent email>"
```

Be aware this is a *label*, not an attestation — git config identity is a string
anyone can set. Don't claim provenance it doesn't carry.

**3. Cut your branch off the base.** Default to the datasource's default branch as
the base unless the task says otherwise. Name the branch `job/<short_id>`, where
`<short_id>` is the **first 8 characters of your job id** — this convention is what
lets the reviewer's tooling find your branch from the job.

```bash
git -C repos/<name> fetch origin
git -C repos/<name> checkout -b job/<short_id> origin/<base-branch>
```

Branch first, before you edit anything. Cutting the branch after you've made
changes works, but it's how you end up accidentally committing to the base.
There's no tool for this step — branching stays a shell command; the `repo_*`
tools you'll meet later cover commit, push, pull, and opening the PR, not
checking out.

**4. Read the task fully before editing.** If it references an issue or design
document in the repo, read that document — it usually contains constraints, prior
decisions, and rejected approaches that the task summary omits. Follow the
surrounding code's conventions over your own preferences: match its naming, error
handling, test layout, and comment density. A change that reads as foreign is
harder to review even when it's correct.

**5. Implement in reviewable commits.** Prefer several coherent commits over one
giant one — the reviewer reads commit-by-commit. Write messages that say *why*,
not just *what*; the diff already shows what. Keep unrelated cleanups out: a
drive-by refactor buried in a bugfix is the single most common reason a good change
gets bounced.

**6. Run the repository's checks — and be honest about what you couldn't run.**
Find the project's actual gate (test command, linter, type checker) and run it.
Then state the result plainly in step 8.

If the suite **cannot** run in this workspace — missing dependencies, wrong
interpreter version, needs services you don't have — do not quietly skip this step
and do not imply you verified anything. Say explicitly, in the PR description, which
checks you ran, which you could not, and why. An unverified change that is labeled
unverified is useful; an unverified change that reads as tested is a trap you set
for your reviewer. If CI will run the real gate, say that too.

**7. Commit and push your branch.** From here on, use the `repo_*` tools instead
of raw git for anything that touches the remote. The three write tools —
`repo_commit`, `repo_push`, `repo_open_pr` — all refuse outright if the datasource
is attached read-only, or if its forge metadata is missing entirely; both fail
closed instead of guessing (`repo_pull` is exempt from both checks and always
works). None of that should apply to a repo you were told to contribute to, but if
a call comes back with that refusal, believe it — don't route around it with the
shell.

```
repo_commit(repo="<name>", message="<type>(<scope>): <what and why>")
repo_push(repo="<name>")
```

`repo_push` defaults to whatever branch is currently checked out, so it pushes
`job/<short_id>` without you naming it again. `repo_commit` stages every change in
the clone before committing; if there's nothing to commit it tells you rather than
manufacturing an empty commit — treat that as a cue to check `git status`, not a
bug.

If `repo_push` reports a rejection, you almost certainly targeted a protected
branch — re-read step 3. If it reports a credentials problem, stop and report it;
don't reach for the shell to try alternate remotes or force the push through.

**8. Write `output/pr.md` — the reviewer's entry point.** Use the scaffold below.
This is what the human will paste as the pull-request description, so write it for
them, not as a log of your session. It also becomes the `body` you hand to
`repo_open_pr` next, so leave it as that finished artifact rather than draft notes.

**9. Open the pull request.**

```
repo_open_pr(
    repo="<name>",
    title="<type>(<scope>): <one-line summary>",
    base="<base branch from step 3>",
    body=<contents of output/pr.md>,
)
```

Call this only after `repo_push` has succeeded — the forge rejects a pull request
whose head branch doesn't exist on the remote yet, and getting that order backwards
is the most common way this step fails. `head` defaults to the branch you're
already on, so name it only if you're opening the PR from somewhere else. If the
call fails after a successful push, nothing is lost: `output/pr.md` is still on
disk, so you or the human reviewer can open the PR by hand with that file as the
description.

**10. Stop.** Do not merge, do not push to the base branch, and do not mark the
work complete yourself. The job freezes for human review by design — that gate is
the whole point of the workflow. If you're resumed with feedback, push more
commits to the *same* branch; the pull request updates in place.

## `output/pr.md` scaffold

```markdown
# <type>(<scope>): <one-line summary>

## What
<2–4 sentences: what changed and why. Lead with the problem, not the diff.>

## Approach
<Why this way. Name the alternatives you rejected and what ruled them out —
this is the part the reviewer cannot reconstruct on their own.>

## Testing
| Check | Command | Result |
|---|---|---|
| <suite/linter> | `<command>` | passed / failed / **could not run — <reason>** |

<If you could not verify something, say so here in plain words.>

## Risks & follow-ups
<What might break, what you deliberately left out, what should happen next.>

## Files
<Notable files touched and why — skip if the diff is small and obvious.>
```

## Don't

- **Edit outside `repos/<name>/`** — those changes are not in the repo and vanish at teardown.
- **Commit to or push the base branch** — cut `job/<short_id>`; a rejected push means the guardrail worked.
- **Merge your own work, or self-approve** — the human review gate is the point.
- **Imply you tested what you didn't run** — name the checks you skipped and why; a labeled unverified change is honest, an unlabeled one is a trap.
- **Bundle unrelated changes** — a drive-by refactor inside a bugfix gets the whole PR bounced.
- **Write the PR description as a session log** — the reviewer needs the *why* and the risks, not your narration.
- **Commit secrets, credentials, or workspace scratch files** — `repo_commit` stages everything in the clone, so run `git status` before calling it, and respect the repo's `.gitignore`.
- **Reach for the shell to force-push or rewrite history on a pushed branch** — `repo_push` has no force option by design; if it's rejected, push new commits instead of overwriting ones the reviewer may already be reading.
