---
tags:
  - issue
  - cockpit
  - orchestrator
  - sessions
  - config-resolution
related:
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
  - "[[session_tool_group_enablement_is_computed_in_two_places]]"
  - "[[session_uploads_never_extract_archives]]"
---

# New Session tool toggles: no group can be enabled, two thirds cannot be disabled, and the Reasoning pick is silently discarded — the creation form kept the pre-`ce9222f9` re-enable path

**Status:** **RESOLVED 2026-08-03** — code committed on `develop`, **live gate
owed** (see the roadmap's task-8 report for the exact command). All three
defects are closed:

* **No group could be enabled** — the re-enable branch sourced member names
  from `defaultsTools()`, the very layer it was overriding, which ships `[]`
  for every category worth re-enabling. Both creation forms and the live pane
  now send a **policy** (`true`, or the enumeration the response serves for
  `shell`) and the write boundary expands it against the registry.
* **Two thirds could not be disabled** — closed server-side by task 7
  (`validate_tool_override_fragment` at every write boundary: reject, never
  drop).
* **Shell is settable** (D3/D4/D8) — off by default, `shell_tools` remains the
  gate, and a user without it sees *unavailable* with the server's reason
  rather than a silently unticked box.

The controls are now three-state (on / off / unavailable-with-a-reason) and
read `GET /api/persistent/threads/{id}/tool-groups` (live) or
`POST /api/persistent/tool-groups/preview` (creation), which are the agent's
own answer and a labelled forecast respectively.

*Original diagnosis, 2026-08-01:*
**Severity:** high, and one part **fails open** — unticking 8 of the 12 rendered
tool categories at session creation is silently discarded, so a user shown a
restriction may not have one. Separately, **sessions lost shell entirely on
2026-07-22** (see "The regression"), and the defects here are why it could not
be switched back on. Affects every session created from the New Session form.

> **Read this first.** An earlier revision of this document framed
> shell-in-sessions as a longstanding design policy ("opt-in at the expert
> layer") and the missing toggle as a gap that was never built. That was wrong.
> Persistent sessions shipped `run_command` + `shell_read` **by default** from
> 2026-03-31 to 2026-07-22. The policy comment was written *by the same commit
> that removed the capability*. The lesson: an inline comment asserting a design
> intent is not evidence of one — check when it was written.
**Component:** `cockpit/src/app/views/agent-settings/tools-group.component.ts`
(`getOverrides`), `cockpit/src/app/views/agent-settings/model-group.component.ts`
(`onSessionModelChange`, `prefillFromConfig`),
`orchestrator/main.py` (`_validated_session_tool_overrides` at the create
boundary), `src/core/session_tool_overrides.py`, `config/session_base.yaml`.

**Motivating incident:** dev session `1930dec9-181d-4fd5-a030-90b3d0b363d6`
("Ausbildungsbetriebe Garten- und Landschaftsbau"), 2026-08-01. The user
uploaded a `.zip`, the agent could not open it (see
`[[session_uploads_never_extract_archives]]`), and fell back to asking for
shell — which it did not have. The user had ticked **Shell** in the New Session
form and had selected **Reasoning: Max**. Neither reached the thread. The
persisted `config_override` is:

```json
{"llm": {"model": "gpt-5.6-sol"},
 "tools": {"workflows": [], "orchestrator": [], "agent_catalog": []},
 "workspace": {"backend": "sandbox"},
 "interactive": {"permission_mode": "autonomous"}}
```

The three groups the user **unticked** are recorded correctly. The group the
user **ticked** is absent, and so is `llm.reasoning_level`. The form can express
"off" and cannot express "on".

The agent then compounded it: it advised switching the workspace to "Container"
and enabling shell tools at session creation. `sandbox` **is** the container
tier (`src/core/loader.py:1426` maps the legacy name `container` → `sandbox`)
and `RemoteBackend.supports_shell` is `True` (`src/core/backends/remote.py:264`),
so the backend was never the blocker and following that advice would have
changed nothing.

---

## Part 1 — a ticked tool group produces no payload

### The chain

```
config/session_base.yaml:97                shell: []
  ↓
disabledToolCategoriesFromConfig()         tools-group.component.ts:26-38
  Array.isArray([]) && length === 0        → shell starts UNTICKED,
                                             and enters expertDisabledCategories
  ↓
user ticks it                              toggleCategory() removes it from
                                             disabledCategories → renders TICKED
  ↓
getOverrides() re-enable branch            tools-group.component.ts:422-427
  const defaults = this.mode() === 'live'
      ? SESSION_TOOL_GROUP_NAMES           ← live: hardcoded mirror, non-empty
      : this.defaultsTools();              ← creation: the base config itself
  if (!disabled.has(cat) && defaults[cat]?.length)   ← [] → length 0 → falsy
      tools[cat] = [...defaults[cat]];               ← NEVER RUNS
```

`defaultsTools()` is the API's `defaults_tools`, which the orchestrator fills
from `base.get("tools", {})` (`orchestrator/main.py:27549`) — the very
`session_base.yaml` map in which the group is `[]`. **The re-enable path sources
the tool names from the empty list it is trying to replace.** An empty source
cannot produce a payload, so ticking the box is a no-op.

### Unticking 8 of the 12 rendered categories is also a no-op

The reverse direction is only partly wired, and the failure is equally silent.
`getOverrides` writes `tools[cat] = []` for **every** unticked category
(`tools-group.component.ts:412-415`), but the create boundary copies across only
the four allowlisted groups. The form renders twelve:

| Disabling works | Disabling is silently discarded |
|---|---|
| `orchestrator`, `agent_catalog`, `workflows`, `canvas` | `research`, `browser_direct`, `citation`, `shell`, `communication`, `delegation`, `knowledge`, `git` |

So a session created with **Research** or **Browser** unticked still binds those
tools. The user is shown a restriction that was never applied — and unlike the
enable direction, this one fails *open*. Treat it as the more serious half.

The motivating incident only exercised the working column, which is why the
"off" direction has looked dependable: the three groups the user unticked
(`workflows`, `orchestrator`, `agent_catalog`) are all on the accept list.

Combined with Part 1, the honest summary of the creation form today is: **no
group can be enabled, and two thirds of the groups cannot be disabled.**

### Blast radius

Thirteen categories are empty in `config/session_base.yaml` and are therefore
un-enableable from the creation form:

```
shell, orchestrator, agent_catalog, workflows, core, graph, sql, mongodb,
email, repo, evaluation, delegation, communication
```

Seven are non-empty and behave correctly (`workspace, research,
browser_direct, citation, git, knowledge, canvas`) — which is why this has gone
unnoticed: every group a user routinely leaves alone works, and the bug only
bites when you try to turn something *on*.

### Why shell in particular cannot be fixed in the cockpit alone

There is a **second, independent** silent drop on the server. At create,
`config_override` is rebuilt server-side and only the allowlisted subset is
copied across:

```python
# orchestrator/main.py:22570-22573
req_tool_groups = _validated_session_tool_overrides(request_body.config_override)
if req_tool_groups:
    config_override.setdefault("tools", {}).update(req_tool_groups)
```

`SESSION_TOOL_OVERRIDE_NAMES` (`src/core/session_tool_overrides.py:20-59`) is
exactly `{orchestrator, agent_catalog, workflows, canvas}`. A `tools.shell`
fragment is ignored — no error, no warning. The runtime-update boundary does the
same thing more bluntly (`main.py:21468-21477`: `config_override["tools"] =
accepted_tools`, else `pop`).

And a **third** gate exists behind that: `shell_tools` is a deny-by-default
capability grant (`src/core/capability_grants.py:28`), validated at
`capability_grants.py:161-162` — `tools.shell requires the shell_tools grant`.

So enabling shell from the New Session form is blocked three times over, and all
three are silent. The form still renders the checkbox: `SESSION_TOOL_CATEGORIES`
includes `{key: 'shell', label: 'Shell', description: 'Ability to run shell
commands in a sandboxed terminal'}` (`agent-settings.types.ts:28`). The UI's own
grant gate (`CAT_TO_GRANT.shell = 'shell_tools'`,
`tools-group.component.ts:274-287`) only greys the row when the user *lacks* the
grant — an admin who *has* it sees a fully live, fully inert control.

### The regression — sessions used to have shell by default

The three gates above explain why the *checkbox* cannot turn shell on. They do
not explain why shell was missing in the first place. This does:

```diff
# config/persistent_defaults.yaml → config/session_base.yaml
# commit 57430a2a, 2026-07-22
-  shell:
-    - run_command
-    - shell_read
+  # Shell and application-control groups are opt-in at the expert layer.  Empty
+  # application groups are converted into runtime disable markers after the
+  # complete base/expert/project/request merge.
+  shell: []
```

Commit title: **`chore(config): remove unused default and persistent YAML
configuration files`**. Commit body, final sentence: **"No functional changes
introduced."**

Every persistent session bound `run_command` and `shell_read` by default from
2026-03-31 (`1535a627`, when `persistent_defaults.yaml` was introduced) until
2026-07-22. The rename to `session_base.yaml` dropped them. The change is not
mentioned in the commit message, and the message explicitly denies it.

**The "opt-in at the expert layer" comment is not prior policy.** It was written
on the same line, in the same commit, as the removal. Do not cite it as design
intent — it is a justification introduced simultaneously with the change it
justifies. (`config/interactive.yaml` does still set `tools.shell:
[run_command, shell_read]`, so an expert-layer route exists; it just was not the
established convention the comment implies.)

### The same commit dropped more than shell

`57430a2a` renamed both bases. Diffing the `tools` blocks across it:

| Base | Group | Before → after | Tools lost |
|---|---|---|---|
| `persistent_defaults.yaml` → `session_base.yaml` | `shell` | 2 → 0 | `run_command`, `shell_read` |
| `defaults.yaml` → `worker_base.yaml` | `shell` | 3 → 0 | `run_command`, `cancel_command`, `shell_read` |
| `defaults.yaml` → `worker_base.yaml` | `delegation` | 2 → 0 | `delegate_work`, `resume_delegation_child` |

**All six names are still live in `TOOL_REGISTRY`** — verified 2026-08-01 — so
none of this was stale-name cleanup. (There is precedent for that kind of
cleanup in the same era: `browse_website` / `download_from_website` really were
dead names, see `[[session_tool_group_checkbox_disagrees_with_the_agent]]` §4.
This is not that.)

No remaining YAML config requests delegation tools, and only
`config/interactive.yaml` requests shell. Since `load_tools` binds only what a
category's list explicitly names (`src/tools/registry.py:707` for delegation,
`:637` for shell), an empty list means nothing loads.

**Owed check — RESOLVED 2026-08-02.** DB-backed experts do *not* backfill
`tools.delegation` (both mirror their YAML files and declare no delegation
category). But worker jobs did **not** lose `spawn_subagent` — the critic,
developer and scholar configs each grant it directly. Only the heavy pair
`delegate_work` / `resume_delegation_child` became unreachable, and their five
references in `config/templates/instructions*.md` and
`config/skills/todo-guide/` are all `{% if has_tool(...) %}`-guarded, so they
degrade silently rather than failing. The cost is a dead instruction block, not
a broken capability. Full analysis in
`[[registered_tools_no_config_can_grant]]`.

Note also that base-granted shell bypasses the `shell_tools` capability grant,
since that validation applies to request overrides rather than the resolved
base. That was equally true before 2026-07-22, so restoring the list restores
that property too. Making the grant a real gate is a separate deliberate change
and should not be smuggled in as a side effect of this fix.

---

## Part 2 — why creation and live diverged

This is not one bad commit. It is a fix that was applied to one of two callers.

| Date | Commit / doc | What it did |
|---|---|---|
| 2026-07-14 | `21f55ab1` | Moved the session Reasoning select into the MODEL group. Made `ModelGroupComponent` the single writer of `llm.reasoning_level`, and **cleared the pick on model change and expert prefill** so a level cannot leak across families — explicitly "the stale `top_k` lesson". |
| 2026-07-16 | `docs/done/2026-07-16-live-session-settings.md` | Built the live settings pane. Because the live `config.update` boundary validates against the closed vocabulary, the pane renders **only** those four groups (`LIVE_TOOL_CATEGORIES`, `agent-settings.types.ts:50`); the others "would no-op as toggles" (`tools-group.component.ts:301-304`). Re-enable payloads use `SESSION_TOOL_GROUP_NAMES`, "a cockpit mirror of the closed vocabulary" (design doc, line 444). |
| 2026-07-22 | `57430a2a` | Config base rename — **and the regression**. `persistent_defaults.yaml` → `session_base.yaml` drops `shell: [run_command, shell_read]` to `shell: []`, adding the "opt-in at the expert layer" comment on the same line. Titled a chore; body claims "No functional changes introduced." |
| 2026-07-26 | `ce9222f9` | Fixed `[[session_tool_group_checkbox_disagrees_with_the_agent]]` — the live pane rendering groups as ticked that the agent did not have. |

The July-26 fix is the key one, because **its own root-cause section already
describes the bug we are now hitting** (`docs/done/…checkbox_disagrees…md`,
lines 72-75):

> A second-order effect: because the group's prefill baseline was "enabled", the
> re-enable branch in `tools-group.component.ts:411` could never produce a
> delta. **Turning the group on was unreachable from the UI**; only toggling it
> off then on worked, by writing an explicit list that beat the base `[]` at
> merge.

That fix worked by injecting a tool-group defaults layer under `threadOverride()`
in `liveConfig()`, and it notes "`tools-group.component.ts` needed no changes,
and the dead re-enable path started working." That is true **for live mode**,
because live mode had already been given a non-empty name source
(`SESSION_TOOL_GROUP_NAMES`) back on 2026-07-16.

Creation mode was never revisited, for an understandable reason: **the symptom
that motivated the fix does not appear there.** The creation form reads the
selected expert's real config, so `shell: []` renders as unticked — correctly.
Only the *second-order* half of that bug (the dead re-enable branch) is present,
and it was never carried over. The two modes diverge on exactly one line,
`tools-group.component.ts:422`.

So, to answer the three questions directly:

- **Why the different control sets?** Live shows four groups because that is
  literally all the live transport can carry; showing more would render toggles
  that no-op. Creation shows twelve because it writes a `config_override` at
  create time, where — in principle — more is expressible. That asymmetry is
  deliberate and defensible.
- **Why does one work and the other not?** Live's re-enable was given a
  hardcoded, always-non-empty name source when the closed vocabulary was
  introduced. Creation still derives names from the resolved base config, which
  is empty for precisely the groups a user would want to switch on.
- **Why was the creation form allowed to offer Shell at all?** Nobody re-checked
  `SESSION_TOOL_CATEGORIES` against `SESSION_TOOL_OVERRIDE_NAMES` after the
  closed vocabulary landed. The category list predates the allowlist and was
  never reconciled with it.

---

## Part 3 — the Reasoning pick is discarded, not mis-displayed

The live pane reporting "High (default)" is **accurate**: no
`llm.reasoning_level` exists on the thread. Nothing was dropped server-side —
`max` is accepted (`_SESSION_REASONING_LEVELS`, `orchestrator/main.py:3593`), the
`gpt-5.6` family declares `options: [low, medium, high, xhigh, max]` in
`config/model_config_matrix.yaml`, and `_clamp_reasoning_level`
(`src/core/loader.py:2939`) would have preserved it. (The "OpenAI is capped at
high" rule applies to the `gpt-5` family, not `gpt-5.6`.)

The cockpit never sent it. `sessionReasoning` is cleared by:

- `onSessionModelChange` → `sessionReasoning.set(null)` (`model-group.component.ts:518`)
- `prefillFromConfig` → `sessionReasoning.set(null)` (`model-group.component.ts:589`)

and `getOverrides` only emits when non-null (`:550`). Both clearers are
deliberate and were introduced for a real production defect (`21f55ab1` — stale
sampler params leaking across families and producing 400s). The defects are that
they are:

1. **Silent** — no toast, no marker; the select simply snaps back to the family
   default, which for `gpt-5.6-sol` is the visually similar "High".
2. **Unconditional** — cleared even when the new family's option set contains the
   picked level. Re-picking Max after a model change would have been valid.

Because Model sits above Reasoning in the form, going top-to-bottom is safe;
going *back* to the model row after picking a level silently discards it.

**Workaround that works today:** the live pane does track `llm.reasoning_level`
(`settings-pane.component.ts:35`), so setting Max there applies from the next
response.

---

## Proposed fixes

Ordered by value. Not started — none of this is implemented.

0. **Restore `tools.shell: [run_command, shell_read]` in
   `config/session_base.yaml`.** One line, reverts the 2026-07-22 regression,
   and returns every new session to the behaviour that shipped for the previous
   four months. Independent of everything below — no cockpit or allowlist work
   is needed for it. Do this first and separately, so the revert is not tangled
   up with the toggle redesign.

1. **Stop the creation form from sourcing re-enable names from a possibly empty
   list.** Either use the same closed-vocabulary mirror live uses, or send a
   sentinel meaning "enable this group" and let the server expand it from
   `TOOL_REGISTRY`. The server-side expansion is the better shape: it kills the
   cockpit's duplicate name lists entirely and removes the drift test's reason
   to exist.
2. **Reconcile `SESSION_TOOL_CATEGORIES` with `SESSION_TOOL_OVERRIDE_NAMES`.**
   Whatever the creation surface renders must be something the create boundary
   accepts. A test asserting `SESSION_TOOL_CATEGORIES ⊆ SESSION_TOOL_OVERRIDE_NAMES ∪ {non-empty base groups}`
   would have caught this the day the allowlist landed.
3. **Decide whether shell should also be a per-session toggle.** Item 0 restores
   it as a default; this is the separate question of whether the checkbox should
   work. If yes, add `shell` to `SESSION_TOOL_OVERRIDE_NAMES` and let the
   `shell_tools` grant be the real gate, surfacing a denial instead of a silent
   drop. If no, remove the Shell row from the creation form so it stops
   asserting a control it does not have.
4. **Make the reasoning clearers conditional and visible.** Keep the pick when
   the new family's option set contains it; when it genuinely must be dropped,
   say so.
5. **Make the silent drops loud.** `_validated_session_tool_overrides` discards
   unknown groups without a word at both boundaries. At minimum log the
   discarded keys; better, 400 on a group the surface should never have sent —
   the same "garbage fails loud here instead of being silently dropped" reasoning
   already applied to `_validated_reasoning_level` (`main.py:3591-3593`).

## Verification owed

- Unit: ticking a group whose base list is `[]` produces a non-empty
  `tools.<group>` payload in creation mode.
- Unit: the category-list ⊆ allowlist assertion in item 2.
- Unit: a reasoning pick survives a model change within the same family's option
  set, and is dropped with a signal when it is not.
- Regression test pinning the tool lists both bases must contain, so a config
  refactor cannot silently empty a category again. This is the test that would
  have caught `57430a2a`; `tests/test_config_tool_names_are_registered.py`
  checks the inverse direction (every named tool exists) and passes happily on
  an empty list.
- Live gate on dev: create a session with the target group ticked, then confirm
  the agent's bound tool count from the pod log
  (`Loaded N tools for persistent session`) — the same evidence that settled
  `[[session_tool_group_checkbox_disagrees_with_the_agent]]`. The checkbox state
  is not evidence; that is the whole lesson of this bug class.
