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

## One family: skills generalize Layer-3 instruction files

SRW already has two instruction-bundle concepts:

- **Experts** — heavy persona / tools / workspace bundles, pre-selected at job start ([[global_expert_management]]).
- **Layer-3 instruction files** — focused guides activated by *deterministic* triggers (`before_tool:<tool>`, `phase:strategic|tactical`) ([[prompting]]).

A skill is a Layer-3 instruction file with a new activation: **model-invoked, from a catalog.** Rather than introduce a fourth parallel concept — which would *add* conceptual load, the opposite of the goal — skills should be modeled as an extension of the existing `instruction_files` / `InstructionFileEntry` trigger taxonomy:

| Trigger | Activation | Status |
|---|---|---|
| `before_tool:<tool>` | Tool-gated (passive / enforced) | Exists |
| `phase:strategic\|tactical` | Injected on phase transition (active) | Exists |
| `model_invoked` | Agent selects from the catalog by reading descriptions | **New — this feature** |
| `semantic` | Surfaced by relevance via the memory/knowledge retrieval engine | Future (Phase 3) |

This keeps experts, instruction files, and skills as one coherent family with a single trigger model.

## Prior art — what already exists

- **Four-layer prompt architecture** ([[prompting]]): Layer 3 ("instruction files, auto-injected by trigger conditions") is the deterministic ancestor of skills. The passive (tool-gated `read_file`) and active (transient injection) mechanisms are already implemented and proven by `todo_guide.md` and `research_guide.md`.
- **Experts** ([[global_expert_management]]): the heavier sibling. Experts-v2 introduces DB-backed, user-authored, capability-gated bundles with user/project/global scope and an overlay model — exactly the substrate skills should reuse.
- **Memory & knowledge** ([[memory_light]], [[project_knowledge_base]]): hybrid (dense + sparse + recency) retrieval that injects relevant content by similarity to the current todo + phase. The future `semantic` skill trigger routes the skill catalog through this same engine — context-aware skill suggestion with no new retrieval infrastructure.

## Locked decisions (concept)

1. Skills use the Claude Code/Codex format (`SKILL.md` directory + frontmatter `name`/`description` + optional supporting files) for portability and a familiar authoring model.
2. Progressive disclosure is mandatory: only descriptions are always-on; bodies load on demand.
3. Skills are modeled as a generalization of Layer-3 instruction files (a new `model_invoked` trigger), **not** a separate subsystem.
4. Users can author skills (parity with Claude Code/Codex), but the authoring **substrate** (storage / scopes / grants) is inherited from experts-v2, not built here.
5. The engine is: catalog scan → Layer-1 menu injection → `use_skill` load tool.
6. Context-aware auto-injection/suggestion is explicitly a later phase, built as a `semantic` trigger over the memory engine.

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
- **Phase 2 — Starter library.** Bundled default skills covering the highest-value recurring activities.
- **Phase 3 — Context-aware.** `semantic` trigger routing the catalog through the memory engine to auto-surface/suggest skills.

## Related

- [[global_expert_management]] — experts-v2; provides the storage/scope/grants substrate skills reuse
- [[prompting]] — four-layer prompt architecture; Layer 3 is the deterministic ancestor
- [[tool_implementation]] — tool framework for the `use_skill` tool
- [[memory_light]] — hybrid retrieval engine for the future `semantic` trigger
- [[project_knowledge_base]] — project-scoped knowledge retrieval
- [[model_assembly]] — per-phase tool/model assembly
- [[subagent_delegation]] — related capability-routing concept
