---
tags:
  - feature
  - config-resolution
  - tools
  - cockpit
  - orchestrator
  - security
aliases:
  - tool policy vs membership
  - tools config schema
  - tool group policy
related:
  - "[[session_create_tool_toggles_cannot_enable_a_group]]"
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
  - "[[session_tool_group_enablement_is_computed_in_two_places]]"
  - "[[registered_tools_no_config_can_grant]]"
  - "[[stale_tool_names_degrade_every_worker_job_tool_load]]"
  - "[[global_expert_management]]"
  - "[[tool_permission_tiers]]"
  - "[[settings_design]]"
  - "[[datasource_redesign]]"
  - "[[tool_configuration_defects_and_fix_roadmap]]"
  - "[[tool_configuration_live_gates_2026-08-03]]"
  - "[[tool_configuration_deferred_findings]]"
---

# Tool Config: Policy vs Membership

> `tools.<category>` is a list of tool names. That one representation carries two
> unrelated facts — **membership** (which tools are in the group) and **policy**
> (whether the group is on) — and the overload is the root cause of a family of
> shipped defects. Membership already has an authority: `TOOL_REGISTRY`, which
> knows all 151 tools and their 24 categories. Config should carry policy.
>
> The fix is a **front-end**, not a rewrite. `true` / `false` / `{only: […]}` /
> `{except: […]}` normalise down to the exact list-of-names representation the
> whole stack already speaks, at one point, before anything downstream sees it.
> Every existing declaration is already in canonical form, so normalisation is
> the identity function on today's configs and the migration is opt-in per line.

**Status:** IMPLEMENTED 2026-08-02/03 on `develop`, **not pushed**. Commits 1–6
of [Sequencing](#sequencing) all landed; each carries an in-place amendment
above where the implementation diverged from the plan (three of them:
`grant:` became a tri-state rather than a boolean marker, there is no
`SESSION_TOOL_OVERRIDE_GROUPS` allowlist, and commit 6 went wider than the four
groups). Commit 7 — per-persona `only`/`except` for the 40 subsets, pure
readability — is untouched and still optional, and nothing under
["Not in this plan"](#not-in-this-plan) was taken.

**Live-gated 2026-08-03**, six gates across three rounds, all passing — including
the two properties this document's design rests on: a category-level `true`
expands to the *grantable* subset only (`agent_catalog: true` bound 5 tools with
all six `*_bundle` writes absent, measured from the agent), and every write
boundary refuses a cross-category name rather than dropping it (400 at eight
boundaries, each with a clean-body control). Evidence:
[[tool_configuration_live_gates_2026-08-03]]. Findings deferred during the run,
and the rulings settled along the way — the `shell` auto-tracking rule, why
`ToolsConfig` cannot be registry-derived, why `only` is never intersected — are in
[[tool_configuration_deferred_findings]].

Two of this document's own rules were only satisfied after the whole-branch
review, and both are now in:

- **[The request boundary](#sequencing) covers the expert surface.** Commit 5
  wired eight boundaries and missed `/api/experts` (create, update, import,
  duplicate, fork-a-default), where an expert's `config` is an authored layer
  under every job and session it drives. All five now run
  `validate_tool_override_fragment` and persist the canonical form.
- **"Off is a promise" has its mirror** (the rule lives in
  `src/core/tool_report.py::compose_tool_view`, which
  [the two consumers](#the-two-consumers-that-must-agree) both read). A
  category held *only* by per-tool `grant: "code"` names reports `on` with
  `settable: false` and a reason, because the runtime re-appends those after the
  merge (`srw_cloud_status`, `sleep`/`notify_user`,
  `request_workspace_upgrade`, `checkout_project_repository`) and unticking
  cannot release them.

**Still owed:** the live gates for commits 5 and 6, blocked on the k3d cluster.
Register and full execution record:
[[tool_configuration_defects_and_fix_roadmap]].
Line anchors below were verified on `develop` 2026-08-02 and several have since
moved with the implementation they describe.
**Filed against:** `docs/issues/session_create_tool_toggles_cannot_enable_a_group.md`
**Scope:** `config/*.yaml`, `config/schema.json`, `src/core/loader.py`,
`orchestrator/services/config_resolver.py`, `src/core/session_tool_overrides.py`,
`src/tools/registry.py`, and four cockpit hosts of `tools-group.component.ts`.

## Recommendation up front: do not rewrite the config system

The user asked whether the config system needs a refactor. It does not. The
**resolved** representation is correct and load-bearing: `load_tools`
(`src/tools/registry.py:356`) takes a flat `List[str]`, regroups it by *registry*
category at `:395-400`, and binds only explicitly named tools — that is a good
design and it is what makes cross-category smuggling detectable at all. Eight
independent consumers pattern-match on `== []` to mean "disabled"
(inventoried in [Consumers of the canonical form](#consumers-of-the-canonical-form)),
and every one of them is correct given a canonical input.

The defect is entirely on the **authoring** side. So the change is:

1. Add an authoring vocabulary that can say "on".
2. Normalise it to the existing canonical form at one seam, early.
3. Delete the duplicated name lists that only existed because config carried names.

A full rewrite would simultaneously touch the dispatch PEP
(`src/core/capability_grants.py:147`), the session attach path
(`orchestrator/main.py:1716`), the hydration blob
(`src/api/persistent_app.py:1296`), the legacy experts-off path
(`orchestrator/main.py:1835-1870`) and four cockpit surfaces — during a feature
freeze, with two live pilots. The phased plan in
[Sequencing](#sequencing) leaves the system strictly better at every stopping
point and can be abandoned after commit 1, 2, 4, 5 or 6 without leaving debris.

## The overload, and what it costs

`tools.shell: []` reads as a statement about shell's contents. It is a policy
flag on a category that has five tools. Four consequences, all verified:

**`[]` means both "empty" and "disabled".** All 24 registry categories have at
least one tool, so no group is ever legitimately empty. Every empty list in every
config is a disable marker. There are **80** of them across the 12 configs that
declare `tools:` — 13 in `config/session_base.yaml`, 13 in
`config/worker_base.yaml`, 54 in the other ten.

**"Off" is expressible, "on" is not.** Disabling is self-describing. Re-enabling
requires the full member list, and the cockpit looks for it in the layer it is
overriding — which says `[]`:

```ts
// cockpit/src/app/views/agent-settings/tools-group.component.ts:422-427
const defaults = this.mode() === 'live' ? SESSION_TOOL_GROUP_NAMES : this.defaultsTools();
for (const cat of this.expertDisabledCategories) {
  if (!disabled.has(cat) && defaults[cat]?.length) {   // [] → length 0 → falsy
    tools[cat] = [...defaults[cat]];                   // never runs
  }
}
```

`defaultsTools()` is the API's `defaults_tools`, which the orchestrator fills
from `base.get("tools", {})` (`orchestrator/main.py:27549`) — the very map in
which the group is `[]`. The New Session form can disable a group and can never
enable one.

**Name lists cannot track the registry.** Adding a tool to an existing category
reaches zero existing configs. **67 of 151 tools** are named by no config at all;
57 of those are granted by runtime code and **10 are unreachable by any route**.
The inventory and the per-tool disposition live in
`docs/issues/registered_tools_no_config_can_grant.md` — this design consumes it
rather than restating it. Two consequences shape the sections below: the code
grants make runtime injection [a first-class
layer](#runtime-injection-is-a-layer-not-an-edge-case), and six of the ten
unreachable tools are `*_bundle` writes inside user-tickable groups, which is
[the biggest risk in the
design](#the-biggest-risk-true-is-wider-than-todays-closed-vocabulary).

> **Correction to the brief.** An earlier count put the unreferenced set at six
> (`sleep`, `notify_user`, `request_workspace_upgrade`, `srw_cloud_status`,
> `kb_index`, `kb_lint`). Four of those six *are* granted, by code — just never
> through YAML. The real unreachable ten are `kb_index`, `kb_lint`,
> `delegate_work`, `resume_delegation_child` and the six `*_bundle` tools.
> `delegate_work` is the sharp one: `src/core/loader.py:1961` documents a config
> flag gating a tool that nothing can enable.

**Configs are unreadable as documentation.** Commit `57430a2a` (2026-07-22)
removed shell from every session and delegation from every worker job while
claiming "No functional changes introduced". The diff read like tidying a list,
because that is what a list of names looks like.

## Schema

`tools.<category>` accepts five forms. The first two are new; the last two are
today's forms, unchanged and not deprecated.

| Form | Meaning | Resolves to |
|---|---|---|
| `true` | every config-grantable tool the registry has in this category | the full category, minus code-granted tools |
| `false` | no tools | `[]` |
| `{except: [names]}` | the category minus these; tracks registry additions | full category − names |
| `{only: [names]}` | exactly these; frozen | names |
| `[names]` | legacy spelling of `{only: [names]}` | names |
| `[]` | legacy spelling of `false` | `[]` |

```yaml
tools:
  shell: false
  core: true
  workspace:
    except: [delete_directory, rename_file]   # category minus a few; auto-tracks
  research:
    only: [web_search, extract_webpage]       # genuine narrow selection; frozen
```

Rejected at schema validation, with a message naming the replacement:

- `{}` and `{only: []}` — wordy spellings of `false`. Both are also live hazards:
  see [Security boundary](#security-boundary).
- `{only: [...], except: [...]}` — both keys in one mapping. The two intuitive
  readings (`only − except`, or "one wins") diverge, and the combination is
  reachable by accident through `deep_merge`. Reject it rather than pick.
- Any name in `only` / `except` that is not in that category per
  `get_tools_by_category` (`src/tools/registry.py:116`).

### Write `true`/`false`, not `on`/`off` — a deliberate divergence from the brief

PyYAML resolves `on`, `off`, `yes` and `no` to booleans (YAML 1.1), so accepting
them costs nothing — by the time Python sees the node it is already a `bool` and
there is no code path that can tell the difference. The argument is about the
*other* two readers:

- `config/schema.json` is consumed by `yaml-language-server` via the
  `# yaml-language-server: $schema=` header on line 1 of all 14 config YAMLs.
  YAML 1.2's core schema — which modern YAML tooling defaults to — resolves `off`
  to the **string** `"off"`, not `false`. A file that means `false` to the agent
  and `"off"` to the editor's validator is precisely the "the artifact lies about
  itself" failure this document exists to end. **Verify the resolved type in the
  editor before choosing the spelling; if in doubt `true`/`false` is correct in
  both YAML 1.1 and 1.2.**
- DB-backed experts store their fragment as JSON (`json.loads` at
  `orchestrator/services/config_resolver.py:140`). JSON has only `true`/`false`.
  Spelling the same policy differently in YAML and in the DB re-creates a
  two-representations problem inside the feature that is supposed to remove one.

House style: `true`/`false` everywhere. `on`/`off` are accepted because PyYAML
gives them to us free, and are never written.

### JSON-schema implications

`config/schema.json:262-275` types `tools` as an object with 17 enumerated
category properties, each `{"type": "array", "items": {"type": "string"}}`. Three
existing gaps matter here:

1. **`additionalProperties` is absent** from the `tools` subschema, so it defaults
   to `true` and any category key validates. The file uses
   `"additionalProperties": false` at `:459`, `:535`, `:622`, `:795`, `:818`, so
   the omission is a gap, not house style. Today `tools.workspaces:` (typo) is
   silently ignored end to end.
2. **Seven registry categories are missing from the schema**: `browser_direct`,
   `email`, `loop`, `product_help`, `repo`, `session_task`, `webdav`. Four configs
   already use `browser_direct` and `config/worker_base.yaml` uses `webdav`; they
   validate only because of gap 1.
3. **Item type is a bare string** with no `enum`. There is no validation of tool
   names against the registry anywhere in the schema. That the stale-name count
   is currently zero is discipline (and
   `tests/test_config_tool_names_are_registered.py`), not enforcement.

The new schema fragment, per category:

```json
"shell": {
  "description": "Shell tools (src/tools/shell/). Policy, not membership.",
  "oneOf": [
    { "type": "boolean" },
    { "type": "array", "items": { "type": "string" }, "uniqueItems": true },
    {
      "type": "object",
      "additionalProperties": false,
      "oneOf": [
        { "required": ["only"],   "properties": { "only":   { "type": "array", "items": { "type": "string" }, "minItems": 1, "uniqueItems": true } } },
        { "required": ["except"], "properties": { "except": { "type": "array", "items": { "type": "string" }, "uniqueItems": true } } }
      ]
    }
  ]
}
```

`minItems: 1` on `only` is what rejects `{only: []}`; `additionalProperties:
false` plus the inner `oneOf` is what rejects `{}` and the both-keys mapping.
Generate the 24 per-category blocks from `TOOL_REGISTRY` rather than hand-writing
them, and add `"additionalProperties": false` to the `tools` object — that closes
gaps 1 and 2 in the same change and makes gap 3 fixable later by dropping an
`enum` into the generated `items`.

### The category vocabulary already disagrees with the registry

Before `true` can expand anything, "what is a category" needs one answer. Today
there are four lists and they differ:

| Source | Count | Notes |
|---|---|---|
| `TOOL_REGISTRY` (`get_categories`, `src/tools/registry.py:130`) | 24 | the authority |
| `ToolsConfig` dataclass fields (`src/core/loader.py:1459-1491`) | 23 | has `mcp`; lacks `product_help`, `session_task` |
| `get_all_tool_names` tuple (`src/core/loader.py:4473-4497`) | 23 | same set as `ToolsConfig` |
| `config/schema.json:266` properties | 17 | lacks 7 registry categories |

`mcp` exists in `ToolsConfig` but not in the registry, because MCP tool names are
discovered at runtime. `product_help` and `session_task` exist in the registry but
not in `ToolsConfig` (`src/core/loader.py:1458-1491`), so `tools.product_help:
[read_product_guide]` in a YAML file is **silently discarded** — those 5 tools
arrive only via the code floors at `persistent_session.py:1415-1443`.

**Position: every registry category gets a config field.** The alternative —
maintaining a curated subset of "config-addressable" categories — is a fourth list
to drift, and the drift is silent in the worst direction (a key that validates,
parses, and does nothing). A category whose tools should not be config-grantable
expresses that per-tool with `grant: "code"`, which is checkable, rather than by
being absent from a dataclass, which is not.

So: derive `ToolsConfig`, `get_all_tool_names` and the generated `schema.json`
blocks from `get_categories()` plus the one declared exception (`mcp`), in the
same commit as the normaliser. Pin it with a three-way agreement test —
`set(get_categories()) | {"mcp"} == ToolsConfig fields == get_all_tool_names
tuple == schema.json tools properties` — so adding a registry category fails
loudly at the four places it must be reflected instead of silently at none.

## Resolution algorithm

### Normalisation is layer-local

`expand(policy, category) -> list[str]` needs only the value and the registry. It
never consults the parent layer. That single property is what keeps this change
small:

```
expand(True,               c) = [t for t in category(c) if not code_granted(t)]
expand(False,              c) = []
expand({"except": xs},     c) = [t for t in expand(True, c) if t not in xs]
expand({"only":   xs},     c) = list(xs)
expand(list(xs),           c) = list(xs)
```

Because each layer resolves independently to a `list[str]`, layers are merged by
the **existing, unmodified** `deep_merge` (`src/core/loader.py:178-215`).

**The semantics being preserved, stated once and explicitly: lists REPLACE, dicts
MERGE.** `src/core/loader.py:211-213` is the implementation —

```python
else:
    # Arrays and scalars: override replaces
    result[key] = value
```

— and `orchestrator/main.py:12943-12945` corroborates it from the other side, in
the docstring of a server-generated override that had to spell out every group
because of it: *"Each tool group is spelled out explicitly because `deep_merge`
replaces lists but merges dicts by key — an omitted group is INHERITED, not
empty."* That comment is the clearest existing statement of the rule and the
clearest evidence of what it costs.

So the merge rule, stated unambiguously for every layer combination, is the rule
that already holds today: **the most specific layer that mentions a category wins
that category wholesale; a layer that does not mention it inherits.** There is no
per-tool union, no per-tool subtraction, and no new merge behaviour to learn.

| Parent | Child | Resolved |
|---|---|---|
| `true` | *(absent)* | full category |
| `true` | `false` | `[]` |
| `true` | `{except: [a]}` | full − a |
| `{only: [a,b]}` | *(absent)* | `[a, b]` |
| `{only: [a,b]}` | `{only: [a]}` | `[a]` |
| `{only: [a,b]}` | `{except: [c]}` | **full − c** — see below |
| `{except: [a]}` | `{except: [b]}` | full − b (not full − a − b) |
| `[a,b]` | `true` | full category |
| `[]` | `true` | full category |

### Parent says `only`, child says `except`

**The child wins entirely, and `except` is relative to the registry category —
never to the parent's selection.** A child `{except: [c]}` under a parent
`{only: [a,b]}` resolves to the whole category minus `c`, which is *wider* than
the parent. This is stated explicitly because the intuitive reading is the other
one.

The justification: layer-local expansion is what avoids touching `deep_merge`,
and avoiding `deep_merge` is what keeps the blast radius at "authoring". The
parent-relative alternative makes `except` order-dependent, forces normalisation
to run post-merge, and then hits a real trap — with post-merge normalisation,
parent `{only: [x]}` and child `{except: [x]}` dict-merge into
`{only: [x], except: [x]}`, and the child that explicitly asked for `x` gets
nothing. A config system whose presenting complaint is "`[]` means two things"
must not ship a second silent-empty.

**Widening is the status quo, not a regression.** Today a child list replaces a
parent list with anything at all; there is no restrict-only notion for tools in
`deep_merge`. Narrowing is enforced one layer up, by capability grants
(`restrict_only` in `src/core/capability_grants.py:18-61`), which run on the
merged result. Two mitigations, both cheap:

- Log at WARNING at resolve time when a child `except` produces a strictly larger
  set than its parent, naming both layers.
- A lint test over the bundled configs asserting no `except` sits above an `only`
  for the same category in the `$extends` chain. Bundled configs are the only
  place we control; DB experts get the warning.

Authors narrowing an already-narrowed parent write `only`.

### Where normalisation runs

Four call sites, ordered by importance. The function is idempotent, so a later
sweep re-running over an already-normalised fragment is a no-op — which is what
makes the belt-and-braces placement safe.

1. **`src/core/loader.py:load_and_merge_config`** — normalise `config_data` and
   `parent_data` immediately after `yaml.safe_load`, before the `$extends` merge
   at `:274`. Covers all 14 bundled YAMLs and the bundled-expert inheritance
   chain.
2. **`orchestrator/services/config_resolver.py:resolve_config`** — normalise
   `base_defaults`, `bundled_leaf`, the expert fragment, and each of
   `project_overrides` / `db_overrides` / `user_settings` / `request_override`
   before their `deep_merge` calls at `:122`, `:127` and `:157`. Then one
   idempotent sweep over `data` **before** `capture["merged_fragment"]` at `:165`.
   That ordering is a hard requirement — see
   [Security boundary](#security-boundary).
3. **`src/api/persistent_app.py`** — normalise `config_override` before
   `_apply_session_tool_group_markers` at `:1728` and `:6405`. This is the legacy
   experts-off path, which reads the raw override rather than the merged
   fragment.
4. **`src/core/loader.py:load_agent_config_from_dict`** — a defensive sweep before
   `ToolsConfig(...)` at `:2610` (and its sibling at `:2357`). `ToolsConfig` fields
   are typed `List[str]`, and `get_all_tool_names._category_names`
   (`src/core/loader.py:4464-4470`) silently returns `[]` for a non-list. A `true`
   leaking this far would **silently disable** the group — the exact failure mode
   under repair. Make this sweep raise rather than coerce; a `bool` arriving here
   is a missed call site, not an input.

### Runtime injection is a layer, not an edge case

There is a config layer that lives in Python. A YAML-only reading of tool grants
is wrong by 57 tools. Naming it explicitly is a prerequisite for this design,
because `true` and `except` are defined against the registry and the registry
includes tools that YAML has never been able to grant.

The full order, with the new normalisation seam marked:

```
  bundled base ($extends chain)
→ base_defaults
→ expert fragment (bundled leaf or DB row)
→ project_experts.config_override
→ DB config_overrides (0022)
→ user persistent_agent settings
→ request config_override  ← includes SERVER-GENERATED fragments (see A)
══ normalise ══ deep_merge complete ══ capture["merged_fragment"] ══ PDP ══
→ datasource-derived categories                          (B — replaces)
→ code floors                                            (C — additive)
→ live datasource resetup                                (D — post-dataclass)
```

**A — server-generated request fragments are not a new layer.** `_critic_config_override`
(`orchestrator/main.py:12961-12969`) stamps `evaluation: [approve_job,
return_job_with_feedback]`, a narrowed `core`, and `communication: []` onto every
verification critic. `orchestrator/services/project_loops.py:898` stamps
`{"loop": ["loop_plan"]}` onto a campaign critic. Both ride the ordinary request
layer and obey the ordinary merge, so both are covered by the normaliser with no
special handling — they simply become eligible for the new vocabulary later
(`communication: false`, `evaluation: {only: […]}`).

**B — datasource-derived categories are machine-owned and replace.**
`datasource_tool_categories` (`src/core/datasource_setup.py:160-211`) generates
lists for `sql`, `mongodb`, `graph`, `webdav`, `email`, `repo` and `mcp` from what
is actually attached — `[]` when detached, the read set, the write set, or the
email tier — applied above the merge at `src/api/persistent_app.py:1345`
(`agent_tools.update(...)`). It already emits canonical form and must stay
authoritative: a detached datasource has to strip the tools whatever the config
said.

**C — code floors are additive and gated elsewhere.** The 38 code-only tools
(inventoried in
`docs/issues/registered_tools_no_config_can_grant.md`) are bound by
`src/api/persistent_session.py:1408-1557` and `src/agent.py:3066-3068` on runtime
conditions — `config.officer.enabled` for `sleep` / `notify_user`, cloud-mount
activity for `srw_cloud_status`, the lite tier for `request_workspace_upgrade`,
break-glass flags for the product-guide pair.

**D — live datasource resetup writes past the whole model.**
`src/api/persistent_session.py:1819` does
`setattr(self.config.tools, category, list(names))` directly on the dataclass,
after resolution, after `ToolsConfig`. Its docstring at `:1717-1719` says why:
the closed session vocabulary *"silently drops sql/graph/mongodb/webdav, so they
must never ride `config.update`"*. It writes canonical lists, so it stays correct
— but it is outside the layer model and any future change to the resolved shape
has to account for it.

#### Does `tools.<cat>: false` override an injection?

**No — and for the machine-owned categories it must not even be spelled that
way.** Taking the three kinds separately:

- **B (datasource):** config has no say today, and making `false` a veto would be
  actively dangerous *right now*: all twelve configs currently write `sql: []`,
  `mongodb: []`, `graph: []` and friends. If `[]`→`false` were a veto, the
  migration would silently disable every datasource in the product. So `[]` on a
  connector category is not a policy statement at all — it is the same
  two-meanings overload one level down, here meaning *"config does not manage
  this"*. **The migration must delete those keys, not convert them.** Eleven of the
  26 base empties are of this kind (5 in `session_base`, 6 in `worker_base`).
  Deleting them frees `false` to mean a genuine veto later ("no SQL tools even
  when a Postgres datasource is attached") — which is a real wish, currently only
  expressible through the `datasource_tools` capability grant
  (`src/core/capability_grants.py:169-173`) at user scope, never per-expert.
- **C (code floors):** a code-only tool's on/off lives at *its own* gate. `core:
  false` does not suppress `sleep`; `config.officer.enabled: false` does. This is
  defensible but only if it is discoverable, so the registry entry must name the
  gate: `{"grant": "code", "gate": "officer.enabled"}`. Without that field the
  rule is folklore, which is how we got here.
- **D:** post-dataclass, outside the model, unaffected.

#### The redesign raises the cost of a wrong `category`

Today a tool's `category` only decides which toolkit builder loads it and which
YAML key must name it. Under `true` and `except`, category membership becomes
**policy**: everything in the category is granted unless excluded. A
miscategorised tool silently changes what a policy means.

`srw_cloud_status` is the live example — it reports cloud-mount status and sits
in the `shell` category alongside `run_command`. Under `shell: true` an operator
who wanted "shell commands on" also gets it. It is `grant: "code"` so nothing
breaks, but the general hazard is real: **audit `category` assignments in the same
commit that makes `true` meaningful**, and treat a category change as a
policy-affecting change from then on.

#### `mcp` resolves late

MCP tool names are discovered after the agent connects, so `mcp` has no registry
membership at config-resolution time. `mcp: true` normalises to the existing
`["*"]` sentinel, expanded by `expand_tool_wildcards`
(`src/tools/registry.py:172-184`) after discovery, at `src/agent.py:3052` and
`src/api/persistent_session.py:1411`. Note that function's latent bug: `"*"` in
*any* category injects the `mcp` category's tools, because it operates on the
flattened list. Do not generalise `"*"`; keep it MCP-private and let `true` be the
general form everywhere else.

### Consumers of the canonical form

These all keep working unchanged, *provided* normalisation runs upstream. This
list is the argument for normalising to `list[str]` rather than teaching each
consumer a new vocabulary.

| Site | Predicate |
|---|---|
| `src/api/persistent_app.py:1313` | `tools.get(group) == []` → sets `_fleet_management_disabled` &co. |
| `orchestrator/main.py:3716`, `:3723`, `:3730` | `tools.get(<group>) == []` |
| `orchestrator/main.py:3742` | `tools.get(group) == []` → marker map |
| `orchestrator/main.py:1841` | `explicit.get(group) != []` → `/tool-groups` legacy branch |
| `src/core/session_tool_overrides.py:125` | `tools.get(group) != []` → `/tool-groups` resolved branch |
| `src/tools/orchestrator/catalog.py:574-575` | `if value` / `if value == []` → the expert-detail tool line |
| `src/core/capability_grants.py:161-175` | `_truthy(tools.get(...))` → the dispatch PDP |
| `src/core/loader.py:4464-4470` | `isinstance(value, list)` → flatten for `load_tools` |

`catalog.py:574-575` is the one that fails *silently* on an un-normalised input: a
raw `false` is neither truthy nor `== []`, so the category would appear in
neither the "Enabled" nor the "Disabled" line of the expert description the agent
reads.

## Backward compatibility

**There is nothing to deprecate, and no clock to run.** Both legacy forms are
already the canonical resolved form:

- `[name, …]` is bit-identical to `expand({only: [name, …]})`.
- `[]` is bit-identical to `expand(false)`, preserving today's "disabled" meaning.

`normalize_tool_policy` is therefore the **identity function on all 143 existing
declarations** across the 12 configs. That is the single most important property
of this design: the enabling commit changes no resolved tool set anywhere, and can
be verified to change none by the golden test from commit 1.

Consequences:

- **DB-backed experts need no migration.** Their stored fragments are JSON lists;
  lists stay canonical forever. Nothing has to be edited in a commit, and nothing
  breaks if a row is never touched again. When the Admin/expert editor learns the
  new forms, existing rows keep working alongside new ones.
- **Bare lists stay first-class.** `{only: [...]}` and `[...]` are two spellings of
  one thing. House style prefers `only` when the intent is "frozen selection", but
  a bare list is never wrong and is never linted against. Removing a spelling that
  costs nothing to support is not worth a migration during a feature freeze.
- **`[]` stays first-class too**, though house style writes `false`. A lint that
  *reports* remaining `[]` occurrences is useful; one that fails CI is not.

The only genuinely one-way step is `true` adoption, because `true` binds the
config to the registry's future. That is handled per declaration in
[Migration](#migration), never in bulk.

## The two consumers that must agree

Today the cockpit maintains a name-list mirror of the backend vocabulary, kept in
sync by `tests/test_session_tool_group_mirror.py` (which regex-parses TypeScript
source — `_parse_ts_mirror` at `:22-35` depends on single-quoted literals and a
`\n};` terminator, so a Prettier run can break it). Under policy-carrying config
the cockpit stops knowing names.

| Symbol | File:line | Fate |
|---|---|---|
| `SESSION_TOOL_GROUP_NAMES` | `agent-settings.types.ts:62-82` | **delete** — 29 hardcoded tool names whose only job is building a re-enable payload that becomes `true` |
| `toolGroupDefaultsConfig` | `agent-settings.types.ts:110-119` | **delete** — it exists solely to re-expand bools into name lists so they can be `deepMergeConfig`d as lists |
| `defaultsTools` input | `tools-group.component.ts:269`, forwarded `agent-settings.component.ts:93` | **delete** — plus the `defaults_tools` API field at `orchestrator/main.py:27549`, `:27569`, `:27691`, `:27701` and the model type at `api.model.ts:119` |
| `SESSION_TOOL_GROUP_BASE_ENABLED` | `agent-settings.types.ts:94-99` | ~~**keep**~~ → **deleted 2026-08-03**, see correction below |
| `SESSION_TOOL_CATEGORIES` / `JOB_TOOL_CATEGORIES` / `LIVE_TOOL_CATEGORIES` | `agent-settings.types.ts:24-52` | **keep** the first two (presentation only); `LIVE_TOOL_CATEGORIES` **deleted 2026-08-03**, see below. Since 2026-08-04 both surviving lists are the READ-FAILED fallback only — both creation forms now render the server's answer when they have one, so a stale entry costs a label on a degraded path and nothing more |
| `CAT_TO_GRANT` | `tools-group.component.ts:274-278` | **keep** — category → capability-grant key, orthogonal to membership |

> **Corrected 2026-08-03, at implementation.** Two rows in the table above were
> wrong, and one addition was needed.
>
> * **`SESSION_TOOL_GROUP_BASE_ENABLED` is deleted, not kept.** Its stated job
>   was to be the offline fallback when `/tool-groups` 404s. But it answers a
>   question the surface no longer asks: with three states, "no answer" is a
>   state the UI can *render* ("the resolved toolset could not be read") rather
>   than a hole it has to fill with a guess. Substituting a stale four-entry
>   copy of `session_base.yaml` for a missing measurement is the same fail-open
>   in a smaller costume — and the copy could only ever be right about the base
>   layer, never about the expert, project, grant or backend layers that
>   actually decide.
> * **`LIVE_TOOL_CATEGORIES` is deleted.** The live pane renders whatever the
>   resolved read returns. Filtering a complete answer down to four is the same
>   untruth as a toggle that does nothing.
> * **One thing had to be ADDED, and it is served, not transcribed.** `shell`
>   refuses `tools.shell: true` (`ENUMERATE_ONLY_CATEGORIES`), so "the UI can
>   turn shell on" (D3) needs an enumeration. Both tool-groups reads now carry
>   `enumerate_only` — `src.core.tool_policy.enumerate_only_members()`, derived
>   from the registry — so the enumeration is served rather than mirrored. A
>   hardcoded shell tool list in the cockpit would have been a fifth parallel
>   list added by the change that deletes four.
>
> `tests/test_session_tool_group_mirror.py` is repurposed rather than deleted:
> it now fails if any of the four retired symbols reappears, or if a served
> enumeration is hardcoded in the cockpit.

Two places in the cockpit already speak pure policy and get simpler rather than
different:

- `GET /api/persistent/threads/{id}/tool-groups` returns
  `Record<string, boolean>` (`api.service.ts:147-151`). It is the one surface in
  the stack that already has the target representation. Widen it from the four
  closed groups to all 24 categories and it becomes the cockpit's single source
  for checkbox state.
- `settings-pane.component.ts:409-416` (`desiredState`) already collapses tool
  groups to booleans internally — *"so list-content differences never masquerade
  as changes"* — and `:445-450` (`applyChanges`) re-expands them through
  `SESSION_TOOL_GROUP_NAMES`. The bool → names → bool round-trip is pure ceremony
  that disappears.

And one read predicate must widen:

```ts
// tools-group.component.ts:35 — today
if (Array.isArray(value) && value.length === 0) disabled.add(key);
// must also treat `false` as disabled, and `true` / {only|except} as enabled
```

**Four hosts, not three.** `tools-group.component.ts` is mounted by
`job-create.component.ts:277` (`mode="job"`),
`session-create.component.ts:170` (`mode="session"`),
`settings-pane.component.ts:79` (`mode="live"`), and — directly, bypassing
`app-agent-settings` — `expert-editor.component.ts:287-293`, whose `getOverrides()`
output is persisted as a **stored expert fragment** (`:656`), not a request-layer
override. Whatever representation the tools group emits therefore lands in the
`experts` table. That is fine (JSON booleans), but it means the cockpit change and
the DB-expert story are the same change, and `MANAGED_CONFIG_KEYS`
(`expert-config.ts:10-26`, which lists `'tools'`) governs the round-trip.

## Security boundary

`src/core/session_tool_overrides.py` exists because `load_tools` groups by
*registry* metadata, not by the key a fragment arrives under: a request saying
`tools.canvas: ["run_command"]` would otherwise instantiate a shell tool. Its
`SESSION_TOOL_OVERRIDE_NAMES` (`:20-60`) fuses two separable concerns into one
literal:

1. **Which groups may a request set at all** — the allowlist. `{orchestrator,
   agent_catalog, workflows, canvas}`.
2. **Which names are legal inside each** — the anti-smuggling check. 29 hand-typed
   tool names.

Split them. Concern 2 is exactly what the registry answers:

```python
SESSION_TOOL_OVERRIDE_GROUPS: frozenset[str] = frozenset(
    {"orchestrator", "agent_catalog", "workflows", "canvas"}
)

def validate_session_tool_overrides(config_override):
    accepted = {}
    for group in SESSION_TOOL_OVERRIDE_GROUPS:
        if group not in tools:
            continue
        value = tools[group]
        if isinstance(value, bool):
            accepted[group] = value           # carries no names — nothing to smuggle
            continue
        names = _policy_names(value)          # list, or only/except payload
        unexpected = sorted(set(names) - get_tools_by_category(group))
        if unexpected:
            raise SessionToolOverrideError(...)
        accepted[group] = value
    return accepted
```

This is a **strengthening**, not a port:

- `true`, `false` and `{except: […]}` carry no names at all. Membership is derived
  from the registry keyed by the group name, so cross-category smuggling is
  structurally inexpressible for those three forms.
- The remaining vector (`only`, bare list) is closed by one subset check that works
  for **all 24 categories**, not the four somebody happened to type out. Adding a
  group to the allowlist stops being "transcribe its tool names correctly" and
  becomes a one-word edit.
- The 29 duplicated names in `session_tool_overrides.py` and the 29 in
  `agent-settings.types.ts` both stop existing, and so does the drift test between
  them.

### The biggest risk: `true` is wider than today's closed vocabulary

`SESSION_TOOL_OVERRIDE_NAMES` is **not** a transcription of the registry
categories. It is a hand-curated subset, and the difference is not cosmetic:

| Group | Registry | Closed vocabulary | In the registry but not the vocabulary |
|---|---|---|---|
| `orchestrator` | 17 | 14 | `checkout_project_repository`, `get_stuck_jobs`, `steer_worker_job` |
| `agent_catalog` | 9 | 5 | `get_expert_bundle`, **`set_expert_bundle`**, `get_skill_bundle`, **`set_skill_bundle`** |
| `workflows` | 9 | 7 | `get_automation_bundle`, **`set_automation_bundle`** |
| `canvas` | 3 | 3 | — |

A naive `agent_catalog: true` therefore grants `set_expert_bundle` and
`set_skill_bundle` — tools that **mutate the expert and skill catalogue** — to any
session that ticks "Experts & Skills". `set_automation_bundle` is the same shape
for automations. Six of these nine tools are in the ten-tool "unreachable by any
route" set from [The overload](#the-overload-and-what-it-costs): nobody has ever
audited them in a session context, because nothing could grant them.

> **RESOLVED 2026-08-03 — and by mitigation 2, taken further than written.** The
> nine tools were classified as this section demands, and the six `*_bundle`
> writes went where it guessed they belonged: their own category. They now live in
> `catalog_authoring` behind a deny-by-default capability grant
> ([[agent_authored_catalog_entries]]), so `agent_catalog` and `workflows` contain
> only reads and their `true` expansion equals the session vocabulary **by
> construction** — the hazard below cannot occur, rather than being held off by a
> mark. The table above is now historical: those categories are 5 and 7 tools,
> matching their vocabularies exactly.
>
> One correction to this section's framing. It calls the `set_*_bundle` trio
> catalogue writes wanting "their own admin-scoped category". Own category, yes;
> admin-scoped, no — the endpoints they call are already owner-scoped, so the
> capability shipped user-scoped and became a feature. Mitigation 3's temporary
> "expand `true` to the curated set" divergence was never needed.

**This is the single largest hazard in the design**, because it is exactly the
failure mode the design is supposed to prevent, inverted: instead of a config
silently losing tools, a config silently gains them. Three mitigations, and the
first two are mandatory:

1. **The golden snapshot from commit 1 is the gate.** Any `true` adoption that
   moves the snapshot must be an explicit, titled commit. That is the mechanism,
   not a convention.
2. **Classify these nine before any `true` reaches a closed group.** Each is either
   safe to grant (add it to the group), or it belongs in a different category, or
   it is `grant: "code"`, or it is dead and should leave the registry. The
   `set_*_bundle` trio are catalogue writes and almost certainly want their own
   admin-scoped category rather than membership in a user-tickable group.
3. Until step 2 lands, the request boundary may accept `true` for a closed group
   and expand it to the *curated* set rather than the registry category — a
   deliberate, commented divergence with an expiry, not a permanent second
   vocabulary.

Note the shape of the problem: the curated vocabulary encodes a **safety**
judgement ("which of this category may a session request?") that the registry
category does not carry. Membership-from-the-registry is right for the 20
categories with no such judgement; for these four the judgement has to move into
registry metadata rather than be deleted along with the name list.

### Ordering requirement: normalise before the PDP

`src/core/capability_grants.py:147` (`evaluate`) is the single policy decision
point, fed the merged fragment at `orchestrator/main.py:1727` via
`_enforce_dispatch_grants(_cap["merged_fragment"], …)`. It tests
`_truthy(tools.get("shell"))` at `:161`, and `_truthy` (`:122-123`) treats
`(None, False, 0, "", [], {})` as false. Against un-normalised policy values that
is *mostly* right by luck and wrong in two places:

| Raw value | `_truthy` | Correct | Outcome if the PDP sees it raw |
|---|---|---|---|
| `true` | True | True | correct |
| `false` | False | False | correct |
| `{only: [run_command]}` | True | True | correct |
| `{except: [shell_read]}` | True | True | correct |
| `{}` | **False** | — | **grant violation missed** |
| `{only: []}` | **True** | False | **violation fabricated for an empty group** |

The two failure rows are why `{}` and `{only: []}` are schema errors, and why the
sweep must land **before** `capture["merged_fragment"]` at
`config_resolver.py:165` rather than inside `load_agent_config_from_dict` at
`:167`. The same reasoning applies to the other three gated reads — `delegation`
at `:165-168`, the seven connector categories at `:169-173`, and `browser_direct`
at `:174-175`.

Grant precedence is unchanged: `restrict_only` grants clamp with `meet`
(`capability_grants.py:64-80`) and a config asking for a denied category is a
fail-loud 422 at dispatch, not a silent drop.

### Two open items, recorded but not fixed here

**The job-create path has no allowlist at all.** ~~`validate_session_tool_overrides`
is called from exactly three places — `orchestrator/main.py:21473` (live
`config.update`), `:22569` (`POST /persistent/threads`), and
`src/api/persistent_app.py:1367` (runtime sanitiser). None is the job path. Job
creation strips only `_PUBLIC_JOB_CONFIG_RESERVED_KEYS`
(`orchestrator/main.py:4627-4632`, four lifecycle markers) and then accepts
arbitrary `config_override.tools.<anything>: [<any registered name>]`. That is
literally the scenario `session_tool_overrides.py:1-13` was written to prevent,
open on the other surface, with only the dispatch PDP behind it. **Out of scope
here**~~ — but the generic validator above makes the fix a one-line reuse instead of
transcribing 24 name sets, so file it and land it after commit 5.

> **CLOSED 2026-08-03**, together with commit 5 — the two are one change, because
> the reason the job path had no allowlist was that writing a fourth copy of a
> hand-curated list was unthinkable. `POST /api/jobs` now calls
> `_with_validated_tool_overrides` on the request fragment, on **both** the
> public and the internal path: `X-Internal-Key` is transport authentication,
> not authorization, and `create_worker_job` forwards a model-authored
> `config_override` verbatim, which is exactly a caller who can write
> `tools.canvas: ["run_command"]`. The server's own fragments are untouched —
> `_critic_config_override` and the campaign loop's `{"loop": ["loop_plan"]}` go
> through `postgres_db.create_job` directly, and the officer slot patch merges
> in after the boundary. `tests/test_tool_override_boundary.py`.
>
> **Three more, found by review of that change and closed with it.** "Exactly
> three places" was never the whole list:
>
> * `POST /api/sessions/{id}/prepare` (`orchestrator/routers/sessions.py`) — the
>   body's `config_override` flows to `_resolve_session_config`, where a
>   non-None value **replaces** the thread's persisted override outright
>   (`main.py:1684-1688`). A write boundary, not a hint.
> * `POST` / `PATCH /api/automations` — stored raw, and
>   `create_job_from_automation` passes it **directly** to `db.create_job`, so
>   it never crosses `POST /api/jobs` and every cron fire re-plants it.
> * `POST` / `PATCH /api/projects` `default_config_override` — merged under
>   every job in the project, making it a cross-principal escalation: the
>   planter is a project owner, the runner is any member, and `evaluate()` keys
>   off the category name. Validated on the **write path only**, so no existing
>   row is read or rejected until someone rewrites it. Note the cockpit *does*
>   send this field (`project-detail.component.ts::toggleProjectMemory`
>   re-submits the whole stored override), so a project already holding an
>   invalid `tools` block will surface it at the next toggle — which is the
>   intended way to learn a row is broken.

**The "base-granted shell bypasses the grant" claim needs re-verification.**
`docs/issues/session_create_tool_toggles_cannot_enable_a_group.md` states the
`shell_tools` grant "applies to request overrides rather than the resolved base".
`_enforce_dispatch_grants` is fed the *merged* fragment, which includes the base,
so on the resolved path a base-granted shell should trip the grant — unless the
0030 grandfathering backfill (noted at `config_resolver.py:162-164`) granted
`shell_tools: true` to every existing user, which would produce the same observed
behaviour for a different reason. Settle this with a live check before anyone
relies on either reading; nothing in this design depends on the answer.

## Migration

**Invariant: every migration commit must be a no-op on resolved tool sets, proven
by the golden snapshot from commit 1. Any name that appears or disappears is a
separate, titled commit with the behaviour change in the subject line.** This is
the whole lesson of `57430a2a`.

### Step A: classify code-only tools in the registry

Without this, `true` is not behaviour-preserving and the migration becomes 63
judgement calls. With it, most of them vanish.

Add `grant: "code"` to `TOOL_REGISTRY` metadata for tools that runtime code binds
**instead of** config, and define `expand(true, c)` to exclude them. `true` then
means *"every tool in this category that config is allowed to grant"*.

The distinction matters and is easy to get wrong. Of the 57 unreferenced tools
with a code path, only **38 are code-*only***:

| Set | n | Site |
|---|---|---|
| connector tools (`sql`, `mongodb`, `graph`, `webdav`, `email`, `repo`, `mcp`) | 28 | `src/core/datasource_setup.py:47-130` |
| `sleep`, `notify_user`, `request_workspace_upgrade` (`core`) | 3 | `persistent_session.py:1547`, `:1555`; `src/agent.py:3067` |
| `task_add`, `task_complete`, `task_list` (`session_task`) | 3 | `persistent_session.py:1415-1417`, unconditional |
| `read_product_guide`, `get_product_capabilities` (`product_help`) | 2 | `persistent_session.py:1429-1443` |
| `srw_cloud_status` (`shell`) | 1 | `persistent_session.py:1526` |
| `checkout_project_repository` (`orchestrator`) | 1 | `persistent_session.py:1540` |

The other 19 — the `orchestrator` / `agent_catalog` / `workflows` appends at
`persistent_session.py:1471-1520` — are **not** code grants. They are the
legacy experts-off compatibility shim described at `orchestrator/main.py:1835-1845`:
the runtime appends those canonical lists when no disable marker is present. On
the resolved path config still decides, and `agent_catalog: true` from a request
must expand to the real five tools. Marking them `grant: "code"` would make that
group permanently un-enableable — the current bug, re-introduced by the fix.

Effect on the recurring 100%-omission rows:

| Tool | Category | Omitted by | Code grant | With `grant: "code"` |
|---|---|---|---|---|
| `srw_cloud_status` | shell | 8/8 | `persistent_session.py:1526` | excluded from `true` — no change |
| `sleep` | core | 8/8 | `persistent_session.py:1555` | excluded — no change |
| `notify_user` | core | 8/8 | `persistent_session.py:1555` | excluded — no change |
| `request_workspace_upgrade` | core | 8/8 | `persistent_session.py:1547`, `agent.py:3067` | excluded — no change |
| `kb_index`, `kb_lint` | knowledge | 3/3 | none | **still a decision** |
| `delegate_work`, `resume_delegation_child` | delegation | 3/3 | none | **still a decision** |

So `core: true` resolves to exactly today's 6 tools, and the drift disappears
without granting anything new. Four of the six "unreachable" tools from the brief
were never drift at all — they were an unclassified grant path.

Each `grant: "code"` entry also carries `gate:` — the config key or runtime fact
that actually controls it (`officer.enabled`, `cloud_mount.active`, lite tier,
break-glass flag). Without it the rule in [Does `tools.<cat>: false` override an
injection?](#does-toolscat-false-override-an-injection) is folklore.

The four remaining decisions are about genuinely dead tools. Take them
**separately and first**: `kb_index`/`kb_lint` and
`delegate_work`/`resume_delegation_child` are either dead code to delete from the
registry, or capabilities to enable deliberately. Do not let `knowledge: true` or
`delegation: true` be the thing that decides. Per-tool dispositions belong in
`docs/issues/registered_tools_no_config_can_grant.md`, not here.

### Step B: the 23 FULL declarations become `true`

Zero resolved-set change today; registry additions reach them from now on.

| Config | Categories |
|---|---|
| `config/session_base.yaml` | `workspace`, `research`, `browser_direct`, `citation`, `git`, `canvas` |
| `config/worker_base.yaml` | `workspace`, `research`, `browser_direct`, `citation`, `communication`, `git` |
| `bughunter` | `git`, `browser_direct` |
| `critic`, `curator`, `designer`, `designer-interactive` | `git` |
| `product-qa` | `git`, `browser_direct` |
| `scholar` | `research`, `git`, `citation` |

`git` appearing 9 times as a full list is the clearest single argument for this
change: nine configs independently transcribe the same five names, and a sixth git
tool would reach none of them.

### Step C: the 40 subsets

Roughly half are drift, half are intent. After step A the split resolves cleanly.

**Migrate to `except`** — short, incidental omissions where tracking the registry
is what the author would want:

| Config | Category | Becomes |
|---|---|---|
| `session_base`, `worker_base` | `knowledge` | `{except: [kb_index, kb_lint]}` — `true` once the kb decision lands |
| `curator` | `knowledge` | `{except: [kb_export, kb_index, kb_lint]}` — drops `kb_export` too, which is intent, not drift |
| `worker_base` + 6 experts (`bughunter`, `critic`, `curator`, `designer`, `product-qa`, `scholar`) | `core` | `true` after step A — the only omissions are the 3 code-only tools |
| `developer` | `core` | `{except: [todo_list]}` — 5/9, one genuine omission on top of the 3 |
| `designer`, `designer-interactive`, `scholar` | `workspace` | `{except: [delete_directory, rename_file, use_skill]}` |
| `developer` | `git` | `{except: [git_show]}` |
| `bughunter` | `shell` | **no change — leave the bare list.** A bare list already *is* `only`. Both `true` and `{except: [srw_cloud_status]}` now raise for `shell`; see the rule below |
| `critic`, `developer`, `scholar` | `delegation` | `{only: [spawn_subagent]}` until the `delegate_work` decision lands |

**Keep as `only`** — genuine per-persona narrowing, frozen deliberately:

| Config | Category | Size | Character |
|---|---|---|---|
| `critic`, `curator`, `developer` | `workspace` | 6/14 | identical 6-tool set: `read_file`, `write_file`, `list_files`, `search_files`, `file_exists`, `get_document_info` |
| `bughunter`, `product-qa` | `workspace` | 8/14 | adds `edit_file`, `create_directory` |
| `interactive` | `workspace` | 5/14 | `read_file`, `write_file`, `list_files`, `search_files`, `use_skill` — the only config that keeps `use_skill` while dropping the file-inspection pair |
| `developer` | `research` | 5/8 | web, no papers |
| `bughunter`, `designer`, `designer-interactive`, `product-qa` | `research` | 2/8 | `web_search`, `extract_webpage` |
| `interactive` | `research` | 1/8 | `web_search` only |
| `centurion` | `orchestrator` | 12/17 | job control without repo/project reads |
| `critic`, `scholar` | `shell` | 2/5 | `run_command` + `cancel_command`, no `shell_read` |

> **Correction to the brief.** The critic/curator 6-tool workspace set is *not*
> "effectively read-only" — it contains `write_file`. What it removes is mutation
> of existing artifacts and structure (`edit_file`, `delete_file`,
> `delete_directory`, `create_directory`, `move_file`, `copy_file`, `rename_file`,
> `use_skill`). "Append-only, no destructive edit" is the accurate description.
> It is also byte-identical to `developer`'s workspace set, which undercuts reading
> it as a review-specific posture — it may itself be a copied snapshot. Flag it for
> a human decision rather than encoding it as intent.

**`shell` accepts `only` and `false`. Nothing else.** `true`, `{except: [...]}`
and `{except: []}` are all refused by the normaliser and by `config/schema.json`.

The rule is about auto-tracking, and only `only` avoids it. `true` means "this
category and whatever is added to it later"; `except` means exactly the same
thing minus a fixed subtraction, recomputed from the registry on every
resolution. For a code-execution category both are wrong: a tool added to
`shell` in the registry would land in every config that used either form, with
no diff to review anywhere. `only` forces a titled commit.

> **Rationale corrected 2026-08-02, three times — the last one is the rule
> above.** The first two attempts both permitted `except`, which does not
> survive the argument they were making: `{except: [srw_cloud_status]}` expands
> to the same four names `true` would, recomputed every resolution, so it
> auto-tracks identically. A rule that forbids `true` while blessing `except`
> is spelling-based, not semantic. `only` is the only form that does not
> auto-track, so `only` is the rule.
>
> The earlier reasoning, kept so it is not re-derived: the original reason was
> the `run_command` / `shell_execute` mode-alias pair, which
> `get_all_tool_names` (`src/core/loader.py:4507-4510`) rewrites based on
> `extra.shell.mode`. That reason does not survive contact: `bughunter` already
> names both halves today, and `expand_true("shell")` is byte-identical to what
> it already grants — so `true` would have been pure identity for the one config
> the migration table proposed it for. A first correction swapped that row to
> `{except: [srw_cloud_status]}`, which expands to the *same four names* and
> therefore fixed nothing.
>
> The real reason is auto-tracking. `true` means "this category, and whatever
> is added to it later". For a code-execution category that is the wrong
> default: a tool added to `shell` in the registry would silently land in every
> config that said `true`, with no diff to review anywhere. Explicit
> enumeration forces a titled commit. The alias-pair behaviour is real but
> orthogonal — it makes naming both halves redundant, not dangerous.

### The 80 empty declarations — two kinds, two different fixes

Not a single mechanical sweep. `[]` on a connector category means something
different from `[]` on a policy category, and conflating them is how this
migration could break every datasource in the product.

| Kind | Count (bases) | Categories | Becomes |
|---|---|---|---|
| **Policy** — config genuinely says "off" | 15 of 26 | `shell`, `orchestrator`, `agent_catalog`, `workflows`, `core`, `evaluation`, `delegation`, `communication`, `canvas` | `false` |
| **Machine-owned** — config does not manage this | 11 of 26 (5 `session_base`, 6 `worker_base`) | `graph`, `sql`, `mongodb`, `webdav`, `email`, `repo` | **delete the key** |

The reasoning for the second row is in [Does `tools.<cat>: false` override an
injection?](#does-toolscat-false-override-an-injection) — writing `false` there
would look like a veto, and one day it will *be* a veto, at which point every
config that "migrated" would silently kill its own datasources. Delete the keys
and let `schema.json` mark those six categories machine-owned. Same treatment for
the other ten configs' connector empties.

Do the policy row per file, in its own commits, after the normaliser is in.
`config/session_base.yaml:97`'s comment — the one written by the commit that caused
the regression — gets deleted along with the ambiguity it was excusing.

## Sequencing

Each commit stands alone. Stopping after any of them leaves the system better than
it is now.

**Commit 1 — the regression test. Independent of this entire design; land it
first, today.** A golden snapshot of the resolved tool set per config:
`tests/test_config_tool_grants_snapshot.py` walks `session_base`, `worker_base`,
`interactive` and every `config/experts/*/config.yaml` through
`load_and_merge_config` + `get_all_tool_names` and asserts the sorted result
against a checked-in fixture. This is what would have caught `57430a2a`, and what
makes every later commit verifiable. `tests/test_config_tool_names_are_registered.py`
checks only the inverse direction (every named tool exists) and passes happily on
an empty list. While here, add the unknown-category-key assertion — `schema.json`
cannot catch it.

**Commit 2 — `normalize_tool_policy` + registry-derived expansion.** Wired at the
four call sites, plus the generated `schema.json`, plus deriving `ToolsConfig`,
`get_all_tool_names` and the schema blocks from `get_categories()` with the
three-way agreement test. **No config file changes.** The commit-1 snapshot must
not move — that is the acceptance criterion.

**Commit 3 — `grant: "code"` + `gate:` classification** for the 38 code-only
tools, plus a test that no config names one and a test that the unmarked set is
*not* marked. Makes `true` behaviour-preserving for `core` and `shell`.

> **Corrected 2026-08-02, after implementation.** The count "19" was right but
> its description was not: the must-not-mark set is **29**, and the legacy shim
> itself appends **26** names. The 19 decomposes as 16 shim names (the other 10
> being named by `config/experts/centurion`) plus `approve_job`,
> `return_job_with_feedback` and `loop_plan`. Implemented as a tri-state
> `grant` (`"code"` / `"explicit"` / absent) rather than the `gate:` marker
> proposed here — `gate:` already carries a descriptive-string role at
> §"Step A", so overloading it as a boolean predicate would recreate the very
> conflation this document exists to remove. `gate:` stays descriptive on every
> classified entry. See `src/tools/orchestrator/jobs.py` and
> `tests/test_tool_grant_classification.py`.

**Commit 4a — the 80 empty declarations.** `[]` → `false` for the 15 policy
categories; **delete the key** for the 11 machine-owned connector categories.
Snapshot must not move. This is the readability payoff and it is independent of
`true`.

**Commit 4b — decide the nine tools** the registry has and the closed vocabulary
does not, six of them `*_bundle` writes. **This gates commit 5**, because commit 5
is the point at which a user ticking "Experts & Skills" starts sending
`agent_catalog: true`. If the decision is not ready, commit 5 ships with mitigation
3 from [the biggest
risk](#the-biggest-risk-true-is-wider-than-todays-closed-vocabulary) — expand a
closed group's `true` to the curated set, commented, with an expiry — and 4b
removes the divergence later.

**Commit 4c — migrate the 23 FULL declarations to `true`.** None of them is on a
closed group, so this is independent of 4b. Snapshot may move only where commit 3
explicitly licensed it.

**Commit 5 — the request boundary.** `SESSION_TOOL_OVERRIDE_GROUPS` + registry
membership validation, accepting `true`/`false`/`only`/`except`. Server-side only;
the cockpit still sends lists and still works.

> **Corrected 2026-08-03, at implementation.** There is no
> `SESSION_TOOL_OVERRIDE_GROUPS`. A group allowlist would have been a fifth
> parallel list, and the "which categories may a request address?" question it
> answers has only one honest answer: all of them. So
> `validate_tool_override_fragment` (`src/core/tool_policy.py`) validates
> **every** category against the registry, at all three boundaries plus the job
> path, and the plan's two consequences change accordingly:
>
> * `{"tools": {"shell": true}}` is **not** "dropped and logged". It is a 400 —
>   from `ENUMERATE_ONLY_CATEGORIES`, which already refuses `true` for `shell`.
>   `{"tools": {"shell": []}}` is accepted and honoured, which is the Defect-2
>   fix for that checkbox.
> * Anything the boundary will not honour is **rejected**, never dropped.
>   Replacing one silent discard with a narrower silent discard fixes nothing;
>   the precedent is `_validated_reasoning_level`. That rule bites this design's
>   own `expand_category_true` warning: `{"tools": {"sql": true}}` returning 200
>   with `[]` is "asked for ON, got OFF". The warning stays correct for a config
>   layer; at a request boundary an affirmative policy that expands to nothing
>   is now a 400. `false` / `[]` remain legal, since asserting an unmanaged
>   category is off costs nothing.
>
> The plan also assumed commit 5 preceded any widening of what a *closed group*
> may name. It does not: membership is checked against the whole registry
> category, because `grant: "explicit"` restricts category-level policy and not
> an explicit name (`config/experts/centurion` names two of them). See "the
> biggest risk" above — mitigation 2 landed, and `true` still expands to exactly
> the curated set. The residual is that a request may now *name*
> `set_expert_bundle` under `agent_catalog`, which the four-group list refused.
> That is bounded: the tool acts as the session's own user
> (`_get_client(user_id=context.user_id)`), so it grants an agent something the
> user can already do in the cockpit — and the job path accepted it outright
> until this commit.

**Commit 6 — the cockpit.** ✅ **DONE 2026-08-03.** Send a policy (`true`, or the
served `enumerate_only` enumeration for `shell`) and `[]` for off; delete
`SESSION_TOOL_GROUP_NAMES`, `SESSION_TOOL_GROUP_BASE_ENABLED`,
`toolGroupDefaultsConfig`, `LIVE_TOOL_CATEGORIES`, the `defaultsTools` input and
the `defaults_tools` API field; drop the `mode()` branch at
`tools-group.component.ts:422`. **This is the commit that fixes the motivating
bug**, and it went wider than "the four allowlisted groups": both surfaces now
render every category the resolved read returns, in three states, with the
server's own reason — because two states cannot express "unavailable, and here
is why" (D7), and forcing them to try is what produced the defect.

**Commit 7 — optional.** Per-persona `only`/`except` adoption for the 40 subsets.
Pure readability. Abandonable with no debris.

### Not in this plan

Each is its own decision with its own risk, and none is a prerequisite:

- **Restoring `tools.shell: [run_command, shell_read]` to
  `config/session_base.yaml`** — item 0 of the motivating issue, a one-line revert
  of `57430a2a`. It should land *before* commit 1 so the snapshot pins the intended
  state rather than the regressed one, but this design neither blocks it nor
  depends on it.
- Adding `shell` to `SESSION_TOOL_OVERRIDE_GROUPS` and letting the `shell_tools`
  grant be the real gate (issue item 3). The new scheme makes this *safe* —
  `shell: true` carries no names — but "should the checkbox work" is a product
  decision.
- Closing the job-path allowlist gap.
- The reasoning-level clearers (issue Part 3) — unrelated, same form.
- Unifying `_session_tool_group_disabled_markers` with
  `session_tool_group_enablement`
  (`docs/issues/session_tool_group_enablement_is_computed_in_two_places.md`).

## Test strategy

**Pinning what exists (commit 1, before anything else):**

- Golden snapshot of resolved tool names per config. Fixture-based so a diff shows
  exactly which names moved.
- Unknown category keys in any config fail. `schema.json` cannot catch these
  (`additionalProperties` is open at `:262`).
- **Three-way category agreement**: `set(get_categories()) | {"mcp"}` equals the
  `ToolsConfig` field set, equals the `get_all_tool_names` tuple, equals the
  `schema.json` `tools` properties. Adding a registry category must fail here
  rather than silently produce a key that parses and does nothing.

**The normaliser:**

- Table test: every form × a representative category → expected list. Include
  every rejection case with its message.
- **Identity property**: `normalize(f) == f` for all 143 existing declarations
  across the 12 configs. This is the backward-compatibility guarantee, expressed
  as an executable assertion.
- **Idempotence**: `normalize(normalize(x)) == normalize(x)`, which is what makes
  the four-call-site placement safe.
- `expand(true, c)` excludes `grant: "code"` tools, for every category.

**Merge order** — a matrix over base × expert × request across all forms, asserting
the resolved list for each. Assert the parent-`only` / child-`except` **widening**
case explicitly, with a comment saying it is intended, so nobody "fixes" it into a
silent-empty.

**Runtime injection:**

- A config with **no** `sql` / `mongodb` / `graph` / `webdav` / `email` / `repo`
  key still binds the datasource-derived tools when one is attached, and binds
  none when detached. This is the test that would catch a migration that wrote
  `false` on a connector category.
- `core: false` does not suppress `sleep` when `officer.enabled` is true; the
  officer gate does. Same for `srw_cloud_status` under `shell: false`.
- `_critic_config_override` (`orchestrator/main.py:12961`) and the campaign-loop
  `{"loop": ["loop_plan"]}` fragment (`project_loops.py:898`) resolve identically
  before and after the normaliser — they are ordinary request-layer fragments and
  the identity property must hold for them too.

**The PDP seam:**

- `resolve_config`'s `capture["merged_fragment"]` contains no `bool` and no `dict`
  under `tools`, for every layer combination. This is the ordering requirement as a
  test.
- `evaluate` fires `shell_tools` for `shell: true` and does not for `shell: false`.
- Both `_truthy` failure rows from the table above are unreachable, because the
  schema rejects `{}` and `{only: []}`.

**The request boundary:**

- `{"tools": {"canvas": {"only": ["run_command"]}}}` → 400. The classic smuggling
  case, now generic.
- `{"tools": {"canvas": true}}` → accepted, resolves to exactly the three canvas
  tools.
- `{"tools": {"shell": true}}` → dropped (not in `SESSION_TOOL_OVERRIDE_GROUPS`),
  and — per issue item 5 — **logged**, so the silent discard stops being silent.

**Cockpit:**

- `getOverrides()` in `session` mode, group ticked whose base is off →
  `{tools: {orchestrator: true}}`. Fails today; this is the motivating bug as a
  unit test.
- `disabledToolCategoriesFromConfig` treats `false` and `[]` alike, and `true` /
  `{only}` / `{except}` as enabled.
- `tests/test_session_tool_group_mirror.py` loses
  `test_cockpit_mirror_matches_backend_vocabulary` and `_parse_ts_mirror`; keeps
  the two bool-map assertions.

**Live gate (dev, after commit 6).** Create a session with a previously
un-enableable group ticked; confirm the agent's bound tool count from the pod log
(`Loaded N tools for persistent session`) — the same evidence that settled
`docs/done/session_tool_group_checkbox_disagrees_with_the_agent.md`. **The checkbox
state is not evidence.** That is the whole lesson of this bug class.

## Non-goals

- **Rewriting the config loader or the layer model.** `deep_merge`
  (`src/core/loader.py:178`) and the layer order from
  `docs/done/global_expert_management.md` are unchanged. This design is
  specifically constructed to avoid touching them.
- **Per-tool policy.** The unit stays the category. A user who wants three tools
  from `workspace` writes `only`.
- **Making tool policy `restrict_only`.** Narrowing is enforced by capability
  grants, one layer up. Adding a second restrict-only system inside config
  resolution is a much larger change with its own failure modes.
- **A UI for `only` / `except`.** The cockpit keeps rendering checkboxes, which
  means `true` / `false`. Authoring subsets stays a YAML/expert-editor activity.
- **Deleting the bare-list or `[]` spelling.** Both stay supported indefinitely.
- **Validating tool names in `schema.json` via `enum`.** Possible once the schema
  is generated, but `tests/test_config_tool_names_are_registered.py` already covers
  it and the generated-enum churn is not worth it during a freeze.
- **Fixing `expand_tool_wildcards`' category-blind `"*"`** (`registry.py:172`).
  Recorded above; MCP is the only user and it works.
- **Making `tools.<cat>: false` a veto over datasource injection.** The design
  deliberately *frees* that spelling by deleting the connector keys, but does not
  use it. Per-expert "no SQL even when attached" is a real wish and a separate
  feature; today it is only expressible as the `datasource_tools` grant at user
  scope.
- **Moving the code floors into the layer model.** They stay in Python. This design
  documents where they sit and requires them to declare their gate; it does not
  relocate them.
- ~~**Rehoming the `*_bundle` tools** out of `agent_catalog` / `workflows`.~~
  **DONE 2026-08-03** as its own change, as anticipated —
  [[agent_authored_catalog_entries]]. It turned out to be the cheaper half of a
  feature rather than pure risk-reduction, and it retired the `grant: "explicit"`
  mark's load-bearing role for those two groups.
