---
tags:
  - skills
  - onboarding
  - product
  - documentation
---

# App Guide Skill (`app-guide`) — the product explains itself

> **Status**: Proposed + in build (2026-07-07). Companion to [[default_skill_roster]]
> (which defines the bundled-skill tiers and the authoring pipeline this follows)
> and [[agent_skills]] (the skills substrate this rides on).

## Problem

SRW has no user-facing guide. `docs/` holds 100+ files, but they are developer
and design docs — grounding any help surface on them would teach users about
migration guards and Fleet syncs, not about what a job is. A new user lands in
an empty cockpit with no path to "what can I do here?".

The classic fixes are known-bad: upfront product tours and written tutorials
have dismal completion and retention ("nobody reads them"). The modern pattern
is contextual help — empty states, starter prompts, and an in-app assistant
grounded on docs.

SRW is unusually well-positioned for the assistant pattern because the primary
interface *already is* a conversation with an agent. The gap is knowledge, not
surface: the session agent knows its own tools (they're in context) but knows
nothing about the product around it — jobs vs. sessions, experts, projects,
loops, datasources, autonomy levels, or the cockpit itself.

## Decision

Ship a bundled, **model-invoked (unbound)** skill at `config/skills/app-guide/`
whose `references/` folder **is** the user-facing usage documentation. When a
user asks "what can I do here" / "how do jobs work" / "what's a project", the
session agent triggers on the skill description (L1), loads the body via
`use_skill` (L2), reads the matching reference file(s) with `read_file` (L3),
and answers grounded in those docs only.

Why a skill rather than prompt injection or a UI tour:

- **Zero cost when unused.** Only the one-line L1 menu entry rides the system
  prompt; the body and references load on demand. Product knowledge doesn't
  tax every unrelated turn.
- **Existing substrate, nothing new to build.** `_scan_skills` already globs
  `config/skills/*/SKILL.md` into the catalog; skill directories are
  materialized into the agent workspace at start; `use_skill` +
  `read_file` are already in both `defaults.yaml` and
  `persistent_defaults.yaml` tool lists. The `todo-guide` skill already ships
  the `references/` progressive-disclosure shape.
- **Docs live as markdown in the repo.** They ship with the image, are
  reviewed like code, and stay a single source of truth (also reusable for
  customer/support material).
- **Unbound sidesteps the `deep_merge` replace-trap** on `instruction_files`
  (see [[default_skill_roster]] mechanics). The cost is catalog noise for
  worker experts; accepted — the description self-scopes to *a user asking
  about the app*, and workers have no user asking.

## Mechanics

- **Catalog**: `_scan_skills` (orchestrator/main.py) picks up the directory;
  bundled tier is the floor, so an owner/project skill with the same name can
  shadow it per-deployment.
- **Delivery**: L1 name+description in the system prompt `available_skills`
  menu → L2 `use_skill("app-guide")` returns the SKILL.md body → L3 the body
  directs `read_file("skills/app-guide/references/<topic>.md")`.
- **Flag**: model-invoked skills appear only when `SKILLS_DB_ENABLED` is on
  (currently dev-on / prod-off). Prod visibility requires the flag flip — a
  rollout item, not part of this build.
- **Primary consumer**: the session/assistant expert. Worker experts see the
  same catalog entry; harmless (see above).

## The reference set (v1)

Each file is a short, user-voiced usage doc (~60–120 lines). Final list may
shift slightly with research findings; the SKILL.md body carries the
authoritative index.

| File | Covers |
|---|---|
| `references/overview.md` | What SRW is; the mental model (sessions = interactive chat, jobs = autonomous work, projects tie them together); a first-run tour of the cockpit |
| `references/sessions.md` | Working in a session: chat, the agent's workspace and files, permission modes, model switching, skills, voice, stopping/interrupting |
| `references/jobs.md` | Creating a job (expert, model, autonomy, project, datasources); statuses; phases/todos/progress; approve / resume-with-feedback / pause / cancel / message; where results land |
| `references/experts.md` | The shipped expert roster and what each is good at; custom experts |
| `references/projects-and-loops.md` | Projects (members, shared knowledge, linked datasources, repo); the self-improvement loop (role sequences, iterations, budgets) |
| `references/datasources.md` | Supported datasource types; what attaching one gives the agent; project vs. job attachment |
| `references/memory-and-knowledge.md` | What agents remember across jobs (memory), the project knowledge base, where to browse/search it |
| `references/files-and-integrations.md` | Deliverables and files; cloud storage (OpenCloud) and git (Gitea) integrations; citations |

## Grounding rules (the anti-hallucination contract)

The SKILL.md body instructs the agent to:

1. Identify the topic and **read the matching reference before answering** —
   never answer app-usage questions from priors.
2. **Answer only from the references** (plus its own directly visible tools).
   If the question isn't covered, say so plainly and suggest where to look —
   don't guess.
3. **Never invent UI labels, buttons, or features.** Deployment-dependent
   features (flag-gated, admin-configured) are described as such, not asserted.

This is what makes "the AI explains itself" safe: the skill turns an
open-ended chatbot answer into a retrieval-then-answer procedure over
reviewed docs.

## Authoring pipeline

Follows the [[default_skill_roster]] pipeline (steps 4–7): house-style
SKILL.md (frontmatter → framing → procedure → index scaffold → Don't),
budgets (description ≤ 1024 chars — it is the trigger; body < 500 lines),
focused test in `tests/test_bundled_skills.py`, `pytest` + `ruff`, k3d
parse-in-pod check. The research step differs: facts are internal, so instead
of web deep-research, three codebase survey passes (cockpit UI surfaces; job
lifecycle + autonomy; sessions/projects/loops/datasources/memory) feed the
reference docs, with file-level citations retained in the research notes.

## Acceptance criteria

1. `pytest tests/test_bundled_skills.py tests/test_skill_bindings.py -q` green,
   including a focused `app-guide` test (parses, budgets, body indexes every
   reference file that exists on disk, grounding rule present).
2. On k3d with `SKILLS_DB_ENABLED=true`: a fresh session asked *"What can I do
   in this app?"* triggers the skill and answers with a correct
   sessions/jobs/projects mental model and no invented features; asked about
   something off-doc, it says the guide doesn't cover it rather than guessing.
3. Every factual claim in `references/` is traceable to code/UI (research
   reports carry `file:line` citations).

## Out of scope / follow-ups

- **Discovery affordances in the cockpit** — starter-prompt chips on a fresh
  session ("What can I do here?") and empty states that point at the
  assistant. The skill answers questions; these make users ask them. Without
  at least the starter prompts, the guide has the same problem as a tutorial
  nobody opens.
- **Demo content** (a sample project with a completed job) — show-don't-tell
  onboarding.
- **Prod rollout**: `SKILLS_DB_ENABLED` flip (or a binding decision) for
  non-dev deployments.
- **Freshness automation**: the loop's product-qa role could periodically diff
  `references/` against shipped features and file drift as issues.

## Maintenance

The references are product docs: updating them is part of shipping a
user-visible feature (same discipline as updating CLAUDE.md for dev-visible
changes). The focused test keeps the body's index honest against the files on
disk; content freshness stays a review-time concern until the automation
follow-up lands.
