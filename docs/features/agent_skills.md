---
tags:
  - agent-architecture
  - prompting
  - context-management
  - capabilities
  - skills
---

# Agent Skills

> **Status**: Design — 2026-06-18. Ready for Slice-1 planning.
> The experts rework ([[global_expert_management]]) has **landed** (Slices 1 + 3 + **Slice 2 capability-grants enforcement**, all on `develop`; only the Cockpit grants panel/control-greying remains), so skills now build on its *shipped* substrate — the orchestrator-resolved frozen-config model, the `experts`-style table + scope precedence, persona fencing, save-time credential deny-scan, **and the capability-grants gate (`evaluate()`) enforced at save/dispatch/resume/session**.
> **Decision: adopt Anthropic's open `SKILL.md` standard** ([agentskills.io](https://agentskills.io/specification)) verbatim, so SRW skills are portable to/from Claude Code and Codex rather than a bespoke format. The previously-deferred knobs are resolved below; what remains open is narrowed to tuning + the script/grants slice.

## Motivation

The end goal is to **relieve the agent of cognitive demand** so it spends its attention on the task itself, not on recalling how to perform recurring activities. Skills are a core part of that: reusable, self-contained "how to do X well" bundles the agent pulls in exactly when relevant — and that **users author themselves, the same way they can in Claude Code and Codex.**

SRW already has most of the machinery (see [Prior art](#prior-art-what-already-exists)). What is missing is the Claude Code/Codex affordance: a **catalog of reusable skills the agent selects from by judgment** (and, later, by context), instead of guides hard-wired to a specific tool or phase inside one expert's config.

## What a skill is — the open `SKILL.md` standard

As of Dec 2025, `SKILL.md` is an **open standard** (agentskills.io), already implemented by Claude Code, OpenAI Codex (`.agents/skills`), and VS Code, and stewarded by the same Linux-Foundation group as MCP. **We adopt it verbatim** — so a skill authored in SRW is a skill in Claude Code, and vice-versa (import/export "just works"), and we ride a standard instead of maintaining our own.

A skill is a **directory**, not a single document:

```
skills/
  <skill-name>/
    SKILL.md            # required: frontmatter + body
    references/*.md     # optional: read on demand
    scripts/*.py|sh|js  # optional: EXECUTED, not read
    assets/*            # optional: templates, schemas
```

- **Frontmatter** — `name` + `description`. The description is third-person and states *what it does + when to use it*; it is the trigger text the agent matches against. (The portable standard is just these two fields; Claude Code adds optional extensions — `allowed-tools`, `disable-model-invocation`, `paths` — we can adopt later.)
- **Body** — the procedure/guidance, kept small (<~500 lines / 5k tokens).
- **Bundled files** — references the body links to (read on demand) and **scripts the body tells the agent to run** (executed via the shell; the *source never enters context*, only the output does).

**Skills are not just prompts.** A skill can ship code — Anthropic's own `pptx`/`docx`/`xlsx`/`pdf` skills run bundled Python to do the deterministic, mechanical work while the model only orchestrates. Two flavours: **prompt-only** (judgment/guidance) and **script-bearing** (deterministic/compute-heavy). The idiom: *decision in the prompt, mechanical work in scripts.*

## Core design principle: progressive disclosure (three levels)

A skills system only *reduces* cognitive load if guidance is disclosed in tiers. This is the whole game, and each tier maps onto a primitive SRW already has:

| Level | What | When loaded | Cost | SRW mechanism |
|---|---|---|---|---|
| **L1 — metadata** | `name` + `description` | Always (the "menu") | ~100 tok/skill | Injected into the **Layer-1 system prompt** via the orchestrator-resolved blob |
| **L2 — body** | `SKILL.md` body | When the skill is invoked; stays for the session | <5k tok | Read on demand by **`use_skill`** (SRW's Skill tool) from the workspace |
| **L3 — files** | references / **scripts** | On demand | refs: file size · **scripts: output only** | refs via `read_file`; **scripts via `run_command`** |

The critical property: **a script's code is executed, never injected** — so a skill can bundle large scripts/datasets/reference docs with *no context penalty until used*. The menu is the contract; the body is the payload; scripts are tools the body points at.

## Activation — model-invoked, with deterministic bindings preserved

By default, invocation is **pure model judgment**: the agent reads the menu descriptions, matches the task, and calls `use_skill` to load a body. There is no automatic trigger.

Two cautions the research surfaced, both of which shape the design:

- **Model-invocation is unreliable.** Descriptions misfire (one community study saw ~coin-flip activation without careful wording — directional, not a benchmark). → Guidance that *must* happen keeps a **deterministic/enforced binding**, not model judgment (see [binding taxonomy](#one-artifact-instruction-documents-are-skills)).
- **The menu has a token budget.** At scale, descriptions are truncated and least-used skills are *silently dropped* (Claude Code exposes `skillListingBudgetFraction` / `maxSkillDescriptionChars`; the API caps a request at 8 skills). → SRW needs a menu-budget knob and per-expert curation; audit any agent with >~10 in-scope skills.

## The engine — and the one decision the research resolved

**Body delivery is settled: materialize skill directories into the workspace.** Because real skills run scripts that need a filesystem, the earlier "ship body text in the blob" and "lazy-fetch via orchestrator" options are dead — they can't execute scripts. Instead, the three levels land on primitives that already shipped with experts:

1. At dispatch, the orchestrator resolves the **in-scope skill set** (precedence: owner > project > global > bundled), and injects the **menu** (`name` + `description`) into the frozen `resolved_config` blob → it appears in the Layer-1 system prompt and is frozen with the job.
2. The workspace is provisioned with the in-scope **skill directories** (reusing the existing instruction-file deployment path; project skills may already be present via the cloned repo).
3. `use_skill` reads a body (L2) from the workspace; the body tells the agent to `run_command` any scripts (L3).

**Net-new code is small:** a skills scanner + a menu block in the orchestrator resolver, workspace materialization of skill dirs, and the `use_skill` tool. The resolved-blob model, workspace deployment, `read_file`, `run_command`, persona fencing, and credential deny-scan all already exist.

## One artifact: instruction documents *are* skills

The Layer-3 instruction files ([[prompting]]) — `todo_guide.md`, `research_guide.md` — are, in content, exactly skills. So skills **subsume** them: **one artifact type** (`SKILL.md`), not a separate "instruction document" concept. (Experts remain the heavier sibling — a persona/tools/workspace bundle an agent *is*, not a capability it *loads*; [[global_expert_management]].)

What differs between today's instruction files and a model-invoked skill is **not the artifact — it's how the artifact is *bound* to an activity.** Activation and enforcement are properties of the *binding*:

| Binding (trigger) | Activation | Use it when | Status |
|---|---|---|---|
| `before_tool:<tool>` + `enforce: true` | Tool-gated; the tool refuses until the skill is read | The guidance is **mandatory** before an action (a guardrail) | Exists (`todo_guide`) |
| `phase:strategic\|tactical` | Auto-injected on phase transition | One skill always applies during a phase | Exists (`research_guide`) |
| `model_invoked` | Agent picks from the menu (via `use_skill`); see [Activation](#activation--model-invoked-with-deterministic-bindings-preserved) | **Many** possible skills, only some apply — discovery has value | **New — this feature** |
| `semantic` | Surfaced by relevance via the memory engine | Same, but the system pre-filters by context | Future (later slice) |

Instruction documents become **skills bound with a deterministic (and possibly enforced) trigger**; the new binding this feature adds is `model_invoked` + the catalog.

**Load-bearing caveat — don't lose enforcement.** The `todo_guide` exists *because* agents reliably skip such guidance under pressure; the tool-gate was added to force it ([[prompting]]). Re-expressing it as a "skill" must **not** quietly turn it into an optional, model-invoked suggestion — it stays a skill with an *enforced* binding. Litmus test: *mandatory-before-an-action* → keep a deterministic/enforced binding; *optional-when-relevant* → `model_invoked`. (Model-invocation's unreliability makes this non-negotiable for guardrails.)

### What is *not* a skill

- **Expert identity / persona / methodology (Layer 1)** — always-on, rebuilt every call so the agent never forgets who it is. Skills are optional by nature; identity must never be optional. Stays in the system prompt. ([[prompting]])
- **Task / deliverable / reference files (Layer 4)** — job-specific outputs and domain reference, read on demand from `plan.md`. Not reusable cross-task capabilities. Out of scope.

The consolidation is precise: **Layer 3 → skills.** Layers 1 and 4 stay as they are.

## Skills × experts: composition

Both skills *and* experts are first-class objects users author. A skill is standalone and reusable; an expert **composes** skills. A skill reaches an agent two ways:

1. **Catalog (model-invoked).** In-scope skills appear in the menu and are loaded by the agent's judgment — the default "use skills like skills" mode, no expert involvement required.
2. **Expert binding.** An expert's `config` references skills and, per skill, may pin a binding: *none* → model-invoked; `before_tool:<tool>` (+ `enforce`) → deterministic/gated; `phase:…` → phase-injected.

The skill is written **once**; each expert decides **how it activates**. The same `code_review` skill might be model-invoked for a generalist but `phase:strategic`-injected for the `critic`. This is exactly what today's `instruction_files` list expresses (`file → trigger → enforce`); it generalizes to `skill → binding`, and `todo_guide` is already an instance.

**Resolved — open catalog, curation at scale.** All in-scope skills are discoverable in the menu by default (matches Claude Code, maximizes reuse). Because of the menu token budget, an expert may *scope/prioritize* which skills it exposes once catalogs grow; the exact budget + eviction policy is a tuning item (see Open).

## Security — reuse the experts pattern, sharper for scripts

- **Descriptions are always-on and user-authored** → a persistent prompt-injection vector (worse than a persona, which only loads when selected). Fence both the description (in the menu) and the body using the shipped **`fence_persona`** mechanism, marking skill content untrusted (`_source=db`), so it reads as a request, not operator policy.
- **Config fragments / frontmatter** run through the shipped **`hard_deny_scan`** at save time (422 on credential keys/paths), exactly as expert fragments do.
- **Script execution is an untrusted-code surface** — Anthropic explicitly warns a malicious skill can direct the agent to misuse tools. Allowing a skill's scripts to *run* therefore sits **behind the capability-grants gate** (experts' Slice 2, **shipped 2026-06-18** — the `evaluate()` PDP a `shell_tools`/`run_scripts`-style grant would extend). Consequently, **script-bearing skills are a later slice**; the first slices are prompt-only.

## Storage — mirror experts, plus a file tree

A skill is a *directory*, so storage splits by scope (precedence owner > project > global > bundled, like experts), gated by a new **`SKILLS_DB_ENABLED`** flag (helm `agent.skillsDbEnabled`, dev-on/prod-off — mirrors `EXPERTS_DB_ENABLED`):

- **Bundled** → `config/skills/<name>/` (mirrors `config/experts/`), scanned + cached at startup.
- **Project** → `skills/<name>/` in the project's Gitea jobs repo (mirrors the project `experts/` scan; identical to Claude Code's `.claude/skills`).
- **User / global** → rows in a `skills` table mirroring `experts` (`id, name (slug, uniq per owner), display_name, description, icon, color, tags[], owner_id, is_global, version, updated_by, ts`) **plus a `skill_files(skill_id, path, content)` table** holding the actual files. The **`SKILL.md` file is canonical**; the row's `name`/`description` are a denormalized cache re-parsed on save — so even frontmatter we don't yet interpret round-trips losslessly.

Everything else mirrors experts: CRUD + duplicate + reload, `list_skills`/`get_skill` MCP, and a Cockpit editor cloned from the experts editor. **Import/export is the native skill directory (zipped), not a JSON envelope** — a deliberate divergence from experts' export: an exported skill drops straight into `.claude/skills/`, and a real Claude Code/Codex skill imports unchanged. That interoperability is the point.

## Prior art — what already exists

- **Experts-v2** ([[global_expert_management]]) — **landed** (Slices 1 + 3 + Slice 2): orchestrator resolves a frozen `resolved_config` blob the agent hydrates; `experts` table (migration 0028) + `project_experts` + `jobs.expert_id`; owner > project > global > bundled precedence; `fence_persona` for untrusted user prompt content; `hard_deny_scan` at save. Skills reuse all of it. (Capability-grants enforcement = experts' Slice 2, **shipped 2026-06-18** — migration `0030`, the `evaluate()` PDP at four PEPs; script gating extends it with a script/shell-style grant.)
- **Four-layer prompt architecture** ([[prompting]]): Layer 3 *is* the skill artifact, bound deterministically; the passive (tool-gated `read_file`) and active (phase injection) mechanisms are shipped and proven by `todo_guide.md` / `research_guide.md`. This feature adds the `model_invoked` binding + catalog.
- **Memory & knowledge** ([[memory_light]], [[project_knowledge_base]]): hybrid retrieval that injects content by similarity to the current todo + phase — the engine the future `semantic` skill trigger rides.

## Locked decisions

1. **Adopt the open `SKILL.md` standard verbatim** (agentskills.io) — portability to/from Claude Code & Codex, no bespoke format. Optional Claude Code frontmatter extensions adopted later as needed.
2. A skill is a **directory** (SKILL.md + optional scripts/references/assets), not a single text blob.
3. **Progressive disclosure, three levels** — L1 menu (always-on) → L2 body (on invoke) → L3 files (refs read / **scripts executed, output-only**).
4. **Body delivery = materialize skill directories into the workspace.** Menu → Layer-1 prompt (in the frozen blob); body → `use_skill`/`read_file`; scripts → `run_command`.
5. Skills **subsume** Layer-3 instruction documents; activation/enforcement is a **binding** (trigger), not baked into the artifact. New binding = `model_invoked` + catalog.
6. **Deterministic/enforced bindings are preserved** for must-happen guidance (model-invocation is unreliable). Migrating `todo_guide`/`research_guide` to skills must keep their enforced/phase bindings.
7. **Consolidation is Layer 3 only** — expert identity (Layer 1) and task/deliverable files (Layer 4) are not skills.
8. Skills are **standalone, authored once**; experts **compose** them. Catalog is **open by default**, with per-expert curation available at scale.
9. **Storage mirrors experts** (`SKILLS_DB_ENABLED`, owner>project>global>bundled): bundled `config/skills/`, project Gitea `skills/`, user/global `skills` row + `skill_files` table (raw `SKILL.md` canonical, name/description denormalized). **Import/export is the native zipped skill directory**, not a JSON envelope — round-trips with Claude Code/Codex.
10. **Security reuses experts' mechanisms** (`fence_persona` for description+body, `hard_deny_scan` at save). Script execution reuses the **shipped** capability-grants gate (`evaluate()`), extended with a script/shell grant key — so script-bearing skills are deferred for **scope/risk** (get prompt-only right first), no longer for missing infra.
11. **Context-aware auto-suggest** is a later slice, built as a `semantic` trigger over the memory engine.

## Open items (tuning + sequencing, not blockers)

- **Menu budget tuning** — the listing budget fraction, per-entry cap, and truncation/eviction policy (mirror Claude Code's knobs); when a coarse pre-filter is warranted.
- **Script-execution grant key** — which key to add to the shipped `evaluate()` catalog to gate skill-script execution (a `run_scripts`/`shell_tools`-style grant).
- **Frontmatter extensions** — whether/when to honour `allowed-tools`, `disable-model-invocation`, `paths`.
- **Vocabulary** — disambiguating "skill" (this) from "expert" (role) in the UI.

## Slices (mirroring how experts shipped)

- **Slice 1 — Authoring foundation (get the basics right).** `SKILLS_DB_ENABLED`; storage (bundled `config/skills/` + project Gitea `skills/` + `skills` row + `skill_files`); the `SKILL.md` parser/serializer; CRUD + duplicate + native-zip import/export + edit; Cockpit editor cloned from the experts editor (`/skills`, `/skills/new`, `/skills/{id}/edit`); `hard_deny_scan` + path-traversal validation at save (persona fencing is a runtime/injection concern → Slice 2). **No agent runtime yet.** *DoD: round-trip a real Claude Code skill (import → edit → export, byte-comparable); create/edit from scratch via the UI.*
- **Slice 2 — Runtime engine.** Resolve the in-scope menu into the resolved blob; materialize skill dirs into the workspace; `use_skill` (L2); fenced Layer-1 menu injection (L1). Prompt-only, open catalog. *DoD: agent discovers and loads a skill end-to-end.*
- **Slice 3 — Expert bindings + migration.** `expert.config` `skill → binding`; migrate `todo_guide`/`research_guide` to skills, preserving their enforced/phase bindings.
- **Slice 4 — Script-bearing skills.** L3 script execution gated by the shipped grants `evaluate()` (extended with a script/shell grant key).
- **Later — Context-aware.** `semantic` trigger routing the catalog through the memory engine.

## Related

- [[global_expert_management]] — experts-v2 (landed); the substrate skills reuse
- [[prompting]] — four-layer prompt architecture; Layer 3 is the skill artifact bound deterministically
- [[tool_implementation]] — tool framework for the `use_skill` tool
- [[memory_light]] — hybrid retrieval engine for the future `semantic` trigger
- [[project_knowledge_base]] — project-scoped knowledge retrieval
- [[model_assembly]] — per-phase tool/model assembly
- [[subagent_delegation]] — related capability-routing concept

## References

- Open standard / spec — https://agentskills.io/specification
- Anthropic, *Agent Skills overview* — https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
- Anthropic, *Agent Skills best practices* — https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices
- Anthropic eng, *Equipping agents for the real world with Agent Skills* — https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Claude Code, *Skills* — https://code.claude.com/docs/en/skills
- Reference document skills (source-available) — https://github.com/anthropics/skills
