# `full` autonomy does not guarantee a job completes

**Status:** **Open by decision, 2026-08-15.** Not a defect — current behaviour is deliberate
and arguably correct. Filed because the *name* promises something the ladder does not
deliver, and because a second condition of the same shape is about to be added.
**Severity:** **Low today, rising.** Exactly one condition can force the downgrade, and the
job is recoverable by a human in two clicks. It becomes a real problem the moment unattended
runs depend on `full` meaning terminal.

`file:line` as of `163e23b0`.

## The ladder

`src/core/capability_grants.py:16`:

```python
_AUTONOMY_ORDER = ["dependent", "guided", "partial", "review", "full"]
```

`full` is the top. The contract a reader infers is "this job completes without me". The
contract the code implements is "this job completes without me **unless** something it
produced needs a human decision".

## The asterisk

`orchestrator/main.py:24989`:

```python
if mode_a_capture["captured"] and new_status == "completed":
    new_status = "pending_review"
    actions.append("mode A diff captured -> pending_review")
```

An `autonomy=full` job normally transitions straight to `completed` (`main.py:17768-17775`),
never entering `pending_review`. This is the one path that overrides that.

**It is narrower than it looks.** The guard above it (`main.py:24950-24955`) requires all of:

- `job.cloud_diff_baseline_commit` is set — the job is Mode A, attached to a project cloud
  folder;
- `new_status` is already terminal;
- Gitea is reachable;
- **`not _completion_loop_id`** — *loop jobs are explicitly excluded.*

So the population is "cloud-folder-attached non-loop jobs that changed files". A loop cannot
get stuck on this today, which is the case that would matter most.

## Why the current behaviour is defensible

The diff is unreviewed change to a **user's own cloud storage**. Writing it back without a
human look is the kind of thing that is very hard to undo and very easy to regret. Stopping
is the conservative choice, and the job is not lost — `accept_job_diff` / reject resolve it.

## Why it is still wrong-ish

`full` is a *ceiling*, chosen deliberately by someone who accepted the risk. Silently
demoting it re-imposes a review they opted out of, and it does so through a mechanism they
cannot see from the autonomy setting itself. For an unattended run the worst outcome is not a
bad write — it is **stalling**, because a stalled run produces nothing and nobody is watching
to unstick it.

The user's framing, 2026-08-15:

> "the worst thing that can happen to your loop is to get stuck on something if you want it
> to just yolo. And if you want it to stop then you can choose the less dangerous level."

## The shape of a fix

Add a level **above** `full` that never downgrades — the explicit "I accept a bad write over
a stall" choice — and leave `full` exactly as it is. Then the ladder encodes the trade-off
instead of hiding it, and neither existing setting changes meaning.

The grants machinery already makes this safe to add:

- `autonomy_ceiling` is a `restrict_only` enum ordered by `_AUTONOMY_ORDER`, default
  **`review`** (`capability_grants.py:65-70`).
- Appending a level to the end of that list therefore grants it to **nobody** by default. It
  would require an explicit `autonomy_ceiling` grant at user, project, or global scope, and
  `meet` keeps a child from widening past a parent cap.

So the dangerous level is opt-in by construction, with no grandfathering and no migration
risk of the kind `0030` had to absorb.

Open questions, deliberately not answered here:

- Name. `full` is already the superlative, which is what makes this awkward.
- Whether it suppresses *every* future downgrade condition automatically, or names them.
  Blanket suppression is the honest reading of the setting and the one that stays correct as
  conditions are added; per-condition opt-out is safer but grows a matrix.
- Whether an unattended run should signal the skipped review somewhere rather than silently
  proceeding.

## Explicitly deferred — scope fence

Decided 2026-08-15 while designing the "require a merged PR before approval" gate: that gate
follows the **existing** pattern rather than waiting on this. It may force a `full`-autonomy
job into `pending_review`, exactly as a captured cloud diff does. Consistency now, ladder
change later.

This means the condition count goes from one to two before this is addressed. That is the
accepted cost, and it slightly strengthens the case for the new level — a second condition is
what turns "an odd exception" into "a pattern that needs a name".

## Related

- `docs/features/job_review_delivery_links_and_review_session.md` — the PR gate's home.
- `docs/done/job_cloud_export.md` §3.5 — Mode A accept/reject, the recovery path.
