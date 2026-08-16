# The deliverable gate cannot see deliverables in a cloned repo datasource

**Status:** **FIXED on develop `997e9a58`** (2026-08-15), both halves, not yet deployed.
Observed live on dev 2026-08-14 (job `29c28492`). The one occurrence was worked around *by the
agent*, not by an operator — see below, because the workaround is itself the problem.

What shipped, against "Suggested fix" below: creation refuses the manifest (a `JobCreate`
validator naming the reason and the alternative), and the gate fails open per entry rather
than bouncing — checking the tree FIRST, so a path that really is committed reports `present`
rather than excused. Both consume one shared predicate,
`is_cloned_repo_deliverable` / `cloned_repo_deliverables`. Of the two variants offered in (2),
the skip-with-reason one was taken: the gate stays forge-unaware, and the loop's own delivery
guard (`a040dd31`) remains the thing that catches a turn which genuinely delivered nothing, so
failing open here opens no hole. The pass-path action line was also corrected — it had called
every fail-open a "kb" entry, which would have described a check that never ran.
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

Job `29c28492` completed correctly — clone → branch → commit → push → **PR #1**.
`job_complete` returned:

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

`context.pull_request` is the record the gate would need: orchestrator-written at tool-call
time, `verified: true`, and `parse_job_pull_request` fails closed on a malformed record. The
gate does not look at it.

> **Correction (2026-08-16).** An earlier revision of this document stated that job
> `29c28492`'s PR **was** recorded into `jobs.context.pull_request`. It was not. The live row
> has `pull_request: null`, as does every job in the project — the `repo_open_pr` persist
> (`23bbf28a`) shipped at 09:19 UTC on 2026-08-15, roughly 7.3 hours *after* this job ran, the
> same timeline already given under "Loose thread" below. The mechanism described above is
> real and available to new jobs; it simply had not shipped when this one executed, so this
> job is not an example of it. Verified against the database 2026-08-16.

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

## Loose thread — CLOSED, harmless

The agent's freeze notes say remote PR status "could not be re-queried in the corrective final
review because no remote PR tool was available". That is simply true and not a defect: the
tool did not exist yet.

| event | UTC |
|---|---|
| job `29c28492` ran | `00:44:33` → `~02:12` |
| `repo_pr_status` committed (`23bbf28a`) | `09:19:43` |
| deployed as `sha-6b15c93` (`ce9f2873`) | `09:33:33` |

The job finished **~7.3 hours before the tool was written**. It is also not a critic
observation — the record is `freeze_type: job_complete`, written by the job about itself, and
this was a manual job with no critic.

It will not recur: `repo_pr_status` is granted under BOTH repository tiers
(`src/core/datasource_setup.py:130` read, `:136` write), so any job with a repository
datasource attached can read live PR state regardless of access level.

## Related

- `docs/features/better_resavio_restart_status.md` §5 (the job) and §6a (the sibling fix).
- [`kb_read_missed_an_indexed_note_unexplained`](kb_read_missed_an_indexed_note_unexplained.md)
  — found in the same job.
- [`deliverable_contract_satisfied_by_a_note_about_failure`](deliverable_contract_satisfied_by_a_note_about_failure.md)
  — **the consequence of the fix shipped here.** The creation-time refusal works exactly as
  designed, and the officer's response to it was to rewrite the contract into a `kb:` slug that
  a note about the failure then satisfied. The refusal message names the right alternative
  ("the pull request the agent opens") and nothing yet accepts that contract type, so the only
  reachable escape launders the failure. Read the two together.
