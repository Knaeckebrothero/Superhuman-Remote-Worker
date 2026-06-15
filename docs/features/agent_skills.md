---
tags:
  - agent-architecture
  - prompting
  - context-management
  - capabilities
---

# Agent Skills

> **Status**: Design / concept — 2026-06-15
> **Implementation deferred** until the experts rework ([[global_expert_management]]) lands. Skills will ride experts-v2's storage, scope model, and capability-grants rather than build a parallel authoring stack. This document captures the concept and the locked conceptual decisions; storage/UI specifics are intentionally left open (see Deferred).

## Motivation

The end goal of this system is to **relieve the agent of cognitive demand** so it spends its attention on the task itself, not on recalling how to perform recurring activities. Skills are a core part of that: reusable, self-contained "how to do X well" bundles the agent discovers and pulls in exactly when relevant — and, crucially, that **users can author themselves, the same way they can in Claude Code and Codex.**

SRW already has most of the machinery for this (see [Prior art](#prior-art-what-already-exists)). What is missing is the Claude Code/Codex affordance: a **catalog of reusable skills the agent selects from by judgment** (and, later, by context), rather than guides hard-wired to a specific tool or phase inside one expert's config.

## What a skill is (parity with Claude Code / Codex)

A skill is a self-contained directory:

```
skills/
  <skill-name>/
    SKILL.md            # required: frontmatter (name, description) + body
    <supporting files>  # optional: scripts, references, templates
```

- **Frontmatter** — `name` + `description`. The description states *when to use* the skill (triggering conditions and symptoms), **not** what it does. This is what the agent reads to decide relevance.
- **Body** — the actual guidance/procedure, loaded on demand.
- **Supporting files** — optional scripts or reference material the body points to.

The format is deliberately identical to the Claude Code skill format so the mental model, the authoring experience, and (eventually) imported skills all carry over.

## Core design principle: progressive disclosure

A skills system only *reduces* cognitive load if guidance is disclosed progressively:

1. **Always loaded** — only the skill *menu*: each skill's name + description. Small, stable, survives compaction.
2. **Loaded on demand** — the full `SKILL.md` body, only when the agent decides the skill applies.
3. **Loaded only when reached** — supporting files/scripts, only when the body references them.

Injecting skill *bodies* eagerly would **add** cognitive load, not remove it — defeating the purpose. The menu is the contract; the body is the payload. "Work the same way as Claude Code/Codex" and "relieve cognitive demand" are therefore the same requirement.

## Architecture — the engine

Three pieces; each maps onto existing SRW machinery, so the net-new code is small.

| Skill piece | What it does | Existing machinery to mirror |
|---|---|---|
| **1. Catalog / scan** | Enumerate available skills as (name, description, path) | `_scan_experts()` and the per-project Gitea `experts/` scan → add a sibling `skills/` source |
| **2. Menu injection** | Put names + descriptions (only) where the agent always sees them | Layer-1 system prompt, where expert identity lives and which is rebuilt every call → survives compaction ([[prompting]]) |
| **3. Load tool** (`use_skill`) | Pull a skill's `SKILL.md` body into context on demand; expose its bundled files | The existing `read_file` self-injection pattern + the tool framework ([[tool_implementation]]) |

Retrieval, injection, repo scanning, config overlay, and the tool framework already exist. The genuinely new code is a scanner, a system-prompt menu block, and one tool.

## One artifact: instruction documents *are* skills

SRW already has the content. The Layer-3 instruction files ([[prompting]]) — `todo_guide.md`, `research_guide.md` — are, in content, exactly skills: focused "how to do X well" bundles. So skills don't sit *beside* instruction documents; they **subsume** them. There should be **one artifact type** (a skill / `SKILL.md`), not a separate "instruction document" concept. (Experts, by contrast, remain the heavier sibling — a persona/tools/workspace bundle that an agent *is*, not a capability it *loads*; [[global_expert_management]].)

What differs between today's instruction files and a Claude Code skill is **not the artifact — it's how the artifact is *bound* to an activity.** Activation and enforcement are properties of the *binding*, not of the skill:

| Binding (trigger) | Activation | Use it when | Status |
|---|---|---|---|
| `before_tool:<tool>` + `enforce: true` | Tool-gated; the tool refuses until the skill is read | The guidance is **mandatory** before an action (a guardrail) | Exists (`todo_guide`) |
| `phase:strategic\|tactical` | Auto-injected on phase transition | One skill always applies during a phase | Exists (`research_guide`) |
| `model_invoked` | Agent picks from the catalog by reading descriptions | **Many** possible skills, only some apply — discovery has value | **New — this feature** |
| `semantic` | Surfaced by relevance via the memory engine | Same, but the system pre-filters by context | Future (Phase 3) |

So instruction documents become **skills bound with a deterministic (and possibly enforced) trigger**; the new thing this feature adds is the *model-invoked* binding plus the catalog. One family, one artifact, a small binding taxonomy.

**Load-bearing caveat — don't lose enforcement.** The `todo_guide` exists *because* agents reliably skip such guidance under pressure; the tool-gate was added precisely to force it ([[prompting]]). Re-expressing it as a "skill" must **not** quietly turn it into an optional, model-invoked suggestion — it stays a skill with an *enforced* binding. Litmus test per guide: *mandatory-before-an-action* → keep a deterministic/enforced binding; *optional-when-relevant* → candidate for `model_invoked`. When exactly one guide always applies to an action, a deterministic binding is also cheaper than a discover-then-read round-trip.

### What is *not* a skill

- **Expert identity / persona / methodology (Layer 1)** — always-on, rebuilt every call so the agent never forgets who it is. Skills are optional/triggered by nature; identity must never be optional. It stays in the system prompt. ([[prompting]])
- **Task / deliverable / reference files (Layer 4)** — job-specific outputs and domain reference, read on demand from `plan.md`. Not reusable cross-task capabilities. Out of scope.

The consolidation is therefore precise: **Layer 3 → skills.** Layers 1 and 4 stay as they are.

## Prior art — what already exists

- **Four-layer prompt architecture** ([[prompting]]): Layer 3 ("instruction files, auto-injected by trigger conditions") *is* the skill artifact, bound with deterministic triggers. The passive (tool-gated `read_file`) and active (transient injection) mechanisms are already implemented and proven by `todo_guide.md` and `research_guide.md`. This feature adds the `model_invoked` binding + catalog on top of them.
- **Experts** ([[global_expert_management]]): the heavier sibling. Experts-v2 introduces DB-backed, user-authored, capability-gated bundles with user/project/global scope and an overlay model — exactly the substrate skills should reuse.
- **Memory & knowledge** ([[memory_light]], [[project_knowledge_base]]): hybrid (dense + sparse + recency) retrieval that injects relevant content by similarity to the current todo + phase. The future `semantic` skill trigger routes the skill catalog through this same engine — context-aware skill suggestion with no new retrieval infrastructure.

## Locked decisions (concept)

1. Skills use the Claude Code/Codex format (`SKILL.md` directory + frontmatter `name`/`description` + optional supporting files) for portability and a familiar authoring model.
2. Progressive disclosure is mandatory: only descriptions are always-on; bodies load on demand.
3. Skills **subsume** Layer-3 instruction documents: one artifact (`SKILL.md`), with activation/enforcement expressed as a *binding* (trigger), not baked into the artifact. The new binding this feature adds is `model_invoked` + the catalog.
4. Users can author skills (parity with Claude Code/Codex), but the authoring **substrate** (storage / scopes / grants) is inherited from experts-v2, not built here.
5. The engine is: catalog scan → Layer-1 menu injection → `use_skill` load tool.
6. Context-aware auto-injection/suggestion is explicitly a later phase, built as a `semantic` trigger over the memory engine.
7. The consolidation is **Layer 3 only.** Expert identity (Layer 1) stays always-on in the system prompt; task/deliverable files (Layer 4) stay read-on-demand. Re-expressing today's enforced guides (e.g. `todo_guide`) as skills **must preserve their enforced binding** — not silently make a mandatory guardrail optional.

## Deferred / open (resolve after the experts rework)

- **Storage & scopes** — file-based (Gitea project `skills/` + bundled config `skills/`) vs DB-backed rows with user/project/global overlay. Defer to whatever experts-v2 settles.
- **Authoring surface** — git commit (technical users) vs a Cockpit authoring page. Follow experts-v2's UI direction.
- **Capability gating** — which skills a runner may load; reuse experts-v2 `capability_grants`.
- **Bundled scripts/tools** — whether `use_skill` auto-registers a skill's scripts as callable tools, or the agent runs them via existing shell tools.
- **Name precedence** — merge/override behaviour when the same skill name exists at multiple scopes (bundled vs project vs user).
- **Menu token budget** — how many skills the always-on menu can hold before it becomes a cost in its own right; whether large catalogs need a coarse pre-filter.
- **Vocabulary** — how to disambiguate "skill" (this) from "expert" (role) in the UI and docs.

## Phasing (indicative; sequenced after the experts rework)

- **Phase 0 — Engine.** Catalog scan + Layer-1 menu + `use_skill`, proven against bundled skills in the config repo.
- **Phase 1 — User authoring.** Project-repo `skills/` and/or experts-v2 DB rows, reusing experts-v2 scopes + grants.
- **Phase 2 — Starter library + migration.** Bundled default skills covering the highest-value recurring activities, including today's `todo_guide` / `research_guide` re-expressed as skills (preserving their enforced/phase bindings).
- **Phase 3 — Context-aware.** `semantic` trigger routing the catalog through the memory engine to auto-surface/suggest skills.

## Related

- [[global_expert_management]] — experts-v2; provides the storage/scope/grants substrate skills reuse
- [[prompting]] — four-layer prompt architecture; Layer 3 is the deterministic ancestor
- [[tool_implementation]] — tool framework for the `use_skill` tool
- [[memory_light]] — hybrid retrieval engine for the future `semantic` trigger
- [[project_knowledge_base]] — project-scoped knowledge retrieval
- [[model_assembly]] — per-phase tool/model assembly
- [[subagent_delegation]] — related capability-routing concept
