---
tags:
  - feature
  - cockpit
  - jobs
  - git-integration
  - sessions
  - review
status: proposed
created: 2026-08-14
related:
  - "[[repo_datasource]]"
  - "[[project_jobs_repo_retirement]]"
  - "[[persistent_session_source_of_truth]]"
  - "[[headless_persistent_sessions]]"
  - "[[builder_to_sessions_consolidation]]"
---

# Job review: link the real delivery, and review it with the agent

**Status:** Proposed 2026-08-14. Motivated by a live job whose review screen pointed
everywhere except at the work. `file:line` references verified at `81cfa654`.

## 1. The problem, from a real job

Job `29c28492` designed a visual theme and delivered it as **PR #1 on
`github.com/Knaeckebrothero/KurortEngine`** — a pushed branch, 1,348 lines, two contracted
files. Its review screen offers exactly two links:

- **"Browse workspace in Gitea"** → the isolated job repo `job-29c28492`, a scratch
  execution surface.
- **"Open in Web IDE"** → the same workspace.

Neither is the deliverable. A reviewer who clicks the obvious button lands in the scratch
repo and has to reconstruct, from prose in the agent's notes, that the actual artefact is a
pull request on another forge entirely.

The agent's own notes on that job say it best:

> "PR #1 was verified open during the original delivery phase; remote PR status could not be
> re-queried in the corrective final review because no remote PR tool was available."

So even the agent could not re-check its own pull request.

## 2. Three distinct gaps

### 2a. The workspace link is hardcoded to Gitea

`getWorkspaceUrl()` (`cockpit/src/app/views/job-review/job-review.component.ts:685`):

```ts
const giteaUrl = environment.giteaUrl;
const repoName = currentJob.repo_name || `job-${currentJob.id}`;
return `${giteaUrl}/${repoName}/src/branch/${currentJob.branch_name}`;
```

It composes an internal Gitea URL from the job's own repo name, unconditionally. It has no
concept of a source repository attached to the job, so when the deliverable lives in an
external repo there is nothing to click. The label compounds it: **"Browse workspace in
Gitea"** is precise about the wrong thing.

### 2b. A pull request the agent opens is never persisted

`repo_open_pr` (`src/tools/repo/repo_tools.py:149`) calls `open_pull_request`
(`src/services/forge.py:341`) and returns a **string to the model**:

```python
return f"Opened #{result['number']} ({source} → {base}): {result['url']}"
```

The number and URL exist for exactly one turn, inside the agent's context. Nothing writes
them to `jobs`, to `job_change_records`, or anywhere else. The cockpit therefore has no
structured PR reference to render, and the only surviving trace is whatever prose the agent
chose to put in its summary — which is unparseable by construction and, as the quote above
shows, sometimes hedged.

`job_change_records` already carries `delivery_ref` and `delivery_sha` and is the natural
home. (Note it is written on terminal transition, so a `pending_review` job has no row yet —
see §5.)

### 2c. There is no read path for pull requests

`repo_open_pr` is create-only. There is no `repo_pr_status` or equivalent, so neither the
agent nor the cockpit can answer "is it still open, was it merged, was it closed". A link
alone will go stale the moment someone merges it.

## 3. What to build

### 3a. Delivery links section

Replace the single hardcoded button with a **Delivery** section that renders what the job
actually produced:

| link | source of truth |
|---|---|
| Source repository | `job_datasources` → `datasources.connection_url` where `type='repository'`; `config.forge` gives the forge |
| Branch | the branch the agent pushed, from the PR record (§3b) or the source repo's refs |
| Pull request | the persisted PR record (§3b), with live state if §3c exists |
| Job workspace | today's Gitea link, kept but **relabelled** and demoted — it is the execution surface, not the deliverable |

`src/services/forge.py` already parses owner/repo (`parse_owner_repo`, `:270`) and resolves
the API base per forge (`resolve_api_base`, `:283`), so deriving a browsable web URL from a
clone URL is solved for github/gitea/gitlab and does not need new forge knowledge.

### 3b. Persist the pull request

When `repo_open_pr` succeeds, record `{forge, repo, number, url, head, base}` against the
job. The tool already holds every field and discards them into a formatted string. This is
the smallest change in the document and unblocks everything else.

### 3c. A pull-request read tool

Add a status read (`repo_pr_status` or similar) so the PR's state can be shown rather than
assumed, and so an agent can verify its own delivery — the thing job `29c28492` explicitly
could not do. This also gives the loop's delivery guard a real signal: **branch pushed + PR
open is delivery**, even though `main` has not moved. A guard that only compares `main`
before/after would have recorded that job as having delivered nothing.

## 4. "Review in a session" — the interesting one

A link is a weak form of what a reviewer actually wants: to **read the change with the agent
that wrote it**, in an environment that matches the one it was written in.

Proposal: a **Review in session** action on the job review screen that provisions an
interactive session pre-loaded with the job's context — same model, same connectors, same
project scope, the source repo checked out at the delivered branch, and an opening brief
naming the job and its deliverables.

### 4a. What is inheritable today

`create_persistent_thread` already accepts `model`, `datasource_ids`, `project_ids`,
`config_name`, `temperature` and `permission_mode`. For job `29c28492` — whose
`config_override` is exactly `{"llm": {"model": "gpt-5.6-sol"}}` — model, connectors and
project scope are all expressible with the existing surface. Nothing new is needed for the
common case.

### 4b. The constraint that shapes the design

Session creation **deliberately exposes no `config_override`**, and this is a security
property, not an omission. From the tool's own contract, pinned by
`tests/test_tool_override_boundary.py::TestNoModelAuthoredPathReachesSessionCreate`:

> "The property that does [answer prompt injection] is that **no model-authored path reaches
> session create's `config_override`**: the MCP `create_persistent_thread` tool exposes no
> such parameter, and `spawn_subagent` uses a fixed environment. That is load-bearing and
> otherwise invisible — adding the parameter would silently dissolve the mitigation."

"Copy the job's settings into a session" is therefore **exactly** the shape that boundary
forbids — *if a model asks for it*. The distinction that makes this feature safe is that a
cockpit button is a **user**-authored path.

So the derivation must happen **server-side, from the job id**, and must never accept a
caller-supplied config:

```
POST /api/jobs/{job_id}/review-session
```

The orchestrator reads the job's own stored config and derives the session. The client names
a job, never a configuration; no new model-reachable parameter appears anywhere. Anything
not expressible through the existing session parameters (notably
`config_override.workspace.backend`) should be mapped to a `config_name`, or deliberately
dropped and stated in the UI — **not** smuggled in by widening the tool.

If an implementer finds themselves adding `config_override` to `create_persistent_thread`,
that test will fail. That failure is the design working, not an obstacle: the answer is to
keep the derivation server-side.

### 4c. Workspace

The session needs the delivered code, not the job's scratch workspace. Cleanest is a fresh
checkout of the source repo at the delivered branch, via the ordinary repository-connector
clone path, so no job-workspace lifetime is involved.

Note the known defect that session restore drops repo checkouts (see
`srw-session-restore-drops-repo-checkouts`); a review session that loses its checkout on
resume would be worse than a link. Whatever this uses must survive a resume, or the feature
should state plainly that the session is single-sitting.

## 5. Ordering, and what is cheap

1. **§3b persist the PR** — smallest change, unblocks §3a and §3c, and stops throwing away
   information the system already has.
2. **§3a delivery links** — pure cockpit work once §3b exists; the relabel alone removes an
   actively misleading affordance.
3. **§3c PR status read** — needed before any link can be trusted, and feeds the loop's
   delivery guard.
4. **§4 review session** — the largest, and the one with a security boundary to respect.

A note for §3a's implementer: `job_change_records` is written on terminal transition, so a
job sitting in `pending_review` — precisely when a human is reviewing it — has **no row**.
Verified: job `29c28492` in `pending_review` returns 0 rows. Delivery links must therefore
resolve from the job and its connectors, or the PR record must be written when the PR is
opened rather than at seal.

## 6. Out of scope

- Rendering diffs in the cockpit. `job-diff-review` already exists; this is about pointing at
  the right artefact, not re-implementing review.
- Any change to how deliverables are contracted or gated.
- Merging PRs from the cockpit.
