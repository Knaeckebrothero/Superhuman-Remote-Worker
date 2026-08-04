---
tags:
  - architecture
  - projects
  - git-integration
  - cloud-storage
  - migration
related:
  - "[[repo_resolution]]"
  - "[[workspace_and_change_records]]"
  - "[[job_cloud_export]]"
  - "[[project_self_improvement_loop]]"
---

# Retire the Shared Project Jobs Repository

**Status: accepted 2026-08-04; new-flow cutover implemented. Legacy backfill
and repository archival remain an operator migration.**

This decision supersedes the parts of `workspace_and_change_records.md`,
`loop_repo_compounding_v2.md`, and `projects.md` that make a shared
`role='jobs'` repository the project workspace, project history, or loop
artifact. It reaffirms `repo_resolution.md`: projects are database-backed
resource hubs, while every root job gets an isolated execution repository.

## Decision

- New projects do not receive a `role='jobs'` repository.
- Every new root job receives `job-<short-id>` whether or not it belongs to a
  project. Subjobs remain branches in the root job's repository.
- A job repository is an isolated execution and recovery surface. It is not a
  project template and it is not the canonical project history.
- Job completion/change records are structured database rows, not Markdown
  files committed under `retros/`.
- The dedicated `role='knowledge'` repository remains server-side and is never
  cloned into a job workspace.
- Source/reference repositories remain explicit project attachments cloned
  below `repos/`. Code and files remain canonical in their external repository
  or cloud destination.
- A future workspace template, if needed, is an explicit version-pinned seed.
  Project history is never used implicitly as a template.

## Loop file destination

File-producing project loops use the project's main cloud folder. They never
merge a job branch into a shared project repository.

During the compatibility phase this reuses Mode A:

1. A loop root job gets its own `job-<short-id>` repository.
2. The orchestrator snapshots the project cloud folder into
   `projects/<project-slug>/` and records the baseline.
3. The agent changes that filesystem copy and commits to its isolated job repo.
4. On successful completion, a non-empty loop diff is checked against the live
   cloud etag baseline.
5. A conflict-free diff is applied automatically. This preserves the loop's
   existing unattended semantics: its old shared-main merge was automatic too.
6. External conflicts or partial cloud writes are not ignored. The job remains
   reviewable and the loop does not advance until the outcome is resolved.
7. The next loop job receives a fresh cloud baseline, so cloud storage—not a
   project Git branch—is the causal hand-off between file-producing turns.

Analysis-only loop jobs continue to coordinate through the project knowledge
base. An empty project-folder diff is therefore normal for scholar, critic, and
product-QA roles.

Projects without a main cloud folder cannot start a new loop. This is a loud
configuration error rather than permission to recreate a hidden Git artifact.

## Structured history

One append-only job change record is retained per terminal job. It carries:

- job/project/loop identity and role;
- terminal status and timestamps;
- isolated repo and branch references;
- delivery status and destination reference (including a cloud apply);
- completion notes, errors, and delivery observations;
- orchestrator-verified KB note ids; and
- agent-declared changes, permanently marked unverified unless separately
  verified.

This record is queryable by project and job. PostgreSQL is authoritative; a
Cockpit view can render it without cloning a repository.

## Legacy compatibility and migration

Existing jobs keep their stored `repo_name` and `branch_name`; in-flight work is
never moved. Existing `role='jobs'` repositories remain registered and
readable during migration, but no new flow writes job records or loop output to
them.

Before a legacy jobs repository can be archived or deleted:

1. the project must have a dedicated `role='knowledge'` repository;
2. any legacy `knowledge/` material must be copied and reindexed;
3. any `experts/` configuration still in the repo must be migrated to the
   database-backed expert catalog;
4. no non-terminal job may reference the repository; and
5. operators must have an explicit archive/export, rather than job deletion
   implicitly deleting a shared repository.

The `jobs` role stays in the database vocabulary temporarily so old rows remain
readable. Public project-repository creation no longer admits that role.

## Implementation sequence

1. Stop jobs-repo provisioning for projects and promotion flows.
2. Always provision isolated root-job repos and decouple auxiliary repository
   cloning from workspace-root selection.
3. Persist structured job change records and stop `retros/` writes.
4. Replace loop merges with cloud diff delivery and fresh-baseline hand-off.
5. Remove new-flow API/UI/tool assumptions that a default jobs repo exists.
6. Backfill and archive legacy repositories only after the guards above pass.

## k3d acceptance (2026-08-04)

The new flow was exercised against a clean Tilt/Helm rebuild of the local k3d
stack while retaining the existing PostgreSQL, Gitea, and Nextcloud PVCs.

- Migrations `0080_job_change_records.sql` and
  `0081_job_change_records_survive_job_deletion.sql` remained checksum-clean;
  `job_change_records.job_id` has no job foreign key after the latter.
- A newly provisioned project exposed only its dedicated `knowledge`
  repository. Its loop job used `job-<short-id>` with no branch, and the live
  workspace contained the isolated repository, runtime scaffold, and current
  project-cloud seed only—no knowledge checkout or previous-job directories.
- A binary file containing non-UTF-8 bytes was committed to the isolated job
  repository and delivered byte-for-byte to Nextcloud. The loop completed,
  its structured record reported `cloud-applied`, and a later agent outage
  callback could not reopen the completed job.
- A second run changed the same file independently in the job repository and
  Nextcloud after baseline capture. Completion stopped at `pending_review`
  with `cloud-conflict`; a later outage callback could not bypass the gate.
  Rejecting the diff through the authenticated API preserved the newer cloud
  bytes, completed the loop, and wrote a `cloud-rejected` record whose agent
  claim remained explicitly unverified.
- Deleting the first completed job through the authenticated API removed its
  isolated Gitea repository and job row. The project history endpoint still
  returned its structured record, validating that execution cleanup does not
  erase project history.
