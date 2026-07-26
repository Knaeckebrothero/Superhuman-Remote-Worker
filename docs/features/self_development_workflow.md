---
tags:
  - feature
  - design
  - git-integration
  - self-improvement-loop
  - security
  - skills
  - jobs
aliases:
  - self development workflow
  - srw builds srw
  - dogfooding workflow
  - agent contributes to own repo
  - deploy key workflow
  - branch and PR workflow
related:
  - "[[scoped_git_push]]"
  - "[[repo_datasource]]"
  - "[[credential_file_datasources]]"
  - "[[agent_skills]]"
  - "[[default_skill_roster]]"
  - "[[loop_repo_compounding_v2]]"
  - "[[project_self_improvement_loop]]"
  - "[[feature_development_pipeline]]"
  - "[[subjob_branch_merge_model]]"
---

# Self-Development Workflow — SRW contributing to its own GitHub repo

> **Status:** Design, v1 approved 2026-07-26. v1 is **configuration + one skill,
> zero code changes**. Follow-ups are tiered at the end. Companion to
> [[scoped_git_push]] (which designs the credential/identity hardening this doc
> deliberately defers) and [[repo_datasource]] (the shipped clone flow it builds on).

## TL;DR

- Attach `Knaeckebrothero/Superhuman-Remote-Worker` to SRW as an **ssh repository
  datasource** backed by a **GitHub deploy key** — a purpose-made keypair scoped
  to exactly one repo.
- Enforce the trust boundary **on GitHub** (rulesets on `main` + `develop`), not
  in SRW. SRW's `read_only` flag is advisory and the agent has a shell, so any
  SRW-side gate is bypassable by construction.
- The agent branches `job/<short_id>` off `develop`, implements, tests, pushes,
  and writes `output/pr.md`. The job freezes at `pending_review`. The human opens
  the PR from GitHub's own banner, reviews, and merges.
- **Nothing about branch creation needs automating** — the clone is already local
  on the workspace, so `git checkout -b` needs no API and no orchestrator involvement.
- The GitHub App (bot identity, expiring tokens, agent-opened PRs) is the first
  follow-up, and is itself a good candidate for the first real developer job.

## Motivation

Two drivers, one of them mundane and one structural.

**Mundane:** OpenAI subscription capacity goes unused while Claude capacity is
saturated. `gpt-5.6-sol` performs well *inside* SRW even though the Codex CLI is
unpleasant to drive directly. Spending that capacity on the SRW repo converts an
expiring resource into repository work.

**Structural:** SRW has never been pointed at its own repository. Doing so
exercises the repository-datasource path, the developer expert, and the
review loop against a codebase whose failure modes the maintainer can judge
instantly — which is the cheapest possible dogfooding signal. The mechanics
generalise directly: **any customer pointing an agent at their own GitHub repo
wants exactly this workflow**, so v1 is a product prototype, not just internal
tooling.

### The constraint that shapes everything

The maintainer's stated requirement: *the agent must not be able to reach other
repositories, and must not be able to write `main` or `develop`.* This is a
blast-radius requirement, not a code-quality one, and it is satisfied entirely
outside SRW.

## Trust model

**Enforcement belongs on GitHub. SRW-side gating is theatre.**

`datasources.read_only` produces an advisory string in `datasources.md` —
`" (declared read-only — treat as no-write)"`, `src/core/datasource_setup.py:1016`.
Because the agent has a shell on the workspace, any tool-layer refusal is one
`run_command("git push")` away from being bypassed. Treating that flag as a
security control would be a false sense of safety.

What actually holds:

| Control | Mechanism | Bypassable by the agent? |
|---|---|---|
| Cannot reach other repos | Deploy key registered on **one** repository | No — GitHub refuses the same public key as a deploy key on a second repo (*"Key is already in use"*), and the key is not attached to a user account, so it inherits no account-wide permissions |
| Cannot write `main` / `develop` | Repository **ruleset**: require a PR before merging, block force pushes, restrict deletions | No — server-side, applies to deploy-key pushes |
| Cannot open a PR | Deploy keys carry no API access | No |
| Human gate before merge | Job freezes at `pending_review`; PR merged manually | No |

Rulesets are **free on public repositories**, and this repo is public
(`Knaeckebrothero/Superhuman-Remote-Worker`), so the enforcement costs nothing.

**Accepted residual risk — Posture 1 (agent-readable credential).** The deploy
key is stored AES-256-GCM-encrypted in `datasources.credentials` and materialized
`0600` on the workspace, which is `emptyDir` and dies at teardown — but the agent
can `cat` it. This is identical to the posture of every existing `ssh_key`
datasource ([[credential_file_datasources]] §Trust model) and identical to what a
PAT would give. Blast radius remains one repository. Hardening is
[[scoped_git_push]]'s Posture 2/3 future work.

**A useful property of putting the gate on GitHub:** if the agent ignores the
workflow and tries to push to `develop`, the push is *rejected* and the agent
sees the error. The failure mode is loud and recoverable rather than silent, so
the skill below does not have to be mandatory in order to be sufficient.

### Why a deploy key, and why not the App yet

[[scoped_git_push]] argues for a fine-grained PAT over a deploy key, because a
PAT uses the identical HTTPS-token transport a GitHub App will later use, so the
App drops in as "a different token source" behind an unchanged agent-side seam.
That reasoning is sound and still governs the *end state*.

v1 chooses the deploy key anyway, for one reason: **it needs no code.** The ssh
branch of `clone_repository_datasources` (`src/core/datasource_setup.py:849`) is
shipped and writes the key, configures `~/.ssh/config`, and rewrites HTTPS URLs
to SSH. The token branch is shipped too, but embeds the secret in the remote URL
— the hygiene problem [[scoped_git_push]] Phase 1 exists to fix. Choosing ssh
avoids paying that debt for a bootstrap.

The cost is real and bounded: **deploy keys cannot open pull requests.** The human
clicks GitHub's "Compare & pull request" banner, which appears automatically for
any branch pushed in the last ~24 h. For a manual test of a handful of jobs that
is seconds of friction; at loop scale it becomes the argument for the App.

## What already exists

Surveyed 2026-07-26. The gap is much smaller than it appears.

| Capability | State |
|---|---|
| Clone external repo to `repos/<name>/` on the workspace, ssh or token auth | **Shipped** — `clone_repository_datasources`, `src/core/datasource_setup.py:849` |
| Encrypted credential storage + `0600` materialization + cleanup manifest | **Shipped** — [[credential_file_datasources]] |
| Branch / commit / push in the clone | **Works today** via the shell; the dedicated git tools (`git_log`, `git_show`, `git_diff`, `git_status`, `git_tags`) are read-only |
| Multiple repository datasources per job | **Unblocked** — `uq_datasource_type_job` dropped (`migrations/app/0001_initial.sql:972`) |
| Job freezes for human review at completion | **Shipped** — `finalize_job`, `src/core/phase.py:761`: `full` auto-completes, every other autonomy level freezes to `pending_review` |
| Feedback round-trip on a frozen job | **Shipped** — `resume_job_with_feedback` / `approve_job` |
| Per-job branch creation + `jobs.branch_name` / `repo_name` recording | **Shipped but Gitea-only** — `orchestrator/services/job_provisioning.py:200`, `branch_name = f"job/{short_id}"`, `short_id = job_id_str[:8]` (line 132). Targets the internal jobs repo via `gitea_client.create_branch`; does not run for attached external repos |
| PR create / merge API | **Shipped for Gitea only** — `orchestrator/services/gitea.py:1201`. No GitHub equivalent |
| Agent knows the branch/PR convention for an attached repo | **Missing** — `_inject_repo_context_to_workspace` (`src/agent.py:1494`) writes a Push & PR section into `datasources.md`, but it is Gitea-specific and describes the *workspace* repo, not attached datasources |
| Commit identity | **Hardcoded** — `Agent <agent@workspace.local>` at four sites in `src/managers/git_manager.py` (lines 165, 834, 915, 965). [[scoped_git_push]] §2 designs the fix |
| Agent-facing `repo_commit` / `repo_push` / `repo_pull` | **Deferred** — [[repo_datasource]] §2, designed in [[scoped_git_push]] Phase 2 |

### Branch creation does not need automating

Worth stating explicitly, because it was the leading candidate for v1 automation
and it is unnecessary.

The orchestrator pre-creates branches for the **internal Gitea jobs repo** because
the agent has no clone of it at provisioning time — the branch must exist remotely
first. For an **attached external repo the situation is inverted**: the clone is
already on the workspace pod, so `git checkout -b job/<short_id> origin/develop`
is a purely local operation requiring no API, no credential beyond the one already
present, and no orchestrator involvement. The branch materialises on GitHub at
first push.

Pre-creating it server-side would add a GitHub API client, a failure mode, and a
race — to replace one local git command. Rejected.

A second-order benefit: because the convention is deterministic
(`job/` + first 8 chars of the job UUID, matching the established codebase
convention), **the PR-creation URL is derivable from the job id alone**, with no
new columns and no agent tool call that could be forgotten:

```
https://github.com/Knaeckebrothero/Superhuman-Remote-Worker/compare/develop...job/<short_id>?expand=1
```

This makes the Cockpit follow-up (F5) nearly free, and is the reason linkage was
dropped from v1 rather than built.

## Design (v1)

Five parts. Only part 4 is an artifact; the rest is configuration.

### 1. GitHub side

```bash
ssh-keygen -t ed25519 -f srw-deploy -C "srw-agent" -N ""
```

A dedicated keypair; personal keys are never involved.

- Add `srw-deploy.pub` under **Settings → Deploy keys**, **"Allow write access" checked**.
- Add a **ruleset** targeting `main` and `develop`: *Require a pull request before
  merging*, *Block force pushes*, *Restrict deletions*.

### 2. Datasource

```jsonc
{
  "type": "repository",
  "connection_url": "git@github.com:Knaeckebrothero/Superhuman-Remote-Worker.git",
  "default_branch": "develop",
  "credentials": { "auth_method": "ssh", "ssh_key": "<private key>" },
  "read_only": false
}
```

Clones to `repos/Superhuman-Remote-Worker/` (`resolve_repo_clone_names`). Note the
**preserved case** — `repo_name_from_url` deliberately does not lowercase, because
GitHub repo names are case-sensitive in URLs (`src/utils/git_url.py:27`).

**Use the canonical URL.** This working copy's `origin` still points at
`Uni-Projekt-Graph-RAG.git` — a pre-rename URL GitHub redirects. Deploy keys are
registered against the repo, and the clone directory name is derived from the URL,
so the stale name would produce a confusing `repos/Uni-Projekt-Graph-RAG/`.

`read_only` stays false. It is advisory (see Trust model); the ruleset is the gate.

### 3. Project container

A project **"SRW Self-Development"** with the datasource linked, and
`default_config_override` pinning:

```jsonc
{ "autonomy": "review", "model": "gpt-5.6-sol", "expert": "developer" }
```

**`autonomy: "review"` is required, not the default.** Autonomy is an `AgentConfig`
setting (`src/core/loader.py:23`, levels `full | review | partial | guided |
dependent`), **not** a `jobs` column; the loader default is `partial`. Both
`partial` and `review` freeze at job-completion, but `should_freeze_at_boundary`
(`src/core/phase.py:459`) makes `partial` *also* freeze after the first strategic
phase — an extra approval this workflow does not want. `review` freezes only at
the end. The grants `autonomy_ceiling` defaults to `review`
(`src/core/capability_grants.py`), so this needs no grant change.

The project's `goal` carries a one-line restatement of the branch convention as a
backstop if the skill is not loaded. This project is also the container a future
loop (F8) attaches to.

### 4. The `repo-contribution` skill

**Written 2026-07-26 → `docs/skills/repo-contribution/SKILL.md`.** Verified against
the real parser (`src.core.skill_format.parse_skill_md`) and the house budgets:
description 763/1024 chars, 147/500 lines, ~1.9k/5k tokens.

**Renamed from `srw-repo-contribution`, and written generically.** The mechanics
(work in the clone, cut a job branch, push, write the PR body, stop) are not
SRW-specific — they are what *any* agent contributing to *any* attached repository
datasource must do. Hardcoding SRW would have made F9 a rewrite instead of a
promotion. SRW-specific details (which test command, the `docs/issues/` convention)
belong in the project `goal` or the job description, not the skill.

It stays a **user-owned DB skill** for now regardless of its genericity — promoting
it to `config/skills/` is a deliberate roster decision under [[default_skill_roster]],
not a side effect of how it happened to be written.

**A user-owned DB skill, deliberately not `config/skills/`.** Bundled skills ship
to every deployment; "how to contribute to the SRW repo" is not universal and
would consume a menu line in every customer's context for no benefit — the
capability-surface cost rule. The `skills` table scopes by `owner_id` +
`is_global` (no project scope), so a non-global user-owned row is the correct
grain. `SKILLS_DB_ENABLED` is already dev-on.

Authored as a version-controlled file in this repo, imported to the dev
deployment via `POST /api/skills/import` (`orchestrator/main.py:26374`), and
round-trippable via `GET /api/skills/{id}/export`.

Content — house format per [[default_skill_roster]] §pipeline (frontmatter →
framing paragraph → `## The <noun>` numbered steps → scaffold → `## Don't`;
description ≤1024 chars, body <500 lines / <5k tokens):

1. Work inside `repos/Superhuman-Remote-Worker/`, never the workspace root.
2. Set commit identity via `git config` (until F2 lands).
3. `git fetch origin && git checkout -b job/<short_id> origin/develop`.
4. Read the task; if it names a `docs/issues/` doc, read it first.
5. Implement. Follow surrounding code style.
6. Run the test gate (see Risks — the gate's real shape is TBD until R1 resolves).
7. Commit with a descriptive message; push the branch.
8. Write `output/pr.md` — title, body, rationale, testing evidence, risks.
9. Stop. Do not attempt to merge, and do not push to `main` or `develop`.

Model-invoked (unbound) is acceptable here because of the loud-failure property
noted in the Trust model.

### 5. Review loop

```
create job in project ──▶ agent branches, implements, pushes ──▶ pending_review
                                                                      │
        ┌─────────────────────────────────────────────────────────────┤
        │                                                             │
   changes wanted                                                  happy
        │                                                             │
resume_job_with_feedback                                    merge PR on GitHub
        │                                                             │
agent pushes more commits                                      approve_job
to the same branch; PR                                    (job → completed)
updates in place
```

The PR body comes from `output/pr.md`, retrievable via `get_job_file`.
Merge → auto-complete is **explicitly not automated** in v1 (F7).

## Risks and open questions

**R1 — Can the workspace image run SRW's test suite? (highest risk, unresolved)**
The workflow's step 6 assumes the agent can verify its own work. The workspace
pod image differs from a developer machine, and CI (Python 3.12) is the real
gate; local runs are known to be noisy. If pytest cannot run on the workspace,
the agent's "tested" claim is worthless and **every PR arrives unverified** — which
is what would make this exercise low-value rather than merely low-priority.
Everything else in v1 is reversible; this is not cheap to discover after a batch
of jobs. **The first job must establish this before any batch is queued.** If it
fails, the fallback is an explicit "push and let CI gate it" step, with the skill
stating plainly that the agent has not verified the change.

**R2 — Commit identity.** Until F2, commits read `Agent <agent@workspace.local>`.
The skill sets `git config` per-clone as a stopgap. Note that a git-config identity
is a *string anyone can set* — it is cosmetic provenance, not attestation. Only the
App (F1) makes the actor unforgeable.

**R3 — Credential readability.** Accepted, Posture 1. Bounded to one repo.

**R4 — Skill drift.** A DB skill is not version-controlled by default. Mitigated by
authoring the source in-repo and importing; re-import after edits. If this proves
annoying, promote to a bound expert instruction file.

**R5 — usage under-reporting on orchestrator restart.** Relevant because the
motivation is *spending* OpenAI capacity, so the ledger is how you know it worked.
The codex metering gap itself is **fixed and live on dev** (`812963ef`, 2026-07-19,
on `origin/develop`; **not on `origin/main`, so prod lacks it**). But
`llm_usage_poll_loop` (`orchestrator/main.py:1880`) keeps its cursor **in memory**
and re-anchors to `max(llm_requests.timestamp)` on every startup — deliberately
forward-only, "does not backfill historical rows." So rows that arrived but were
not yet materialized when the orchestrator restarts are skipped permanently: the
new anchor jumps past them. The lost window is bounded by the 120 s tick plus the
`min_age_s` hold-back, per restart. On a dev cluster that redeploys often, the
dashboard will under-report — and under-report *more* the more you deploy. Persist
the cursor to make it restart-safe if the accounting needs to be trusted.

**Resolved — where the skill source file lives.** `docs/skills/repo-contribution/SKILL.md`.
Outside `config/skills/`, so `_scan_skills` never globs it into every deployment's
menu, but still version-controlled and reviewable. No new bundle-scan exclusion needed.

**Open — does `_scan_skills` or any test assert on skills outside `config/skills/`?**
`tests/test_bundled_skills.py` covers bundled skills only, so a DB skill gets no
format test. A focused parse test for the source file would be cheap insurance.

## Follow-up items

Ordered by the sequence that makes sense, not strictly by value.

### F1 — GitHub App (bot identity, expiring tokens, agent-opened PRs)

The main event, and **a strong candidate for the first real developer job** — it is
well-scoped, the seam is already designed, and it is exactly the self-improvement
work worth dogfooding.

Scope: register the App (no webhooks and no public callback URL are needed —
we push, we do not receive events, which removes the usual hard part); a ~50-line
minting service (RS256 JWT, 10 min exp → `POST /app/installations/{id}/access_tokens`);
App private key into Vault/ESO; `auth_method: "github_app"` wired into the token
branch of `clone_repository_datasources`; Cockpit fields.

`PyJWT[crypto]>=2.8.0` is **already** in `orchestrator/requirements.txt`.

**The sharp edge: installation tokens expire in ~1 hour.** A three-hour job finds a
dead token at push time. Needs either a credential helper calling back to the
orchestrator to mint on demand, or a re-mint immediately before push. This is what
makes F1 a few days rather than an afternoon, and it is the part that would fail
silently in production.

Buys: unforgeable `srw-bot[bot]` actor in the audit log, expiring rather than
static credentials, PR creation. Does **not** buy "commits under its own name" —
that is F2, and works with any auth method.

Lands against [[scoped_git_push]] §"Forward-compatibility: the GitHub App seam".

### F2 — `commit_identity` on repository datasources

[[scoped_git_push]] §2. Collapse the four hardcoded identity sites in
`src/managers/git_manager.py` (165, 834, 915, 965) into one `_configure_identity()`
helper with `DEFAULT_AGENT_NAME` / `DEFAULT_AGENT_EMAIL`, and thread an optional
per-datasource `commit_identity`. Small, self-contained, removes R2's stopgap.

### F3 — Credential seam (token out of `.git/config`)

[[scoped_git_push]] Phase 1. Only needed once the token/App path is in use; the
ssh bootstrap does not depend on it. Prerequisite for F1 in practice.

### F4 — `repo_commit` / `repo_push` / `repo_pull` write tools

[[scoped_git_push]] Phase 2, originally [[repo_datasource]] §2. Auditable surface
that enforces identity and respects `read_only`, replacing raw shell git. Worth it
once the workflow is routine; the shell works meanwhile.

### F5 — Cockpit "Open PR" button

Derived client-side from job id + repo datasource URL using the deterministic
compare URL above. Frontend-only, no schema, no backend. Value appears past a
handful of jobs or once GitHub's 24 h branch banner expires.

### F6 — External branch/PR refs on jobs

An `external_refs` JSONB on `jobs`, or extending `job_provisioning.py` to record
external branch state. Deliberately **not** reusing `jobs.branch_name` /
`repo_merge_statuses`, which are semantically the internal Gitea jobs repo and
would become ambiguous. Only justified once F5's derived link proves insufficient
— e.g. for non-deterministic branch names or multi-PR jobs.

### F7 — Auto-complete job on PR merge

Requires a GitHub webhook receiver (the first piece of this design that does), plus
F6's linkage to map PR → job. Explicitly deferred by the maintainer.

### F8 — Project loop over `docs/issues/`

The original motivation: a loop that works the issue backlog autonomously. Attaches
to the project container from Design §3 and builds on [[loop_unified_engine]] /
[[project_self_improvement_loop]]. **Should not start until R1 is resolved and the
manual workflow has run enough jobs to be trusted** — an unattended loop producing
unverified PRs is worse than no loop.

### F9 — Generalise to a product feature

The mechanics are not SRW-specific. Once F1 + F2 + F4 land, "point an agent at your
GitHub repo, get PRs" is a shippable capability. That is the argument for building
F1 properly rather than hacking a PAT in. Interacts with [[feature_development_pipeline]]
(which stages requirements → research → implementation) — the two compose: that
pipeline's output lands via this workflow.

## Related

- [[scoped_git_push]] — credential seam, commit identity, write tools; F1–F4 land against it
- [[repo_datasource]] — the shipped clone flow v1 builds on
- [[credential_file_datasources]] — encryption at rest, materialization, Posture 1
- [[agent_skills]] / [[default_skill_roster]] — skill substrate and house authoring format
- [[loop_repo_compounding_v2]] — per-job branches + squash merge for the *internal* jobs repo; the convention this doc borrows
- [[subjob_branch_merge_model]] — the recursive branch/merge model for subjobs
- [[project_self_improvement_loop]] / [[loop_unified_engine]] — where F8 attaches
- [[feature_development_pipeline]] — upstream staging that composes with this workflow
- [[workspace_storage_state_topology]] — why the on-workspace key is ephemeral
