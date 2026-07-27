---
tags:
  - issue
  - fix-spec
  - jobs
  - agent
  - workspace-lifecycle
  - git-versioning
---

# Resume onto a fresh workspace never falls back to cloning the job repo — the agent silently restarts the job from zero

**Filed:** 2026-07-27, from job `52949749` ("historische Kernwerke") on dev.
**Status:** CONFIRMED in code + live incident 2026-07-25. UNFIXED.
**Severity:** **high** — total, silent loss of work continuity. The agent
believes it is starting a new job and re-plans from scratch while the entire
committed history sits intact in Gitea.
**Component:** `src/agent.py` (~2008–2070), `src/managers/git_manager.py`,
`src/core/workspace.py`.

## Symptom

After the job's workspace pod **and** its PVC were gone (terminal-state PVC
reclaim — see `failed_job_pvc_reclaimed_without_grace_period.md`), the resumed
agent came up in an empty workspace, initialized a **blank** git repo, and
re-planned the whole job as if it were new. It authored a parallel research
corpus (`research/topic_*.md`, 14 files) alongside — but never reading — the
original `research/candidates.md` + `editions-*.md`, and made **zero** writes
to the actual deliverable `output/kernwerke.md`, which was the thing it had
been resumed to remediate.

Every committed phase of that work was, the whole time, sitting in the job's
Gitea repo (`job-52949749.git`, HEAD = `a8117788 "Job frozen for review"`).
Nothing on this code path consults it.

## Root cause — three workspace-init paths, one hole

| Situation | Path taken | Anchor |
|---|---|---|
| First dispatch | **Clone the job repo** | `git_manager.py:837` *"Cloned http://…/job-52949749.git to /home/agent-host/workspace"*, `workspace.py:432` *"Git versioning enabled (cloned from remote)"* |
| Resume, volume reattached (`.git` present on backend) | Preserve, no clone, no re-init (G2) | `agent.py:2052–2067` |
| **Resume, fresh/empty workspace** | Seed from last phase snapshot — **and if there is no snapshot, do nothing** | `agent.py:2009–2039`; the dead end is the `else` at **`agent.py:2037`** |

The third path's only recovery source is the phase-snapshot store:

```python
if not workspace_backend.exists("task_brief.md"):
    logger.info(f"VM workspace is fresh — seeding from last snapshot for job {job_id}")
    latest = recovery_mgr.get_latest_snapshot()
    if latest:
        recovery_mgr.recover_to_phase(...)
    else:
        logger.warning("No snapshots available to seed VM workspace")   # ← falls through
```

With no snapshot it warns and falls through; the reattach probe then finds no
`.git`, so control reaches the ordinary local-path init and the workspace is
created **blank**. The job repo — the canonical, durable, always-present
record — is never tried.

## Evidence (2026-07-25, job `52949749`)

```
16:34:12.379 INFO    VM workspace is fresh — seeding from last snapshot for job 52949749…  (agent.py:2012)
16:34:12.379 WARNING No snapshots available to seed VM workspace                            (agent.py:2037)
16:34:15.697 INFO    Initialized git repository in /workspace                               (git_manager.py:194)
```

Downstream, from the audit trail: the session's writes from 20:59 onward are
all `research/topic_*.md`, `plan.md`, and `archive/*` — a brand-new corpus —
and there is **no** `write_file`/`edit_file` against `output/kernwerke.md` in
that session (its last edit anywhere is 2026-07-23 22:33:46Z).

**Why no snapshot existed:** the phase-snapshot mechanism had been logging
`Snapshot: checkpoint.db not found at /workspace/checkpoints/job_<id>.db`
(`phase_snapshot.py:250`) throughout the original run, so the seed source this
path depends on was already absent. That deserves its own investigation — but
the fix here must not depend on snapshots existing, because the job repo is
the more reliable source anyway.

## Cost in this incident

~7 hours of LLM work re-deriving research that already existed in Gitea; the
remediation the resume was *for* was never performed; and the resulting
"nothing changed" deliverable then drove a second critic return and the rest
of the cascade (see Related).

## Fix proposal

1. **Add the clone fallback.** On resume + fresh workspace, try in order:
   snapshot → **clone the job repo** (the same call first dispatch makes) →
   blank init. The repo is authenticated and reachable on exactly the same
   code path already used at dispatch, so this is a small, well-understood
   addition.
2. **Make the taken path unambiguous in the logs.** Today a blank init after
   a failed seed is indistinguishable from a legitimate first dispatch; log
   `workspace_init_path=clone|snapshot|reattach|blank` explicitly.
3. **Consider refusing rather than silently restarting.** If `resume=True`,
   the workspace ends up empty, *and* the job repo has commits, that is a
   state worth reporting instead of papering over — the refuse-and-shed
   pattern used for the missing-workspace case is the precedent.

## Related

- `docs/issues/failed_job_pvc_reclaimed_without_grace_period.md` — why the
  workspace was fresh in the first place.
- `docs/issues/bound_skill_missing_from_resume_blob_deadlocks_phase_transition.md`
  — the deadlock that hit immediately afterwards on the same fresh workspace;
  the two defects compound.
- `docs/issues/maxsessions_parallel_tools_false_workspace_death.md` — the
  incident chain this was found in.
