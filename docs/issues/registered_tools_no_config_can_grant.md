---
tags:
  - issue
  - agent
  - tooling
  - config-resolution
  - experts
  - knowledge
related:
  - "[[session_create_tool_toggles_cannot_enable_a_group]]"
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
  - "[[expert_prompts_instruct_a_removed_browser_tool]]"
  - "[[stale_tool_names_degrade_every_worker_job_tool_load]]"
  - "[[application_tool_surface_baseline]]"
  - "[[centurion]]"
---

# Ten registered tools have no reachable grant path — and five curation prompts unconditionally order the curator to call two of them

**Status:** OPEN, diagnosed 2026-08-02 — the *invisible* half is fixed, the ten
tools are unchanged.

**Shipped** 2026-08-02/03 on `develop` (not pushed;
[[tool_configuration_defects_and_fix_roadmap]] is the register), all of it
consuming this inventory rather than acting on it:

- **§2 is machine-readable.** Every injection site config cannot revoke now
  carries `grant: "code"` plus this document's own `gate` string in
  `TOOL_REGISTRY`; `CODE_GRANTED_CATEGORIES` classifies the eight whole-category
  cases. The three marker-gated lists (14 `orchestrator`, 5 `agent_catalog`, 7
  `workflows`) are deliberately *unmarked*, because unticking those categories
  really does drop them.
- **§5's two field-less categories are stated rules, not accidents.**
  `product_help` and `session_task` are recorded as code grants, so
  `tools.product_help: [...]` being discarded is now something the system says
  rather than something it does silently.
- **Both are served to the user.** `GET .../tool-groups` reports, per category,
  that the runtime decided it and which gate — and after this review round, per
  *tool* too (`code_granted_tools()`), which is what makes `srw_cloud_status`,
  `sleep`, `notify_user` and `request_workspace_upgrade` visible instead of
  showing as an ordinary ticked checkbox nobody can untick.
- **Item 3's premise is void.** The six `*_bundle` tools got `grant: "explicit"`,
  so a config *can* name them (an in-category explicit name is accepted at every
  write boundary; `tests/test_tool_override_boundary.py::TestNoModelAuthoredPathReachesSessionCreate`
  pins why that is bounded). They are no longer ungrantable — only ungranted.
  The §3c verification is still owed, but the question narrowed: the reachable
  path is now a *documented* one.

**Not shipped, and what keeps this issue open:** items 1, 2 and 5 — the curator
still cannot call the `kb_lint`/`kb_index` that five of its prompts order, the
`delegate_work` decision is still unrecorded (and
`config/experts/developer/config.yaml`'s "former broad base" comment still reads
as history), and there is no prompt lint. Item 4's reachability test was **not**
added: `tests/test_config_tool_names_are_registered.py` still checks one
direction only. The implementing series held "no shipped config's grants change"
as an acceptance property, so every count and every row in §3 stands as written.

**Severity:** medium. Nothing crashes and nothing fails open. Two distinct
harms: (a) the curator is told, in every model-family variant of its prompt, to
run `kb_lint` and `kb_index`, which it cannot hold — the same class as
`[[expert_prompts_instruct_a_removed_browser_tool]]`; (b) `delegate_work` /
`resume_delegation_child` are a *capability regression* — worker jobs held them
by default until 2026-07-22 and no config has granted them since.
**Component:** `config/worker_base.yaml`, `config/session_base.yaml`,
`config/experts/curator/config.yaml`, `config/prompts/curation_prompt*.txt`,
`src/core/session_tool_overrides.py`, `src/tools/orchestrator/catalog.py`,
`src/tools/orchestrator/workflows.py`, `src/tools/knowledge/knowledge_tools.py`,
`src/tools/delegation/delegate_work.py`.

**Motivating question:** whether `TOOL_REGISTRY` contains tools that no
resolvable config path can grant, making them dead weight in the registry and —
worse — dead references in prompt text. It does: **10 of 151**.

> **Read this first — two premises that turned out to be wrong.** The
> investigation opened with a suspect list of `sleep`, `notify_user`,
> `request_workspace_upgrade`, `srw_cloud_status`, `kb_index` and `kb_lint`,
> derived by diffing `TOOL_REGISTRY` against the twelve YAML files that declare
> `tools:`. **YAML is not the only grant layer.** Four of those six are injected
> at runtime by code that never appears in any config, and one of them
> (`sleep`) is demonstrably in live use right now. Only the two `kb_*` names
> survived. A YAML-only diff over-reports by a factor of six; the real answer
> needs the runtime injection sites enumerated too — see §2.

---

## 1. Method

Reachability was computed, not eyeballed. A tool is **reachable** if any of
these can put its name into the list passed to `load_tools`
(`src/tools/registry.py:356`):

1. a bundled YAML config, in its `$extends`-merged form;
2. a DB-backed expert row;
3. a datasource-derived category override;
4. a runtime injection site in the session or worker tool-assembly code;
5. an orchestrator-stamped `config_override.tools.*` at dispatch.

The registry snapshot: **151 tools across 24 categories**, confirmed by
importing `src.tools.registry` (the premise's numbers are correct). Note the
category set is not the config category set — see §5.

### Merge semantics: lists REPLACE, they do not merge

This is load-bearing for every "expert X omits tool Y" claim, so it was read
rather than assumed. `deep_merge` (`src/core/loader.py:178-215`) recurses into
dicts but at `:211-213` does:

```python
else:
    # Arrays and scalars: override replaces
    result[key] = value
```

`load_and_merge_config` applies it with the parent as base and the child as
override (`:274`). So `tools` (a dict) merges **by category key**, and each
`tools.<category>` (a list) is **replaced wholesale** by a child that declares
it. A category a child does *not* declare is inherited intact.

Independent confirmation from a second author, in a comment that predates this
investigation and describes the behaviour it is coding around
(`orchestrator/main.py:12943-12945`):

> Each tool group is spelled out explicitly because `deep_merge` replaces lists
> but merges dicts by key — an omitted group is INHERITED, not empty.

Consequence for the premise's centurion argument: `config/experts/centurion/config.yaml`
does not declare `core`, so it inherits `session_base.yaml`'s `core: []` — the
reasoning was right. The conclusion drawn from it was still wrong, because
`sleep` never came from YAML. See §4.

---

## 2. The runtime grant layer, which no config shows

`src/api/persistent_session.py:1408-1557` (`_load_tools_for_backend`) starts
from `get_all_tool_names(config)` and then **appends names that appear in no
YAML file anywhere**. `src/agent.py:3052-3068` does a smaller version of the
same for worker jobs. This is the layer a config-only audit misses:

| Tool(s) | Injected at | Gate |
|---|---|---|
| `task_add`, `task_complete`, `task_list` | `persistent_session.py:1414-1417` | unconditional in sessions |
| `read_product_guide` | `:1426-1431` | unless app-guide break-glass |
| `get_product_capabilities` | `:1441-1447` | `PRODUCT_CAPABILITIES_TOOL_ENABLED` |
| 14 `orchestrator` names | `:1470-1488` | Fleet Management enabled |
| 5 `agent_catalog` names | `:1495-1504` | Experts & Skills enabled |
| 7 `workflows` names | `:1509-1520` | Automations & Loops enabled |
| `srw_cloud_status` | `:1525-1527` | cloud mount active |
| `checkout_project_repository` | `:1540-1541` | fleet mgmt **and** `supports_shell` |
| `request_workspace_upgrade` | `:1547-1548`, and `agent.py:3066-3068` for workers | lite tier (`not supports_shell`) |
| `sleep`, `notify_user` | `:1554-1557` | `config.officer.enabled is True` |

Three more grant paths live outside the agent process:

- **Datasource-derived categories.** `datasource_tool_categories`
  (`src/core/datasource_setup.py:160-211`) maps an attached datasource type to a
  whole category list from `DATASOURCE_TOOL_MAP` (`:93-142`), and the result is
  written straight onto `config.tools.<category>`
  (`persistent_session.py:1818-1819`, `persistent_app.py:1645-1650`,
  `orchestrator/main.py:17199-17210`). This covers **every** `graph`, `sql`,
  `mongodb`, `webdav`, `repo` and `email` tool — 28 names that appear in no
  config's tool list, and which the bases deliberately ship as `[]` with a
  comment saying so.
- **Verification critics.** `_critic_config_override`
  (`orchestrator/main.py:12959-12965`, mirrored at
  `src/api/orchestrator_client.py:1756-1758`) stamps
  `tools.evaluation: [approve_job, return_job_with_feedback]` onto every critic
  job.
- **Planner loops.** `orchestrator/services/project_loops.py:898` stamps
  `config_override["tools"] = {"loop": ["loop_plan"]}` onto a checkpoint critic.

Also relevant, in the other direction: `filter_tools_by_backend`
(`src/tools/registry.py:216-269`) runs **after** all of the above
(`persistent_session.py:1564-1566`) and drops the `shell`, `browser_direct` and
`git` categories when `backend.supports_shell` is false. It removes; it never
adds, so it cannot make an unreachable tool reachable. One live interaction
worth knowing: `srw_cloud_status` is category `shell`
(`src/tools/shell/shell_tools.py:252`), so a lite-tier session with an active
cloud mount injects it at `:1526` and then loses it at `:1564`.

### DB-backed experts

Enumerated live on dev via `list_experts`: **11 entries — 9 answered by slug
(bundled) and 2 by UUID (DB rows)**: `Assistant`
(`05525e7b-5c0a-4e59-9333-d2cb870030be`) and `General Worker`
(`6a3ba4b5-0bf8-4a17-87c7-3168c0cf87e7`). Eleven expert directories exist under
`config/experts/`; those two are the ones the catalog serves from the DB. Both
DB rows were fetched in full. Their tool blocks mirror
`config/experts/assistant/config.yaml` and
`config/experts/general-worker/config.yaml` exactly; **neither grants any tool
that the bundled configs do not**. `query_table` cannot reach the `experts`
table (its allowlist is 7 tables: agents, datasources, jobs, project_members,
project_repositories, projects, users), so the enumeration rests on the
`list_experts` API rather than a raw row dump.

This also answers the **owed check** left open in
`[[session_create_tool_toggles_cannot_enable_a_group]]` ("whether DB-backed
experts backfill `tools.delegation` for worker jobs"): **they do not.** Neither
DB expert declares a `delegation` category at all — `General Worker`'s own
description states it ships with "no shell, delegation, scholar pre-job, critic
verification, automations, or loops unless an administrator or copied expert
explicitly enables them". Worker jobs did **not** lose `spawn_subagent`, which
`critic`, `developer` and `scholar` each grant directly; they lost only the
heavy `delegate_work` pair.

---

## 3. The result — 10 unreachable tools

> **Now 8, as of 2026-08-02.** `kb_index` and `kb_lint` were granted to the
> curator (commit `19b3a7cf`), closing §3a. The live unreachable set is the six
> `*_bundle` tools plus `delegate_work` / `resume_delegation_child`. The table
> below is kept as the original finding; §3a is resolved, §3b and §3c are open.

Registry minus every layer in §2:

| Tool | Category | Since | Kind |
|---|---|---|---|
| `kb_index` | `knowledge` | 2026-07-03 (`d0125805`) | drift — never granted |
| `kb_lint` | `knowledge` | 2026-07-03 (`d0125805`) | drift — never granted |
| `delegate_work` | `delegation` | 2026-07-22 (`57430a2a`) | **regression** |
| `resume_delegation_child` | `delegation` | 2026-07-22 (`57430a2a`) | **regression** |
| `get_expert_bundle` | `agent_catalog` | 2026-07-09 (`75eb94b2`) | drift — never granted |
| `set_expert_bundle` | `agent_catalog` | 2026-07-09 (`75eb94b2`) | drift — never granted |
| `get_skill_bundle` | `agent_catalog` | 2026-07-09 (`75eb94b2`) | drift — never granted |
| `set_skill_bundle` | `agent_catalog` | 2026-07-09 (`75eb94b2`) | drift — never granted |
| `get_automation_bundle` | `workflows` | 2026-07-09 (`c6f030da`) | drift — never granted |
| `set_automation_bundle` | `workflows` | 2026-07-09 (`c6f030da`) | drift — never granted |

All ten are fully implemented, non-placeholder, and would load if named:
`create_catalog_tools` returns all four expert/skill bundle tools
(`src/tools/orchestrator/catalog.py:1049-1058`), `create_workflow_tools` returns
both automation bundle tools (`src/tools/orchestrator/workflows.py:856-865`),
and both `kb_*` gardener tools are wired through
`src/tools/knowledge/knowledge_tools.py:2011` / `:2097`. None of this is
stale-name residue — the failure mode is the *inverse* of
`[[stale_tool_names_degrade_every_worker_job_tool_load]]`.

### 3a. `kb_index` / `kb_lint` — added to the registry, never to a config

Added by `d0125805` (2026-07-03, "feat(kb): add OKF gardening tools"). Grep over
the full history of `config/` for either name returns exactly one commit —
`344f68a4` (2026-07-05), which added them to the *curation prompts*, not to a
tool list. They have never appeared in a `tools:` block.

Both bases and the curator grant the same ten of the twelve `knowledge` tools
(`kb_write`, `kb_update`, `kb_read`, `kb_list`, `kb_search`, `kb_related`,
`kb_contradictions`, `kb_provenance`, `kb_unanswered`, `kb_export` —
`config/worker_base.yaml:161-171`, `config/session_base.yaml:104-114`,
`config/experts/curator/config.yaml:42-51`). The
curator, the one expert whose entire job is gardening the KB, drops `kb_export`
and adds nothing, so it holds nine.

**This is the prompt half, and it is unhedged.** All five model-family variants
of the curation prompt order the tool calls outright:

| File | Line | Text |
|---|---|---|
| `config/prompts/curation_prompt.txt` | 19 | "run `kb_lint` and act on what it reports … Finish with `kb_index` to regenerate the index." |
| `config/prompts/curation_prompt_gemma.txt` | 19 | identical |
| `config/prompts/curation_prompt_deepseek.txt` | 2, 23 | "Act through the knowledge-base tools (kb_search, kb_write, kb_update, kb_lint, kb_index)" |
| `config/prompts/curation_prompt_gpt_oss.txt` | 9 | "run `kb_lint` … Then run `kb_index` to refresh the index." |
| `config/prompts/curation_prompt_gpt_5.txt` | 14-15, 29 | lists both with full signatures in a "your tools" block |

`curation_prompt.txt:69` and `_gpt_5.txt:94` additionally cite `kb_lint`'s
oversized-note threshold as a rule the model should write to. Unlike the
`browse_website` case, there is no `{% if has_tool(...) %}` guard and no "if
available" hedge anywhere — step 5 of the curation procedure is a mandatory call
to a tool the curator does not hold. The `index.md` those prompts promise to
regenerate is therefore never regenerated by an agent.

### 3b. `delegate_work` / `resume_delegation_child` — a regression, and the comment is not policy

`config/defaults.yaml` (pre-`57430a2a`) granted:

```yaml
  delegation:
    - delegate_work
    - resume_delegation_child
```

`57430a2a` (2026-07-22, "chore(config): remove unused default and persistent
YAML configuration files", body ending "No functional changes introduced")
renamed it to `config/worker_base.yaml` with `delegation: []`. This is the same
commit and the same denial already documented for `shell` in
`[[session_create_tool_toggles_cannot_enable_a_group]]`; this doc adds that the
delegation row has now been chased to its conclusion — **nothing granted it back**.

The one place in the tree that reads like prior policy is
`config/experts/developer/config.yaml:75-76`:

```yaml
  # Light subagent delegation via spawn_subagent (replaces the inherited heavy
  # delegate_work / resume_delegation_child grant from the former broad base).
```

`git blame` splits that two-line comment across two commits. Line 75 is from
`ebc6e4ba` (2026-07-03) and originally read "…from **defaults**". **Line 76 was
rewritten by `57430a2a` itself**, on the same day it emptied the base. The
phrase "the former broad base" is a post-hoc gloss written by the removal, not a
standing convention — the identical trap flagged in the sibling doc. Treat it as
evidence of what `57430a2a` did, never as evidence of what was intended.

What survives is a real design story for `spawn_subagent` (light, in-process,
granted by `critic`, `developer`, `scholar`) but **no story at all** for the
heavy path: `delegate_work` remains registered, documented, and referenced by
five instruction templates.

Those references are, to the templates' credit, all guarded:
`{% if has_tool("delegate_work") %}` in `config/templates/instructions.md:51`,
`instructions_gemma.md:66`, `instructions_gpt_oss.md:39`,
`instructions_minimax.md:57`, `instructions_minimax_m3.md:78`, and
`config/skills/todo-guide/SKILL.md:64`;
`config/skills/todo-guide/references/phase-patterns.md:9,61` hedges in prose
("applies only if you have the `delegate_work` tool"). `render_instruction_content`
(`src/core/loader.py:939-985`) evaluates `has_tool` against the *actually loaded*
tool names, so these degrade silently and correctly. **No agent is currently
being told to call `delegate_work` when it cannot.** The cost is different: a
whole guarded section of the standard worker instructions has been dead since
2026-07-22 and nobody noticed.

### 3c. The six `*_bundle` tools — designed off-by-default, with no way to turn them on

`75eb94b2` (2026-07-09) and `c6f030da` (2026-07-09) added them. `75eb94b2`'s own
body says: *"Ensured new tools are excluded from default persistent sessions
unless explicitly configured."* `[[application_tool_surface_baseline]]` calls
them "explicit-grant … not auto-injected into existing sessions" (lines 504-506,
541-543, 611-613).

Off-by-default is the intent, and it is honoured. The gap is that **the
"explicitly configured" escape hatch was never built**:

- No bundled or DB expert names them (§2). Grep over the full history of
  `config/` for all six returns zero commits.
- The session grant boundary rejects them by name.
  `SESSION_TOOL_OVERRIDE_NAMES` (`src/core/session_tool_overrides.py:20-60`)
  allows exactly 5 `agent_catalog` names and 7 `workflows` names; a request
  naming `set_expert_bundle` raises `SessionToolOverrideError` at
  `:96-99`. Both the create boundary and the live `config.update` boundary use
  it (`orchestrator/main.py:3700`, `:21473`).
- The cockpit mirrors the same closed vocabulary
  (`cockpit/src/app/views/agent-settings/agent-settings.types.ts:62-82`), so no
  UI surface can express the grant either.

The residual path is a hand-authored expert config: `_validate_expert_fragment`
(`orchestrator/main.py:27905-27915`) only runs `hard_deny_scan` for credential
sections and does **not** validate tool names, so an admin could POST an expert
with `tools.agent_catalog: [..., set_expert_bundle]` and the session gate
(`persistent_session.py:1490-1493`, which only drops the category when the list
is *empty*) would let it through. **This was reasoned from code, not exercised.**
No shipped config, no DB expert, and no UI does it today, so the tools are
unreachable in practice.

---

## 4. Centurion verdict — `sleep` works, and not through the config

The premise reasoned: `centurion/config.yaml` `$extends: session_base`, does not
declare `core`, `session_base` ships `core: []`, lists replace → centurion has
no `sleep`. Every step of that is correct, and the conclusion is still false.

`sleep` and `notify_user` are appended at
`src/api/persistent_session.py:1554-1557`, gated on
`config.officer.enabled is True`:

```python
if getattr(getattr(self.config, "officer", None), "enabled", False) is True:
    for officer_tool in ("sleep", "notify_user"):
        if officer_tool not in tool_names:
            tool_names.append(officer_tool)
```

The centurion config says so itself, and this comment *is* trustworthy because
it describes code that exists: "sleep + notify_user arrive via the officer flag,
not this list." Both tools are category `core` but exempt from the
workspace-manager requirement in `load_tools`
(`src/tools/registry.py:425-444`, via `OFFICER_TOOLS_METADATA`), which is what
lets them load on the `none` lite tier the officer runs — as
`src/tools/core/officer.py:17-19` documents.

**Live evidence beats all of it.** Dev thread
`d67ee261-334a-4315-ab7f-b1e0e7ba8765` ("Centurion — Better Resavio", 258 turns,
`status=active`, 1365 messages) has been calling `sleep` successfully since
2026-07-30 and was still doing so at **2026-08-02 09:15 UTC**:

```
[258] ai  (2026-08-02T09:15:31Z)  Tools: sleep
[258] tool                        Wake-up call filed for ~30 minutes
                                  (bounds applied server-side). This turn ends
                                  now; events will wake you earlier.
```

Earlier in the same thread (turns 43-49, 2026-07-30) the wakes arrive as
orchestrator `[SITREP]` events with `timer: slept ~10 min` reasons — the durable
`timer` outbox row described in `[[centurion]]` §4. So the whole documented
wake/sleep cycle functions: the tool files the wake, the orchestrator fires it.

One observation, deliberately **not** filed as a defect here: since 2026-08-01
the wakes on that thread arrive as `[backstop wake] The orchestrator's durable
timer did not fire in time`, at ~2h intervals rather than the requested 30 min.
That thread carries a standing
`officer.hold = {kind: "maintenance", note: "Held by the Legate for the
project_officers migration"}` in its `config_override`, and `[[centurion]]` §4
specifies that a hold makes the drain defer *every* wake path including timers,
with the agent-local backstop as the residual. The observed behaviour matches
the documented hold semantics. It was not separately verified that the timer
path is healthy for an unheld officer.

---

## 5. Two registry categories that no config can name at all

Not a defect, but it explains part of the YAML-diff over-report and is worth
pinning. `ToolsConfig` (`src/core/loader.py:1458-1491`) has **23** fields and
`get_all_tool_names` (`:4473-4497`) iterates the same 23. The registry's static
category set is **24**. The difference:

- `product_help` (`read_product_guide`, `get_product_capabilities`) and
  `session_task` (`task_add`, `task_complete`, `task_list`) exist in the
  registry but have **no `ToolsConfig` field**. `_parse_agent_config`
  (`:2356-2381`) has no `tools_data.get("product_help", ...)` line, so
  `tools.product_help: [...]` in a YAML file is silently discarded. Both
  categories reach agents exclusively through the §2 injection sites.
- `mcp` is the reverse: a `ToolsConfig` field with no static registry entries.
  `register_mcp_tools` (`src/tools/registry.py:139-169`) populates it at runtime
  and `expand_tool_wildcards` (`:172-184`) expands the `"*"` sentinel.

---

## Proposed fix

Ordered by value. Not started — none of this is implemented.

1. **Grant `kb_lint` + `kb_index` to the curator.** Two lines in
   `config/experts/curator/config.yaml`'s `knowledge` list. This is the only
   item with a live behavioural harm today and the only one where prompt and
   config already disagree. Do it first and alone. (Adding them to
   `worker_base.yaml` / `session_base.yaml` too is defensible — both `kb_*`
   tools need a workspace backend to read notes,
   `knowledge_tools.py:2031` — but the curator is the caller the prompts name.)
2. **Decide the `delegate_work` question explicitly, and record the decision.**
   Either restore `delegation: [delegate_work, resume_delegation_child]` to
   `config/worker_base.yaml` (reverting the undeclared half of `57430a2a`), or
   retire the heavy path: unregister both tools, delete the five
   `{% if has_tool("delegate_work") %}` blocks and the `todo-guide`
   references, and say so in `[[subagent_delegation]]`. What must not persist is
   the current state, where the tool, its docs and its guarded prompt text all
   survive a removal nobody wrote down. Pair the fix with a correction to
   `config/experts/developer/config.yaml:76` — the "former broad base" comment
   is `57430a2a`'s self-justification and should not be left reading as history.
3. **Give the `*_bundle` tools a grant surface, or drop them.** They were shipped
   as "explicit-grant"; no surface can express the grant. Either add an
   authoring group to `SESSION_TOOL_OVERRIDE_NAMES` (with its own capability
   grant — `set_expert_bundle` and `set_automation_bundle` are write tools on
   the control plane, so this is a real permissions decision, not a checkbox),
   or `unregister_tool` all six and keep the HTTP endpoints as the authoring
   path. Note the interaction with
   `[[session_create_tool_toggles_cannot_enable_a_group]]` item 2: whatever
   lands here must satisfy the category-list ⊆ allowlist assertion proposed
   there.
4. **Add the reachability test.** `tests/test_config_tool_names_are_registered.py`
   checks one direction — every configured name exists in the registry. The
   inverse has no guard, which is why three separate features drifted into the
   registry unreachable. The test is cheap: assert
   `set(TOOL_REGISTRY) - reachable == EXPECTED_UNREACHABLE`, where `reachable`
   is the union of the merged bundled configs, `DATASOURCE_TOOL_MAP`,
   `SESSION_TOOL_OVERRIDE_NAMES`, and an explicit list of the runtime injection
   names from §2. `EXPECTED_UNREACHABLE` starts as whatever items 1-3 leave
   behind and is a deliberate, reviewed allowlist. It fails the day someone
   registers a tool without wiring a grant.
5. **Lint prompts against the granting expert's merged toolset.** The real fix
   for the class, already proposed and still unbuilt in
   `[[expert_prompts_instruct_a_removed_browser_tool]]`: grep prompt and
   instruction files for known tool names and assert each is either in that
   expert's merged toolset or inside a `has_tool` guard. The `browse_website`
   case was "prompt names a tool that no longer exists"; this one is "prompt
   names a tool that was never granted". One lint catches both.

## Verification owed

- **The §3c residual path.** It has *not* been shown that a hand-authored
  expert with `tools.agent_catalog: [set_expert_bundle]` actually reaches
  `load_tools`. Create one on dev, attach a session, and read the bound tool
  names. If it loads, item 3 is a live privilege-escalation surface for any
  expert author, not just a dead-tool cleanup, and its priority goes up.
- **Project-scoped experts.** `list_project_experts` reads expert configs from a
  project's Gitea repo, a layer distinct from both bundled YAML and the
  `experts` table. No project (of 53) was surveyed for a config granting any of
  the ten. The mechanism is user-authorable, so it can in principle grant them;
  whether any does is unknown.
- **Curator live gate after item 1.** Run a curation subjob and confirm from the
  pod log that the bound toolset includes `kb_lint`/`kb_index`, then confirm
  `knowledge/index.md` is actually regenerated. The tool count in the log is the
  evidence; the config diff is not — that is the lesson of
  `[[session_tool_group_checkbox_disagrees_with_the_agent]]`.
- **Unheld officer timer.** Whether the orchestrator's durable timer fires for an
  officer thread with no standing `officer.hold`. The one live officer is held,
  so the `[backstop wake]` observations in §4 cannot distinguish "hold working as
  designed" from "timer path degraded". Out of scope here; check it before
  trusting officer wake latency.
