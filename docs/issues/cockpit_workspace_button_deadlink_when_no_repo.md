# Cockpit Workspace button emits a dead link when a job has no repo

**Status**: Backlog — small, low-risk cockpit fix. Filed 2026-06-13.

## Context

Students reported 404s when opening the **Workspace** button for certain jobs
(e.g. `1793b2a8`). Root cause was a backend bug — automation-spawned jobs never
got a Gitea repo created — fixed by extracting provisioning into
`orchestrator/services/job_provisioning.py` and calling it from the cron +
run-now paths (see `project` memory / that change). This issue is the **cockpit
half** of the same symptom, deliberately deferred so the backend fix stays
focused.

## Problem

`getWorkspaceUrl()` builds the button href as:

```ts
const repoName = job.repo_name || `job-${job.id}`;   // FULL UUID fallback
return `${giteaUrl}/${repoName}${branch ? '/src/branch/' + branch : ''}`;
```

in both:
- `cockpit/src/app/views/job-review/job-review.component.ts` (`getWorkspaceUrl()`, ~line 636)
- `cockpit/src/app/views/jobs/job-list.component.ts` (`getWorkspaceUrl()`, ~line 963)

When `repo_name` is empty/null the fallback synthesizes `job-<full-uuid>`. That
URL **can never resolve** — the actual naming convention is `job-<short8>` (the
first 8 chars) for standalone jobs, or the shared `project-<id>-jobs` repo for
project jobs. So the button sends the user to a guaranteed 404. This is exactly
the long-URL 404 students saw (`/srw/job-1793b2a8-94bd-4f3c-...`).

The backend fix means *new* jobs get `repo_name` populated, so the fallback
stops firing for them. But the fallback is still wrong for:
- the ~11 pre-existing repo-less automation jobs (not backfilled),
- any future job-creation path that legitimately has no repo,
- the brief window before provisioning lands on a freshly-created job.

A fabricated link that 404s is worse than no link — it reads as "your workspace
is broken" rather than "this job has no workspace yet."

## Proposal

When `repo_name` is falsy, **don't render a navigable Workspace button**. Either:
- hide the button entirely, or
- disable it with a tooltip ("No workspace repository for this job"),

and have `getWorkspaceUrl()` return `null` instead of synthesizing
`job-${job.id}`. Drop the `|| \`job-${job.id}\`` fallback in both components;
callers already null-check the return in at least the job-review path.

Optionally surface a tiny "provisioning…" affordance for the transient
just-created window, but that's polish — the core fix is: never emit a URL we
know is wrong.

## Acceptance

- A job with `repo_name = null` shows no clickable Workspace link (or a disabled
  one), never a `job-<full-uuid>` href.
- A job with `repo_name` set behaves exactly as today.
- Vitest specs for both components cover the null-`repo_name` branch.

## Notes

Low risk, isolated to two Angular components + their specs. No backend change.
Pairs with the backend provisioning parity fix; that one stops the bleeding,
this one stops the misleading UI.
