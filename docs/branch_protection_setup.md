# Branch Protection Setup

Runbook for enabling branch protection on `develop` and `main` once GitHub Pro
(or an org plan) is active on this repo.

## Background

Until protection is on, every CI workflow runs independently. Two failure modes
this enables:

1. **Failed lint, successful deploy.** `db-migrations.yml` can exit ❌ while
   `develop.yml` exits ✓ in parallel — the deploy still ships. This is how the
   `BIGSERIAL` lint warnings in `0004_thread_events.sql` and
   `0006_headless_notifications.sql` survived for 13 hours before the
   subsequent in-place rewrite tripped the migration runner's checksum guard
   and crash-looped the new orchestrator pod on 2026-05-13.
2. **Direct pushes bypass review.** A `git push origin develop` lands code
   without ever seeing CI's verdict first.

Branch protection enforces "nothing reaches `develop` unless CI has passed on
it" at the GitHub-infrastructure level. The migration runner's checksum guard
remains in place as a *consequence* tool — branch protection is the
*prevention* tool.

## Prerequisites

- **GitHub Pro** (this repo is private; classic branch protection is gated
  behind Pro for private repos on personal accounts).
- **Admin role** on the repo (the account that owns it qualifies).
- **`gh` CLI installed and authenticated** with `repo` and `admin:repo_hook`
  scopes: `gh auth status` should show `Logged in to github.com as
  <user>`. If scopes are missing: `gh auth refresh -s admin:repo_hook,repo`.
- Decide which model to use:
  - **Classic branch protection** — simpler, sufficient for one developer.
    What this runbook uses.
  - **Rulesets** — newer, supports path-conditional required checks and
    per-pattern rules. Worth a look if/when the workflow setup grows beyond
    what's described here.

## The status-check footgun

GitHub treats a required status check that did **not run** the same as one
that *failed* — the PR can't merge. This matters because two of our workflows
use path filters:

| Workflow | Trigger | Implication if required |
|---|---|---|
| `develop.yml` | Every push/PR to `develop` | Safe to require its jobs |
| `db-migrations.yml` | Only when files under `orchestrator/database/migrations/**` change | **Unsafe to require directly** — a docs-only PR would block forever |

Two ways around this:

- **Phase 1 (recommended start):** Require only the always-running checks from
  `develop.yml`. Direct pushes get blocked. Migration lint is still
  non-blocking. This is a strict improvement over today.
- **Phase 2 (full protection):** Restructure `db-migrations.yml` so the
  workflow always runs but the individual jobs short-circuit when no migration
  files changed, then expose a single "all checks ok" aggregator job that's
  safe to require. Closes the original BIGSERIAL loophole.

Pick Phase 1 to start; layer Phase 2 in once Phase 1 is working.

## Phase 1 — Block direct pushes, require base CI

Goals:
- No more `git push origin develop` — everything goes through a PR.
- Ruff, dependency-audit, CodeQL must pass before merge.
- Migration lint is **still advisory** (red badge, doesn't block) — fixed in
  Phase 2.

### Required check names

Use these exact strings (job display names from the workflow files):

| Workflow file | Job display name |
|---|---|
| `develop.yml` | `lint` |
| `develop.yml` | `dependency-audit` |
| `develop.yml` | `codeql (python)` |
| `develop.yml` | `codeql (javascript-typescript)` |

Note: `test-python` / `test-cockpit` are **change-conditional** in the current
workflow — don't include them in required checks until they're restructured
the same way as Phase 2 describes for migration jobs. Same goes for the
`build-*` jobs.

### UI walkthrough (`develop`)

1. Repo → **Settings** → **Branches** → **Add classic branch protection rule**.
2. Branch name pattern: `develop`
3. Tick **Require a pull request before merging**.
   - Required approving reviews: **0** (solo dev). Bump to 1+ if you ever
     onboard a collaborator.
   - Tick **Dismiss stale pull request approvals when new commits are pushed**.
4. Tick **Require status checks to pass before merging**.
   - Tick **Require branches to be up to date before merging** (forces a
     rebase when develop has moved on, so the checks reflect the merged state).
   - In the search box, add the four check names above. They will only appear
     after each check has reported at least once on any commit in the repo —
     if the search comes up empty, push a trivial commit first to seed the
     check registry.
5. Leave **Require conversation resolution before merging** off (no use for
   solo dev; turn it back on when collaborators arrive).
6. Tick **Require linear history** (forbids merge commits — keeps `git log`
   readable).
7. **Do not** tick "Do not allow bypassing the above settings" yet. That
   flag forbids even admins from pushing past failed checks; useful long-term
   but reserve until you've used the protection for a week without surprises.
8. **Save changes**.

### gh CLI scripted equivalent

For reproducibility, the entire setup is one API call. Save this as
`scripts/setup-branch-protection.sh` so the rules live in git:

```bash
#!/usr/bin/env bash
# Apply branch protection rules. Re-runnable; PUT is idempotent.
# Requires: gh auth status with admin:repo scope.
set -euo pipefail

REPO="${REPO:-Knaeckebrothero/Superhuman-Remote-Worker}"

# develop — Phase 1 protection
gh api -X PUT "repos/${REPO}/branches/develop/protection" \
  -f required_status_checks.strict=true \
  -f required_status_checks.contexts[]='lint' \
  -f required_status_checks.contexts[]='dependency-audit' \
  -f required_status_checks.contexts[]='codeql (python)' \
  -f required_status_checks.contexts[]='codeql (javascript-typescript)' \
  -F enforce_admins=false \
  -F required_pull_request_reviews.required_approving_review_count=0 \
  -F required_pull_request_reviews.dismiss_stale_reviews=true \
  -F required_linear_history=true \
  -F allow_force_pushes=false \
  -F allow_deletions=false \
  -F restrictions=null

echo "develop protection applied."
```

Run with: `bash scripts/setup-branch-protection.sh`. Verify with
`gh api repos/${REPO}/branches/develop/protection | jq`.

### `main` is the same plus stricter defaults

The Helm chart on `main` ships to anything more permanent than the test
cluster. Same Phase 1 rules but with:

- `enforce_admins=true` (you don't get to bypass)
- `required_approving_review_count=1` (if/when a reviewer exists; keep 0
  while solo)
- Tag with `restrict-deletions=true` (already set above)

If `main` isn't yet a real deploy target, applying Phase 1 to it is still
cheap insurance — it stops you from accidentally direct-pushing to it.

## Phase 2 — Make migration lint blocking

This requires modifying `db-migrations.yml` (and possibly `develop.yml`) so
the migration checks always run, then exposing one aggregator job that's safe
to require.

### Workflow change sketch

In `db-migrations.yml`, remove the workflow-level `paths:` filter and gate at
the job level instead:

```yaml
on:
  push:
    branches: [develop, main]
  pull_request:
  workflow_dispatch:

jobs:
  detect:
    runs-on: ubuntu-latest
    outputs:
      changed: ${{ steps.diff.outputs.changed }}
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - id: diff
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then
            BASE="${{ github.event.pull_request.base.sha }}"
          else
            BASE="HEAD~1"
          fi
          if git diff --name-only "$BASE" HEAD -- \
              'orchestrator/database/migrations/' \
              'orchestrator/database/migrate.py' \
              '.squawk.toml' \
              '.github/workflows/db-migrations.yml' | grep -q .; then
            echo "changed=true" >> "$GITHUB_OUTPUT"
          else
            echo "changed=false" >> "$GITHUB_OUTPUT"
          fi

  squawk:
    needs: detect
    if: needs.detect.outputs.changed == 'true'
    # ... existing squawk job body ...

  dry-run:
    needs: detect
    if: needs.detect.outputs.changed == 'true'
    # ... existing dry-run job body ...

  uniqueness:
    needs: detect
    if: needs.detect.outputs.changed == 'true'
    # ... existing uniqueness job body ...

  # Always-running aggregator: succeeds iff every (run) job succeeded.
  # Skipped jobs count as success. This is what you require in branch
  # protection.
  status:
    needs: [detect, squawk, dry-run, uniqueness]
    if: always() && !cancelled()
    runs-on: ubuntu-latest
    steps:
      - name: Roll up results
        run: |
          for j in squawk dry-run uniqueness; do
            r="${{ needs[j].result }}"
            if [ "$r" = "failure" ]; then
              echo "::error::$j failed"
              exit 1
            fi
          done
          echo "All migration checks passed (or skipped because no migrations changed)."
```

Then add `db-migrations / status` to the required-checks list:

```bash
gh api -X PUT "repos/${REPO}/branches/develop/protection" \
  -f required_status_checks.contexts[]='status' \
  # ... plus the Phase 1 contexts
```

Once Phase 2 is in, the original BIGSERIAL incident becomes impossible: a PR
introducing the violation gets a ❌ on `db-migrations / status`, branch
protection blocks the merge, no image gets built carrying it.

### Same treatment for tests, if you want full coverage

`test-python` and `test-cockpit` in `develop.yml` are also change-conditional.
A `tests / status` aggregator following the same pattern lets you require
them too. Optional — the current `dependency-audit` requirement catches
most regressions worth blocking on.

## Daily workflow after enabling

Direct pushes to `develop` start failing with
`remote: error: GH013: Repository rule violations found`. New flow:

```bash
git switch -c fix/the-thing       # 1. branch
# edit, edit
git push -u origin fix/the-thing  # 2. push branch
gh pr create --fill               # 3. open PR (--fill uses commits as body)
# wait for CI; if green:
gh pr merge --squash --auto       # 4. auto-merge once required checks pass
# (or --merge / --rebase per your repo policy)
```

The `gh pr merge --auto` half is the magic — it queues the merge so you
don't have to babysit CI. Combined with `gh pr create --fill`, the friction
is two commands per change instead of one.

For long-running branches the PR stays open through multiple pushes; only
the final merge requires CI green.

## Emergency bypass

If `enforce_admins=false`: as the repo admin, you can force-push by
explicitly removing the protection rule via API, doing the push, then
re-applying. **This should feel uncomfortable** — that discomfort is the
point.

```bash
gh api -X DELETE "repos/${REPO}/branches/develop/protection"
# ... emergency push ...
bash scripts/setup-branch-protection.sh   # restore the rules
```

If `enforce_admins=true`: there is no bypass without admin demotion +
remediation. By design.

## Verification

After enabling:

```bash
# 1. Confirm protection is on
gh api "repos/${REPO}/branches/develop/protection" | jq '.required_status_checks.contexts, .required_pull_request_reviews'

# 2. Try the failure path on a throwaway branch — should refuse
git switch -c test/protection-check
echo "" >> README.md
git commit -am "trivial"
git push origin develop  # expect: "GH013: Repository rule violations found"

# 3. Try the success path
git push -u origin test/protection-check
gh pr create --fill
# CI runs; once green, merge. The trivial commit lands.

# 4. Clean up
git switch develop && git pull
git branch -D test/protection-check
gh api -X DELETE "repos/${REPO}/git/refs/heads/test/protection-check"
```

## Open follow-ups

- **Phase 2 workflow refactor** is not done; until then `db-migrations / status`
  doesn't exist and the BIGSERIAL incident remains theoretically repeatable
  (though less likely now that the precedent is documented).
- **Required reviews ≥ 1** becomes appropriate when this stops being a solo
  project.
- **Rulesets migration** is worth revisiting if you ever need path-conditional
  rules (e.g. require `db-migrations / status` only on PRs that touch
  migrations) — Phase 2's aggregator approach is the classic-protection
  workaround for the same need.
