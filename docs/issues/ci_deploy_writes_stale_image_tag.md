# CI deploy can write a stale image tag when runs overlap

**Status:** **Real but self-correcting; guard implemented on `develop` 2026-08-13.**
The guard has **not yet been exercised in CI** — the commit carrying it touched only `.github/`
and `docs/`, so no component built and every update step was skipped. It first runs on the next
commit touching component paths.
**Severity:** **Low-medium** — revised down from "medium-high", see §Retraction. Dev only, and
the next component build corrects it. No data loss, no lost verification.

> **The first version of this doc (same day) was substantially wrong.** It claimed a verified
> deploy had been silently *reverted*. It had not. The retraction is kept rather than deleted,
> because the mistake is more instructive than the bug — see §Retraction.
>
> Renamed from `ci_deploy_race_reverts_image_tags.md`; the old filename asserted the error.

## What actually happens

The `changes` job computes each component's identity at the **start** of a run:

```bash
last_input_sha() { git log -1 --format=%H HEAD -- "$@"; }
ORCHESTRATOR_PATHS=(orchestrator/ src/ config/ docker/Dockerfile.orchestrator)
```

`deploy-experimental` writes that value **at the end** of the run, minutes later. If another
commit touching the same paths lands in between, the run writes an identity already behind the
branch.

Observed 2026-08-13 (UTC):

| time | event |
|---|---|
| 06:00 | `0ef826ca` (KB reindex fixes) committed |
| 06:04:38 | pushed; run starts, computes `orchestrator-sha = 0ef826ca` |
| 06:08 | `9dd51755` committed — newer, also touches orchestrator paths |
| 06:09:38 | `9dd51755` reaches origin |
| **06:13:05** | **`b00ada17` writes `tag: sha-0ef826c` — already stale by 3½ minutes** |
| 06:22:41 | `2517b73c` writes `tag: sha-9dd5175` — forward correction |

The stale write is real, and **self-correcting**: any later run that builds the component writes
the current identity. The exposure is the window in between, plus the case where no further build
follows — dev then runs code older than the branch until someone touches that component again.

## Retraction — what the first version got wrong, and why

The original claim was that `2517b73c` **reverted** a verified deploy
(`- tag: sha-0ef826c` → `+ tag: sha-9dd5175`).

That diff is real; the conclusion was not. **`0ef826ca` is an ancestor of `9dd51755`**, so
`sha-9dd5175` is a *newer* image that already *contains* the KB fixes. It was a forward update.
Three claims fell with it:

| original claim | reality |
|---|---|
| "silently reverts a verified deploy" | Dev moved **forward**. The fix was deployed continuously. |
| "verification invalidated, had to be redone" | The rehearsal passed because the image genuinely had the fix. Nothing was invalidated. |
| "image tags are mutable — same tag, different content" | **No mutation.** The `workflow_dispatch` rebuild ran against a docs-only head: `git log 9dd51755..a6b20e38 -- <orchestrator paths>` is **empty**. Identical inputs, identical image. |

**Root cause of the error:** commit *order* was inferred by reading `git log` output on a branch
that a second agent rebases continuously, instead of being tested with
`git merge-base --is-ancestor`. Everything downstream followed from that one unchecked ordering
assumption. The direction of the race was backwards too: **this session's own run wrote the stale
tag**; the other run corrected it.

**The lesson worth keeping:** on a branch with concurrent rewriters, *"which commit is newer"* is
a question to answer with a command, never by eye. `git merge-base --is-ancestor` takes seconds;
the wrong answer propagated into a spec, a backlog row, three commit messages and a CI change.

## Second defect (real, cosmetic): the deploy commit contradicts itself

`2517b73c`'s message and `deployment/fleet.yaml` say `sha-2b03875` (the **run** sha), while
`deployment/values-experimental.yaml` says `sha-9dd5175` (the **component** identity). Both are
correct by their own rule; together they make the commit unreadable — and they were a direct
contributor to the misdiagnosis above. Worth aligning the message with what was actually written.

## Fix (implemented)

Still correct and still worth having: it prevents a component tag from ever moving backwards,
whichever run is the stale one.

1. `fetch-depth: 0` on the `deploy-experimental` checkout (it was shallow; ancestry needs history).
2. A guard on all five per-component steps (orchestrator, agent, cockpit, mcp, workspace): skip
   the write when the revision recorded in `provenance.components.<c>.sourceRevision` is **not an
   ancestor** of the one about to be written. `vmController`/`agentVmBase` are untouched — no
   per-component identity.

The guard **fails open**: it skips only when the recorded revision is non-empty **and** still
exists **and** is not an ancestor. That third condition matters — this branch is rebased often, a
recorded sha can vanish, and `git merge-base --is-ancestor` *errors* on an unknown object. Without
the `git cat-file -e` check the negation would read that error as "not an ancestor" and block
**every** future deploy — far worse than the bug.

Verified without CI (exercising it needs two overlapping runs):

| case | result |
|---|---|
| deployed older, writing newer (normal) | WRITE |
| **deployed newer, writing older (the race)** | **SKIP** |
| deployed == writing (re-run) | WRITE (idempotent) |
| recorded sha rebased away / unknown | WRITE (fails open) |
| provenance empty (first deploy) | WRITE |

Checked against the exact binary CI installs (mikefarah yq v4.53.3): `-r` accepted, `// ""` works,
missing component key yields empty rather than erroring. Workflow YAML reparsed (21 jobs).

**Alternatives rejected:** `cancel-in-progress: true` (useless once the stale run has finished);
computing tags from the deploy-time checkout (would reference images not yet built); deploying
only when `github.sha` is the branch tip (a docs-only tip would block deploying code underneath).

## Verification still owed

Needs two deliberately overlapping runs:

1. Push a commit touching orchestrator paths; immediately push a second so the runs overlap.
2. Confirm the final `values-experimental.yaml` orchestrator tag is the **newer** identity.
3. Confirm the stale run logged `skipping stale orchestrator tag` instead of writing.
