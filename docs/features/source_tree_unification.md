---
tags:
  - feature
  - architecture
  - refactor
  - orchestrator
  - agent
  - open-source
aliases:
  - flat source tree
  - unified tree
  - monorepo restructure
  - orchestrator/src merge
related:
  - "[[agent_open_source_split]]"
  - "[[orchestrator_main_py_monolith]]"
  - "[[go_rewrite]]"
  - "[[cockpit_folder_restructure]]"
  - "[[2026-06-09-release-package-and-licensing]]"
  - "[[2026-06-09-roadmap-priorities]]"
---

# Source Tree Unification — Risk/Benefit Assessment

**Date:** 2026-06-15
**Status:** Assessment only. No decision, no plan, no commitment. Captures the
risk/benefit picture so this question doesn't get re-derived from scratch later.

## The Proposal

Collapse the current two-tree split — agent in `src/` + `agent.py`, orchestrator
in `orchestrator/` — into a single flat source tree:

- Thin entry files at the top: `orchestrator.py`, the worker-agent entry, and
  the persistent-session-agent entry (today: `agent.py` driving `src/graph.py`
  and `src/persistent_graph.py`).
- Shared packages and utilities below, importable by every entry.

Stated motivation: the split has always made code reuse awkward, which removed
the natural home for shared utilities and likely contributed to
`orchestrator/main.py` growing into a 20k-line monolith. Framed as a precursor
to a possible Go rewrite (see [[go_rewrite]]) and to open-sourcing the core (see
[[agent_open_source_split]] and [[2026-06-09-release-package-and-licensing]]).

Stated disadvantages (from the author): images would carry more Python than
needed, and Dockerfiles would get more complex because you'd have to pick and
choose packages per image instead of copying one directory.

## Verdict (BLUF)

The merge is a good idea whose payoff is **almost entirely conditional on the
open-source release happening**. The release is the event that makes the repo
layout permanent (public import paths become API for forks) and is therefore the
moment when paying the churn cost is cheapest. Done standalone, mid-pilot, it
scores near zero on every roadmap decision rule and is mostly cost. Done as the
first commit of release prep, it is mostly benefit.

Two corollaries:

- The small **`shared/` extraction** (~a day) is positive-EV regardless of the
  release and removes most of the day-to-day pain. It can be done independently
  and early.
- The Go rewrite neither justifies the restructure nor benefits from it at
  *start* time — only at *port* time, and only if its own trigger conditions
  ever fire. Keep the two decisions decoupled.

## Verified Current State

Grounding facts from a pass over the tree (2026-06-15):

- **The shared surface is tiny.** Only two agent modules are imported by the
  orchestrator: `src.core.model_registry` (~316 lines; used by `main.py`,
  `services/builder_config.py`, `services/llm_endpoint_probe.py`,
  `services/capability_credentials.py`) and `src.utils.ssh_key` (~199 lines;
  used by `main.py`). Five import sites, ~515 lines total.
- **The dependency is one-directional.** `grep` for `from orchestrator` /
  `import orchestrator` inside `src/` and `agent.py` returns nothing. The agent
  never imports orchestrator code. This is the property the OSS split depends on.
- **There is already duplication papering over the gap.** `orchestrator/utils/
  db_url.py` is a hand-mirror of `src/utils/db_url.py`. Its comment claims it is
  "kept duplicated so the orchestrator container image doesn't need to bundle the
  agent `src/` tree" — but `docker/Dockerfile.orchestrator` already does
  `COPY src/ ./src/`. The justification is already void; the duplication is pure
  tax.
- **The orchestrator image already bundles all of `src/`.** So "images carry
  more Python than needed" is today's reality, not a new cost the merge
  introduces.
- **Requirements are already split** into two files (agent ~84 lines,
  orchestrator ~44 lines), each image installing its own. This is the real lever
  keeping the agent image from inheriting the k8s client and friends.
- **The monolith is still a monolith, and growing.** `orchestrator/main.py` is
  ~20.5k lines (the [[orchestrator_main_py_monolith]] issue measured 19,032 on
  2026-05-18). The `APIRouter` split has barely started: `routers/sessions.py`
  and `routers/automations.py` exist, but `main.py` has only 5 `include_router`
  calls and still carries the bulk of the endpoints inline.

## Benefits

1. **Ends the duplication-with-a-false-alibi.** Shared code gets one correct
   home; the mirrored `db_url.py` and its now-false comment go away.
2. **Gives shared code a legitimate place to live.** Today, anything needed by
   both sides has no correct location, so it gets duplicated or absorbed into
   `main.py`. This removes that excuse — but see the limits below; it removes the
   excuse, not the monolith.
3. **Layout finality before open-sourcing.** Once the AGPL core is public, import
   paths and repo shape are effectively API for forks and contributors. A
   pre-release move costs ~a week of internal churn; a post-release move imposes
   the same churn on every downstream user and makes the project read as
   unstable. This is the single strongest argument, and it pins the *when*.
4. **Go-rewrite optionality, for free.** A unified tree with thin entries and an
   enforced `entries → components → shared` import direction is structurally
   `cmd/` + `internal/`. If the rewrite ever activates, the port maps 1:1. You
   get this without committing to Go.
5. **Builds get simpler, not harder — if you let them.** Both Dockerfiles
   converge to the same shape: `COPY` the tree + install their own requirements +
   their own `CMD`. The author's "more complex, pick and choose" worry only
   materializes if you *also* try to minimize image contents (see Risk 4); the
   default flat copy is actually simpler than today.

## Risks

1. **Churn radius.** Essentially every import across ~230 Python files changes.
   In-flight branches (workspace-reaper, the uncommitted headscale fix) become
   conflict bombs. The fallout list is long but mechanical: Tiltfile live-sync
   paths, CI workflows, helm, test imports, CLAUDE.md, and the `get_project_root`
   pyproject-marker hack. Fully mitigable by timing (after branches land) and by
   doing it as a single mechanical move-commit with zero logic changes — but only
   if a genuinely quiet window exists.
2. **Boundary erosion (the sneaky one).** The crude two-tree split makes sideways
   imports physically awkward. One tree turns `orchestrator → agent-internals`
   into a one-line temptation, and a year of that quietly destroys the property
   the OSS thesis rests on: that a third party can drive the agent with a
   ~300-line orchestrator over HTTP. **If the merge happens without an
   import-linter rule in CI from day one, this risk is near-certain over time.**
3. **It won't fix the monolith.** The 20k `main.py` needs its own incremental
   router/dispatch extraction (the plan in [[orchestrator_main_py_monolith]] is
   sound). The service layer already exists and was *available* before the merge;
   the split being absent is habit, not a missing home. If this restructure is
   mentally booked as "the monolith fix," the budget gets spent and the monolith
   survives.
4. **Dependency creep.** One tree gravitates toward one requirements file. The
   moment the dep sets merge, the agent image inherits the orchestrator's deps.
   The two-requirements split must survive the merge *deliberately* — it is the
   thing actually keeping images lean (torch already dominates size; everything
   else is noise next to it).
5. **Opportunity cost.** Against the [[2026-06-09-roadmap-priorities]] decision
   rules (pilot proximity, trust, deployment friction, product clarity), a
   standalone restructure scores ~zero. It is parking-lot work unless attached to
   the release milestone. Solo-dev weeks are the scarcest input in the whole
   equation.
6. **Strategy-reversal exposure (low).** If the posture ever reverts from "AGPL
   everything" to open-agent / closed-orchestrator, a unified tree makes
   re-drawing that separation harder. Current direction makes this unlikely, but
   it is a near-one-way door.

## Engaging the Author's Stated Disadvantages

- *"Images carry more Python than needed."* Already true today — the orchestrator
  bundles all of `src/`. It is size-noise next to the torch wheel, and under an
  all-AGPL release it is not a licensing leak either. Net-neutral.
- *"Dockerfiles get more complex — pick and choose packages."* This is the real
  tension, but it is optional. Copying the whole tree keeps the Dockerfile simple
  (the status quo) at the cost of fat images. Selectively copying subpackages
  buys lean images at the cost of the complexity feared. Since torch dominates
  image size, the lean-image optimization isn't worth the complexity — so the
  disadvantage is a hypothetical you can decline, not a cost the restructure
  forces on you.

## The Independent Win: `shared/` Extraction

Regardless of the release decision or the full move, extracting the ~515 shared
lines (`model_registry`, `ssh_key`, `db_url`) into a small shared package is
positive-EV on its own:

- removes the mirrored `db_url.py` and its false comment;
- gives the genuinely-shared code one home;
- is ~a day of low-risk work with a small blast radius (5 import sites);
- does not pre-commit to the full flat layout.

This is the recommended first concrete step *if* any action is taken.

## Sequencing Options (if/when this is acted on)

1. **Pre-release hygiene (recommended).** Extract `shared/` now; do the full
   unified-tree move as the first commit of public-release prep, after
   workspace-reaper and the headscale fix land. Layout is final before it becomes
   public API; churn is paid once, in the cheapest window.
2. **Full move now.** Restructure on `develop` soon and absorb the conflict pain
   on in-flight branches plus Tilt/CI/helm churn mid-pilot. Hard to justify
   against the roadmap rules.
3. **Minimal only, defer layout.** Extract `shared/`, stop the duplication, and
   revisit the full layout only if the Go rewrite actually activates per
   [[go_rewrite]]'s trigger conditions.

## Non-Goals / Out of Scope

- Not a fix for `orchestrator/main.py` — that is the separate router/dispatch
  extraction in [[orchestrator_main_py_monolith]].
- Not a commitment to the Go rewrite — that has its own independent trigger
  conditions and is not a dependency in either direction.
- Not a change to the agent↔orchestrator HTTP boundary, which stays exactly as
  documented in [[agent_open_source_split]].

## Open Questions

- If the full move happens, what enforces the import direction? (Candidate:
  `import-linter` contract in CI, added in the same PR as the move.)
- Same binary vs. selective copy for images — accept fat images for Dockerfile
  simplicity, or pay complexity for lean ones? (Lean recommended *against* given
  torch dominance.)
- Does the move land before or after the partial `APIRouter` split is finished?
  (Finishing first means fewer files churned by the move.)
