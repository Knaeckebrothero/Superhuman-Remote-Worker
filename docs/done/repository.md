# Repository & Workspace URL Issues

Discovered 2026-03-09 while investigating why the cockpit "Workspace" button lands on a 404 page.

## Context

Job `c44fb360` ("What do we need for deployment?") ran successfully with scholar + critic subjobs in project `b3f8ce3e`. All three jobs completed correctly, but the workspace is inaccessible from the cockpit UI. Investigation revealed multiple layered issues spanning the cockpit frontend, orchestrator backend, and Gitea repository lifecycle.

---

## Issue 1: Wrong Gitea user in `env.js`

**Severity:** Breaks all workspace links
**File:** `cockpit/src/assets/env.js:4`

```js
window['env']['giteaUrl'] = 'http://localhost:3000/srw';
```

The `srw` user/org does not exist on Gitea (API returns `"user redirect does not exist"`). All repos are owned by the `graphrag` user. The orchestrator's Gitea client defaults to user `srw` (`orchestrator/services/gitea.py:32`), but the running instance uses `graphrag` (via `GITEA_ADMIN_USER` env var in the container). The cockpit URL base should be `http://localhost:3000/graphrag`.

The sidebar Gitea link (`cockpit/src/app/layout/sidebar/sidebar.component.ts:82`) also uses this base URL, so it points to a non-existent page too.

**Fix:** Change `srw` to match the actual Gitea user. Ideally, expose the Gitea user from the orchestrator API (e.g. `GET /api/config`) so `env.js` doesn't need to hardcode it.

---

## Issue 2: URL pattern doesn't support project repos

**Severity:** Breaks workspace links for all project-based jobs
**Files:**
- `cockpit/src/app/components/job-list/job-list.component.ts:928-932`
- `cockpit/src/app/components/job-review/job-review.component.ts:594-599`

Both construct workspace URLs using the same hardcoded pattern:

```typescript
getWorkspaceUrl(jobId: string): string | null {
  const giteaUrl = environment.giteaUrl;
  if (!giteaUrl) return null;
  return `${giteaUrl}/job-${jobId}`;
}
```

This generates URLs like `http://localhost:3000/graphrag/job-c44fb360-6fb8-41f1-be2b-24a89829032e`, which works for old per-job repos (e.g. `graphrag/job-34f93b9d`). But project-based jobs use a shared repo with branches:

- Repo: `graphrag/project-b3f8ce3e-jobs`
- Branch: `job/c44fb360`

The correct Gitea URL for a project job would be:
```
http://localhost:3000/graphrag/project-b3f8ce3e-jobs/src/branch/job/c44fb360
```

And for a subjob:
```
http://localhost:3000/graphrag/project-b3f8ce3e-jobs/src/branch/subjob/e592ae5c/scholar
```

### Sub-issue 2a: `repo_name` missing from jobs list API query

The `get_jobs()` query in `orchestrator/database/postgres.py:470-473` does not SELECT `repo_name`:

```sql
SELECT id, description, status, creator_status, validator_status,
       config_name, assigned_agent_id, user_id,
       project_id, parent_job_id, priority,
       branch_name, merge_status, created_at
FROM jobs
```

The single-job query `get_job()` at line 501-507 does include both `repo_name` and `branch_name`. The `job_summary` view in `schema.sql` also includes `repo_name` (line 580), but it's not used by the list endpoint.

### Sub-issue 2b: Frontend `Job` model missing `repo_name`

The `Job` interface in `cockpit/src/app/core/models/api.model.ts:413-434` has `branch_name` and `merge_status` but no `repo_name` field. The `JobSummary` interface in `audit.model.ts:99-112` has neither.

**Fix:**
1. Add `repo_name` to the `get_jobs()` SELECT query
2. Add `repo_name` to the `JobSummary` and `Job` frontend interfaces
3. Rewrite `getWorkspaceUrl()` to use `repo_name` and `branch_name`:
   - If `branch_name` exists: `{giteaUrl}/{repo_name}/src/branch/{branch_name}`
   - Else: `{giteaUrl}/{repo_name}` (per-job repo, browses default branch)

---

## Issue 3: Empty project repos — no `main` branch

**Severity:** Causes branch creation failures and wrong default branch
**Affected repo:** `graphrag/project-b3f8ce3e-jobs`

### Root cause chain

1. Project repo created with `auto_init: False` (`orchestrator/services/gitea.py:174`) — repo is completely empty, no commits, no branches.

2. `create_project()` (`orchestrator/main.py:4910-4921`) calls `gitea_client.create_repo()` but does not create a `main` branch or initial commit afterward.

3. When a job is created in the project (`create_job()`, line 1226-1227), it tries to create a branch from `main`:
   ```python
   await gitea_client.create_branch(jobs_repo["name"], branch_name, from_branch="main")
   ```
   This silently fails if `main` doesn't exist — `create_branch()` returns `False` but the error is only logged as a warning (gitea.py:734-737). The job record still gets `branch_name` and `repo_name` set.

4. The scholar subjob (`_spawn_scholar_subjob()`, line 2056-2060) does the same:
   ```python
   from_branch = job.get("branch_name") or "main"
   await gitea_client.create_branch(parent_repo_name, branch_name, from_branch=from_branch)
   ```
   If the parent's branch doesn't exist yet, this also falls back to `"main"` which also doesn't exist.

5. The first agent to push becomes the de facto first branch. In this case, the scholar agent pushed first, and Gitea assigned `subjob/e592ae5c/scholar` as the default branch.

### Observed state across project repos

| Repo | Default Branch | Has `main`? | Notes |
|------|---------------|-------------|-------|
| `project-84e9cd58-jobs` | `main` | Yes | Only branch — no jobs pushed? |
| `project-93ba8d10-jobs` | `main` | Yes | Normal |
| `project-af44bd86-jobs` | `master` | No (`master`) | Inconsistent default |
| `project-b3f8ce3e-jobs` | `subjob/e592ae5c/scholar` | No | Scholar pushed first |

### Downstream effects

- Gitea shows the scholar's workspace as the repo landing page instead of the main job's
- Branch creation from `main` silently fails for all subsequent jobs in the project
- The `create_branch` failure is logged but doesn't prevent job creation, so jobs run but without proper branch isolation

**Fix:** Use `auto_init: True` when creating project repos in `create_project()` (line 4913). This creates a `main` branch with an initial commit. Alternatively, use Gitea's API to push an initial commit (e.g. write a `.gitkeep` via `write_file()`) immediately after `create_repo()`.

---

## Issue 4: Scholar merge skipped — timing dependency

**Severity:** Fragile — works by accident in current setup
**Job:** Scholar `e592ae5c`, `merge_status: skipped`

### What happened

1. Scholar completed → `_squash_merge_subjob()` called (orchestrator/main.py:137)
2. Base branch resolved: `parent.get("branch_name")` → `"job/c44fb360"` (line 164-165)
3. `gitea_client.create_pr()` called with `head=subjob/e592ae5c/scholar`, `base=job/c44fb360`
4. PR creation returned `None` — logged as "no changes" (line 199-204)
5. Merge status set to `"skipped"`

### Why PR creation failed

The PR failed because `job/c44fb360` and `subjob/e592ae5c/scholar` have no common ancestor. Both branches were created independently in an empty repo (since `create_branch(from_branch="main")` failed silently). Each agent pushed to its own orphan branch. Gitea cannot create a PR between unrelated histories.

The log message "branch may have no changes" is misleading — the real issue is unrelated branch histories, not identical content.

### Why it still worked

The main job agent cloned the repo after the scholar had already pushed. Since the scholar's branch was the Gitea default, the agent's initial clone checked it out, inheriting the `research/` files. When the agent pushed to `job/c44fb360`, those files came along. Both branches show identical research files (`brief.md` = 4610 bytes, `sources.md` = 5145 bytes).

### Risk

This only works by accident when:
- The scholar pushes before the parent agent starts
- The scholar's branch is the default (or only) branch
- The parent agent clones and inherits the files

It would break if:
- The parent already had commits on its branch (conflicting histories)
- Multiple scholars ran (second scholar can't merge either)
- Branch creation succeeded from `main` (parent and scholar would diverge from different bases)

**Fix:** Same as Issue 3 — ensure `main` exists before creating branches. Then the branch lineage is `main` → `job/c44fb360` → `subjob/.../scholar`, and the squash merge has a proper common ancestor.

---

## Issue 5: `create_branch` failures are swallowed

**Severity:** Silent data integrity issue
**File:** `orchestrator/services/gitea.py:700-742`

When `create_branch()` fails (e.g. `from_branch` doesn't exist), it returns `False` and logs a warning. But the callers don't check the return value:

**`create_job()` at line 1226-1228:**
```python
await gitea_client.create_branch(
    jobs_repo["name"], branch_name, from_branch="main"
)
# No check — proceeds to set branch_name on the job record regardless
```

**`_spawn_scholar_subjob()` at line 2058-2061:**
```python
await gitea_client.create_branch(
    parent_repo_name, branch_name, from_branch=from_branch
)
# In a try/except but catches broadly — only logs warning
```

The job record gets `branch_name` set even if the branch doesn't actually exist on Gitea. The agent then pushes to a branch that was never properly created from the expected base, resulting in orphan branches.

**Fix:** Check the return value of `create_branch()`. If it fails, either retry with the correct base branch, or skip setting `branch_name` on the job (the agent would then push to a fresh branch, which is the current behavior anyway).

---

## Issue 6: `html_url` uses container hostname

**Severity:** Cosmetic / affects any URL derived from Gitea API responses
**Observed in:** Gitea API responses (`html_url: http://gitea:3000/...`)

Gitea is configured with its container hostname (`gitea:3000`) as the root URL. This means any `html_url` field from the Gitea API is not browser-accessible. The orchestrator avoids this by constructing URLs manually, but if any code or UI ever uses `html_url` from a Gitea API response, it will produce broken links.

Currently no code uses `html_url`, but it's worth noting for future integrations.

---

## Summary of fixes

| Issue | Fix | Files to Change |
|-------|-----|----------------|
| 1. Wrong Gitea user | Change `srw` → `graphrag` in `env.js` | `cockpit/src/assets/env.js` |
| 2. URL pattern | Use `repo_name` + `branch_name` for workspace URLs | `cockpit/.../job-list.component.ts`, `job-review.component.ts` |
| 2a. Missing field in API | Add `repo_name` to `get_jobs()` SELECT | `orchestrator/database/postgres.py:470` |
| 2b. Missing field in model | Add `repo_name` to `Job`/`JobSummary` interfaces | `cockpit/.../api.model.ts`, `audit.model.ts` |
| 3. Empty repos | Use `auto_init: True` in `create_repo()` | `orchestrator/services/gitea.py:174` |
| 4. Scholar merge | Resolved by fix 3 (proper branch lineage) | — |
| 5. Swallowed failures | Check `create_branch()` return value | `orchestrator/main.py:1226`, `2058` |
| 6. Container hostname | Configure Gitea `ROOT_URL` correctly | Docker/Podman compose config |

## Verification

After fixes, test with:

1. Create a new project → verify the Gitea repo has a `main` branch with an initial commit
2. Create a job in the project with scholar enabled → verify:
   - Parent branch `job/{id}` is created from `main`
   - Scholar branch `subjob/{id}/scholar` is created from parent branch
3. Let scholar complete → verify merge succeeds (not skipped)
4. Let main job complete → verify critic spawns and merges
5. Check cockpit:
   - "Workspace" button on the job list opens the correct Gitea page with the job's branch
   - "Workspace" link in job review opens the same page
   - Sidebar Gitea link opens the Gitea user page
6. Verify old per-job repos still work (backwards compatibility)
