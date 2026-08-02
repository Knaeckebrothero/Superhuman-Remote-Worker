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

# New Session tool toggles can be switched off but never on, and the Reasoning pick is silently discarded — the creation form kept the pre-`ce9222f9` re-enable path

**Status:** OPEN, diagnosed 2026-08-01. Not started.
**Severity:** medium-high — no data loss, but two settings the user demonstrably
set were silently dropped, and the resulting session then told the user to go
change a setting that cannot be changed. Affects every session created from the
New Session form.
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

### This contradicts a written policy

`config/session_base.yaml:94-96`, added in `57430a2a` (2026-07-22):

> Shell and application-control groups are **opt-in at the expert layer**. Empty
> application groups are converted into runtime disable markers after the
> complete base/expert/project/request merge.

The sanctioned path is an expert config that sets `tools.shell` — e.g.
`config/interactive.yaml` (`$extends: session_base`, `tools.shell:
[run_command, shell_read]`). The creation form offers a control that the
architecture deliberately does not support.

---

## Part 2 — why creation and live diverged

This is not one bad commit. It is a fix that was applied to one of two callers.

| Date | Commit / doc | What it did |
|---|---|---|
| 2026-07-14 | `21f55ab1` | Moved the session Reasoning select into the MODEL group. Made `ModelGroupComponent` the single writer of `llm.reasoning_level`, and **cleared the pick on model change and expert prefill** so a level cannot leak across families — explicitly "the stale `top_k` lesson". |
| 2026-07-16 | `docs/done/2026-07-16-live-session-settings.md` | Built the live settings pane. Because the live `config.update` boundary validates against the closed vocabulary, the pane renders **only** those four groups (`LIVE_TOOL_CATEGORIES`, `agent-settings.types.ts:50`); the others "would no-op as toggles" (`tools-group.component.ts:301-304`). Re-enable payloads use `SESSION_TOOL_GROUP_NAMES`, "a cockpit mirror of the closed vocabulary" (design doc, line 444). |
| 2026-07-22 | `57430a2a` | Config base rename. `session_base.yaml` gains `shell: []` and the "opt-in at the expert layer" policy comment. |
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
3. **Decide shell policy explicitly** (product call, gates 1 and 2):
   - *Intentionally expert-layer-only* → remove the Shell row from the creation
     form. Cheapest, matches the `session_base.yaml` comment, and the
     `interactive` expert remains the supported route.
   - *Should be selectable* → add `shell` to `SESSION_TOOL_OVERRIDE_NAMES`, keep
     the `shell_tools` grant check as the real gate, and surface a denial
     message instead of a silent drop.
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
- Live gate on dev: create a session with the target group ticked, then confirm
  the agent's bound tool count from the pod log
  (`Loaded N tools for persistent session`) — the same evidence that settled
  `[[session_tool_group_checkbox_disagrees_with_the_agent]]`. The checkbox state
  is not evidence; that is the whole lesson of this bug class.
