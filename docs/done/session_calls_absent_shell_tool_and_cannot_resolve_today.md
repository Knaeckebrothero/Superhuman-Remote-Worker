# A session called `shell_execute` on a shell-less workspace tier — because the prompt advertised shell, and nothing told the agent what day it is

**Status:** **SHIPPED — committed `f36a9713`, pushed to `origin/develop`, k3d smoke PASSED 2026-08-06.** All three defects fixed with tests; the reordering and the date stamp were both exercised live on a real `virtual`-tier session.
**Found:** 2026-08-06 (user report: *"why did the agent try to use a shell tool if he doesn't even have access to it?"*), dev cluster session `c90f83b7-886b-43cb-b035-1773f6408a6a`.
**Severity:** Medium-high. Not silent — but it burned a whole turn, raised a supervised approval card for a tool that cannot run, and left the user's actual question unanswered.
**Component:** prompt assembly (`src/core/loader.py`, `config/prompts/*.txt`) · session tool loop (`src/persistent_graph.py`).
**Related:** `mcp_scholar_smoke_test_dispatch_ssh_overhead_and_stranded_deliverable.md` (§ request 34 — the same "tried unavailable `shell_execute` on the virtual workspace" three days earlier, recorded but not diagnosed) · `docs/done/todo_footer_false_tool_surface.md`-class defect: **the prompt claims a tool surface that does not match what is bound**.

---

## Summary

A supervised session on the `virtual` workspace tier was asked whether a steam
railway runs **today**. It ran two `web_search` calls, found the schedule
(Easter–end of October, Sundays and holidays only), then called `shell_execute`
— which is not bound on that tier — waited **53 seconds**, got back
`Tool 'shell_execute' not found`, and answered that it could not say whether the
train runs today.

The capability gate was working correctly. Three separate defects conspired:

1. the **prompt advertised shell unconditionally**, so the agent believed it had one;
2. **nothing injected the current date**, so it needed a shell to run `date`;
3. the **existence check ran after the permission gate**, so a supervised user was
   shown an approval card for a tool that could not run either way.

Only #2 changed the user-visible answer. #1 is why it reached for a shell at all,
and #3 is where the 53 seconds went.

## Symptom

Session `c90f83b7`, `session_base`, permission mode **supervised**, config override
`{"workspace": {"backend": "virtual"}}`.

| Time (UTC) | Event |
|---|---|
| 10:14:55 | human — *"Hat die Dampfbahn in Bad Orb heute geöffnet?"* |
| 10:15:13 | `web_search` — "Dampfbahn Bad Orb heute geöffnet Öffnungszeiten" |
| 10:15:51 | `web_search` — "Feldbahn Bad Orb …" → finds `dampfkleinbahn-bad-orb.de` |
| 10:16:16 | `shell_execute` |
| 10:17:09 | tool — `Tool 'shell_execute' not found` — **53 s later** |
| 10:17:17 | ai — season + "Sundays and holidays only", then: *"Ob sie heute fährt, hängt also davon ab, ob heute ein Sonntag oder Feiertag ist…"* |

It did the research correctly and then could not land it, because it did not know
what day it was.

## Root cause

### The gate was right — twice over

Shell was unbound by two independent mechanisms:

- `config/session_base.yaml` sets `tools.shell: []` — shell is opt-in at the
  expert layer, and this session ran plain `session_base`.
- The `virtual` tier's backend never implements `shell_run`, so
  `WorkspaceBackend.supports_shell` is `False` (`src/core/workspace_backend.py:689`)
  and `filter_tools_by_backend` (`src/tools/registry.py:332`, applied at
  `src/api/persistent_session.py:1566`) drops the whole `shell` /
  `browser_direct` / `git` categories.

Measured on the reproduction: **60 tools requested → 43 bound, zero shell.**

### Defect 1 — the prompt advertised shell unconditionally

`config/prompts/systemprompt_interactive.txt:61` — *"Use tools when they help…
file operations, **shell commands**, research, etc."* — and `:68` — *"For shell
commands: reuse existing **shell tabs**."* Identical text in every family variant
(`_gpt_5`, `_glm`, `_deepseek`). The prompt is a static file; the toolset is
computed at bind time from the backend. Nothing reconciled them.

The same divergence existed in the worker `tactical*.txt` prompts (whole
`Shell management` / `<shell_management>` sections), the `strategic*.txt`
`evidence_tool_call` example lists, and every `{% if cli_datasources %}` block —
which instructs the model to *"Use `run_command`"* and is therefore wrong
whenever no shell is bound.

**The tell:** "shell **tabs**" is *persistent-mode* vocabulary, and persistent
mode is precisely the mode whose tool is named `shell_execute`. The prompt did
not merely imply that a shell existed — it implied *which flavour*. That is why
the model emitted `shell_execute` rather than `run_command`. This was the prompt
being believed, not a hallucination.

### Defect 2 — nothing injected the current date (the real failure)

The question was about **heute**. The agent had the schedule and needed exactly
one more fact: what day it is. No prompt template and no code path on either the
session or worker side injected a date. So it reached for a shell to run `date`.

The shell call was a *symptom*. The user-visible defect is that a research
session could not answer any question containing "today".

### Defect 3 — the permission gate ran before the existence check

In `src/persistent_graph.py`, `announce_permission_batch` and `permission_check`
both preceded `tool_map.get(tool_name)`. A name that binds to nothing cannot run
whichever way the user answers, so in supervised mode the user was shown an
**approval card for a tool that does not exist** and the turn blocked on that
round-trip — the 53 seconds. The error was also a dead end: bare
`Tool 'X' not found`, no cause, no alternative, so the model's cheapest next move
would have been to retry.

## The fix (commit `f36a9713`)

**Date injection** — `loader.current_date_line()` / `with_current_date()` stamp
`Current date: YYYY-MM-DD (Weekday, UTC)`, wired into both branches of
`get_phase_system_prompt`; `persistent_graph` re-stamps it every turn.

Three deliberate choices:
- **Weekday included.** "Sundays and holidays only" is unanswerable from the
  calendar date alone.
- **Day granularity, not clock time.** The system message heads the provider
  prompt-cache prefix; a per-turn timestamp would invalidate that cache on every
  turn. A date changes at most once per session-day.
- **Rewritten in place, not appended.** The managed product-guide floor is
  deliberately last in the interactive prompt; the refresh must not displace it.
  `with_current_date` is idempotent and returns the original string unchanged
  when the date already matches.

The per-turn refresh is not optional: sessions here run for **weeks**
(`d67ee261` was 306 turns since 07-29), so a date baked in at setup silently
freezes on session-creation day.

**Prompt gating** — new `{% if has_shell %}` Jinja conditional
(`loader._has_shell_tools` + `render_instruction_content`), applied across
4 interactive + 7 tactical + 3 strategic + 10 datasource-CLI blocks. Also added a
standing rule to the interactive prompts: *"The tools listed in this request are
the only ones that exist here."*

`_has_shell_tools` reads `TOOL_REGISTRY`'s `category` rather than a hardcoded
name list, because the shell tools are mid-rename (one job toolset shared by
sessions/MCP/officers, no aliases) and a stale list would silently re-open the
gated blocks.

**Gate ordering** — `tool_map.get()` moved above the permission gate; the batch
announce filtered to bound names so a phantom never raises a card;
`_unavailable_tool_message` replaces the bare "not found":

> `'shell_execute'` is a shell tool, and shell tools are not available in this
> session — this workspace tier or configuration does not provide them. The tools
> listed in this request are the only ones that exist here. Do not retry this
> call: either accomplish the task with an available tool, or tell the user the
> capability is unavailable.

## Two traps that cost a cycle each

- **`srw_cloud_status` is `category: "shell"` but `grant: "code"`**, and is
  re-appended *after* `filter_tools_by_backend` whenever a cloud mount is active.
  A naive "any shell-category tool ⇒ has shell" check therefore re-opens the
  gated blocks on exactly the virtual-tier-with-cloud sessions the gate protects.
  `_has_shell_tools` excludes `grant: "code"` members for this reason.
- **`{% endif -%}` strips the whitespace that follows it**, so a blank line
  sitting after the tag is eaten when the block renders. Put the blank line
  *inside* the gated block. Caught by diffing rendered output against
  `git show HEAD:<file>`: with a shell bound, every gated template must render
  **byte-identically** to before — the gate has to be a pure no-op.

## Verification

**Unit** — `tests/test_prompt_shell_gating.py` (new) runs against the *shipped*
templates, both with and without shell; a synthetic template would not have
caught the original bug. Plus 3 date tests and 7 gate-ordering tests
(`TestUnboundToolSkipsThePermissionGate`). **6 of those 7 fail against pristine
HEAD** — verified in a throwaway worktree, so they catch the old behaviour rather
than describing it. Full suite 14154 passed / 12 failed, the 12 being the known
environment set (MCP servers, arxiv network, kubeconfig, and the Postgres connect
that only fails because `.env` supplies real credentials locally).

**k3d live smoke, 2026-08-06** — two `session_base` + `virtual` sessions,
gemma-4-26B, agent image `tilt-11f02f081d464081`. Pod logs confirm
`supports_shell=False`, 40 tools bound, zero shell.

- *Autonomous* (`c2075ea7`): "Welches Datum und welcher Wochentag ist heute?" →
  *"Heute ist Donnerstag, der 6. August 2026"*, **zero tool calls**. The audited
  reasoning trace says it outright: *"The current date provided in the system
  prompt is 2026-08-06 (Thursday, UTC)."* The live system prompt pulled from
  `llm_requests.request->messages->0` carries the date line and is clean on all
  seven shell probes.
- *Same session, pushed*: told to run `date` anyway, the model **did** emit
  `shell_execute` → `Unbound tool shell_execute called (40 tools bound) —
  rejecting before the permission gate`, the new message came back, and it
  recovered in **one turn without retrying**.
- *Supervised* (`a9808afc`): same push, and `thread_permission_requests` for that
  thread held **only `read_product_guide`** — no card was ever raised for the
  phantom. This is the one thing the autonomous run could not prove.

**Build hazard hit during the smoke:** Tilt's last build had landed *between* two
edits, so the agent image had `_CATEGORY_LABELS` used but not defined — a
`NameError` waiting to happen, behind a perfectly healthy-looking tag. Caught by
md5-summing image contents against the working tree. See
`reference_tilt_builds_partial_edits`: verify image *contents*, not the tag.

## Not done

- The worker path's tool-not-found handling (LangGraph `ToolNode` in
  `src/graph.py`) was not touched; this fix is the session loop only.
- `docs/issues/mcp_scholar_smoke_test_dispatch_ssh_overhead_and_stranded_deliverable.md`
  has other open findings (backend-dependent `search_files` semantics, the
  circular manifest condition); only its `shell_execute` observation is closed here.
