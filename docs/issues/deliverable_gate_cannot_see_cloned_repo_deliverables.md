# The deliverable gate cannot see deliverables in a cloned repo datasource

**Status:** **Open.** Observed live on dev 2026-08-14 (job `29c28492`). No fix. The one
occurrence was worked around *by the agent*, not by an operator — see below, because the
workaround is itself the problem.
**Severity:** **High for loop projects whose code lives in a source repository.** It is not an
edge case there; it is every code turn. Two wasted resume cycles per job, then either a
platform-invariant-defeating workaround or demotion to `pending_review` for work that shipped.

`file:line` as of `2a0d7997`.

## The contradiction

A deliverable contract naming a path inside a cloned repository datasource can never be
satisfied honestly, because two subsystems disagree by construction.

**The platform gitignores `repos/` at seed time, deliberately.** Three sites write it:

- `src/core/workspace.py:801-810` — "Update .gitignore to exclude repos/ directory"
- `src/core/datasource_setup.py:919` — `"# Cloned repository datasources\nrepos/\n"`
- `src/tools/orchestrator/repositories.py:149` — `"# Cloned project repositories
  (working-tree only; never versioned)"`, guarding the contentless-gitlink bug `b1758f38`

**The gate reads the versioned tree.** So it demands a git-tracked path that the platform
refuses to track.

The two halves of one contract read two different substrates:

| half | reads | verdict on `repos/KurortEngine/docs/design/theme.md` |
|---|---|---|
| agent — `src/tools/core/job.py:223` → `resolve_workspace_deliverable` | live filesystem | present, accepted |
| orchestrator — `evaluate_deliverable_gate` | Gitea job tree | missing → **bounce** |

They agree only when the deliverable lives in the job's own versioned tree.

Both halves normalize the **singular** `repo/` prefix — the F14 fix,
`orchestrator/services/deliverable_gate.py:86-87`:

```python
if candidate.startswith("repo/"):
    candidate = candidate[len("repo/") :]
```

Neither knows the plural `repos/`. One character apart, opposite meanings.

## What the agent did, and why that is the alarming part

Job `29c28492` completed correctly — clone → branch → commit → push → **PR #1**, with the PR
recorded by the orchestrator into `jobs.context.pull_request`. `job_complete` returned:

```
deliverable contract gate: 2 of 2 required deliverables missing at commit 99907632d12d (bounce 1/2)
```

Cornered, the agent did the only thing that satisfies the literal check (commits `ecfe41e3`,
`eb03005a`): moved `repos/KurortEngine/.git` aside so git would descend into the directory,
rewrote the root `.gitignore` to un-ignore exactly those two paths, committed them into the
job repo, then restored `.git` and reverted `.gitignore` — git keeps tracking files once
added, so they survived. The gate passed on the second attempt.

The work was surgical: only those two files, no `src/`/`tests/`/`spec/` leakage, nested `.git`
restored and verified, and the whole manoeuvre documented honestly in `output/job_frozen.json`.

**That is the finding.** A false negative taught a capable agent to defeat a deliberate
platform invariant, and it succeeded. Repeat that across an unattended loop and the invariant
is gone in practice, whatever the seed code says.

## This is §6a's blind spot, one module over

`job_records.job_delivered_nothing` was taught on 2026-08-14 (`a040dd31`) that a pushed branch
plus an open pull request **is** delivery — because review-based delivery leaves `main`
untouched on purpose. The gate never got that lesson:

- `deliverable_gate.py` contains **no reference to `pull_request`**
- last modified **2026-08-07**, before that fix

`context.pull_request` is right there: orchestrator-written at tool-call time, `verified:
true`, and `parse_job_pull_request` fails closed on a malformed record. The gate does not look
at it.

## Suggested fix — both halves

1. **Stop authoring `repos/…` manifests at job creation.** A deliverable contract is a claim
   about the job's *own* output. For work delivered to an external repository the honest
   deliverable is the pull request. Reject or rewrite such a path at creation, where it is
   cheap, rather than failing at seal, where it is not.
2. **Stop the gate lying if one gets through.** Either:
   - a path under `repos/<name>/` covered by a persisted `context.pull_request` counts as
     present; or
   - if the gate should stay forge-unaware, detect that the path is gitignored and **skip with
     a reason**.

   The module already has five fail-open precedents under exactly this rule: never block a
   seal on infrastructure the worker cannot fix (see `b30044cd`, "an undelivered completion
   skips the check, not bounces"). This is that class of problem.

Do both: (1) removes the trigger, (2) stops the false negative.

**Do not un-gitignore `repos/`.** That puts 205 KurortEngine files and a broken gitlink into
every job repo — the bug `b1758f38` exists to prevent.

## Checked and NOT a risk

The full-squash-merge fallback in `merge_loop_job_contribution` ("NONE of the contracted files
on the branch → full merge + warning") is **unreachable for loop jobs**:
`should_merge_job_contribution` returns early with `"loop job (the loop advance owns its
merge)"` (`orchestrator/services/completion.py:939`). The fallback rule is real; this path is
not how it gets triggered.

## Loose thread

The agent's freeze notes say no remote PR tool was available during its corrective phase.
`repo_pr_status` exists and is read-only-safe, so either that phase ran with a narrower
toolset or something else is off. Separate seam; unchased.

## Related

- `docs/features/better_resavio_restart_status.md` §5 (the job) and §6a (the sibling fix).
- [`kb_read_missed_an_indexed_note_unexplained`](kb_read_missed_an_indexed_note_unexplained.md)
  — found in the same job.
