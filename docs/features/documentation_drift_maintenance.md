---
tags:
  - documentation
  - ci-cd
  - agent-capability
  - product-surface
  - dogfooding
---

# Documentation Drift Maintenance (Agent-Kept Docs)

> **Status (2026-08-03):** Concept / exploration. No committed work, no
> automation built. This captures a design conversation so the reasoning is not
> lost. Two forks remain open (call mechanism; investment level). The first
> concrete instance — the SRW `app-guide` skill — is specced in
> [[app_guide_skill]] (§Freshness and maintenance model); the cross-repo
> generalization depends on the write/PR path in [[scoped_git_push]].

## The idea

Documentation rots because updating it is uncompensated work that competes with
shipping. The observed failure mode is rarely "I didn't know the docs were
stale" — it is "I knew, and fixing them was 40 minutes I didn't have." A check
that only *reports* drift doesn't touch that cost.

The proposal: an agent watches a repository's diff, decides whether prose
documentation is now contradicted by the code, and — when it is — opens a draft
pull request that fixes the docs, with per-claim evidence (`file:line`) drawn
from the triggering diff. A human reviews and merges or closes; nothing is
auto-merged.

This began as "add a CI job that keeps the app-guide fresh" and generalized,
because the same capability keeps *any* connected repository's docs fresh — the
only thing that changes is who authorizes the write.

## Decomposition (why it generalizes)

The check is three separable layers:

| Layer | Own repo (SRW) | A user's repo (product) |
|---|---|---|
| **Trigger** — when it runs, over what diff range | GitHub Actions cron | an SRW loop / automation on a schedule |
| **Intelligence** — "is this doc now false, and what is the fix?" | model call over (docs + diff) | **identical** |
| **Write path** — branch + PR | built-in `GITHUB_TOKEN` as `github-actions[bot]` | GitHub App / scoped PAT ([[scoped_git_push]]) |

Only the write path differs. "Keep my app-guide fresh" and "SRW keeps a
customer's docs fresh" are therefore the **same feature with two credential
backends**, and [[scoped_git_push]] is the bridge between them. The intelligence
layer — the hard part — is written once and reused.

## Operational shape (as explored, for the app-guide instance)

- **Target docs:** `config/skills/app-guide/**` only — ~614 lines across nine
  files, small enough to hand a model *in full*, so no retrieval is needed and
  the "what does correct mean" contract stays tight. The *trigger* surface
  (which code diffs are worth reacting to) stays the broad product-surface list
  already in [[app_guide_skill]] §AI-assisted drift review (Cockpit routes,
  datasource enums, expert/skill configs, session/job/loop/etc. tools, feature
  flags, capability definitions).
- **Cadence:** nightly batch on `develop`, diffing
  `last_verified_revision..HEAD`. `develop` rather than `main` because that is
  where the change is fresh and the author still has context; `main` is the
  manual `v0.0.X` release cut, weeks later ([[deployment]]). Per-push
  and per-PR were rejected — `develop` takes ~29 direct commits/day, so batching
  is mandatory. At most one drift PR per day; silent exit when the range touches
  no product surface.
- **Output:** one draft PR per batch, editing the target docs, with per-claim
  `file:line` evidence. Never auto-merged.
- **Watermark = `last_verified_revision`:** the SHA the guide was last verified
  against, stored in-repo. **Only a human merge advances it** — the drift PR
  carries the bump in its own diff, so merging is atomic; closing the PR leaves
  the watermark untouched and the range is simply re-reported on the next run.
  The AI never advances it, an invariant already stated in [[app_guide_skill]].
  Consequence: ignoring a finding cannot silently bury it. (Note: this concept
  is referenced in [[app_guide_skill]] but not yet implemented in code — the KB
  `last_verified_cycle` column is unrelated.)
- **Reused seams:** the `changes:` job (`.github/workflows/develop.yml:418`)
  already computes path filters against the last-deployed SHA; the
  `github-actions[bot]` commit-back pattern is established three times over
  (ruff auto-format, license regen, deploy-tag bumps). The only genuinely new
  capability in CI is the model call.
- **Sequencing insight:** [[app_guide_skill]] currently gates the AI drift
  review behind M2/M3 (the capability registry). That gating is only necessary
  for the *deterministic* checks, which need capability IDs. The AI review needs
  only (docs + diff), both of which exist today, so it can land as a standalone
  slice alongside M1b rather than waiting for the registry.

## Open forks (not decided)

1. **Call mechanism.**
   - (a) **Direct Messages API + apply script** — one call with a
     structured-output schema returning `[{file, old, new, evidence}]`, then a
     deterministic script that may only write `app-guide/**`, bumps the
     watermark, and opens the PR via `gh`. Auditable control flow; fits the
     repo's deterministic-CI culture. *Leaning this way.*
   - (b) **`anthropics/claude-code-action`** — an agent loop that explores the
     repo, edits files, and opens the PR itself. Least plumbing; harder to
     hard-constrain to `app-guide/**`; behavior tracks the action's releases.
   - (c) **Advisory-only** — findings as a PR/job comment; re-introduces the
     writing cost the PR output was meant to remove.
   - All three need a new `ANTHROPIC_API_KEY` CI secret (none exists today).
2. **Investment level.**
   - (a) A cheap, disposable **CI janitor** that serves only this repo — a
     weekend's work.
   - (b) Build it as an **SRW loop** with the SRW repo as a repository
     datasource, so the PR is opened *through SRW's own product*. Dogfooding
     that exercises [[scoped_git_push]], the loop engine, and the app-guide skill
     against a real target — turning an internal chore into the first proof of a
     sellable feature. More work, and it pulls on the not-yet-built write tools /
     GitHub App. See [[loop_repo_compounding]] for the repos-that-improve-over-
     loop-runs framing.

## Relationship to existing design

- [[app_guide_skill]] — owns the first instance; its §Freshness and maintenance
  model and Phase 3 hold the deterministic + AI-assisted checks. This doc is the
  cross-cutting synthesis connecting that freshness work to the git-write
  feature.
- [[scoped_git_push]] — the write/PR path that turns this from an internal CI
  job into a cross-repo product capability. Its deferred "PR creation" follow-on
  is the specific seam this consumes.
- [[loop_repo_compounding]] — the dogfood-as-loop investment option.
- [[deployment]] — why the check targets `develop`, not `main`.
