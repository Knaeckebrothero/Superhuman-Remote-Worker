---
tags:
  - issue
  - fix-spec
  - agent
  - session
  - tools
  - config
---

# A live `config.update` buries `config.extra`, and the shell group silently binds only `shell_read`

**Filed:** 2026-08-11, found while investigating why a session that had just been
granted the shell tool group still reported it could not run commands.
**Status:** **Fixes 1 and 2 IMPLEMENTED and k3d-gated 2026-08-11** (uncommitted
at time of writing). Mechanism was CONFIRMED in code and reproduced from live dev
evidence (session `1930dec9-181d-4fd5-a030-90b3d0b363d6`, agent pod
`srw-agent-j-0878d2e7`, 2026-08-10 08:40:04Z). Items 3, 4 and 5 remain OPEN —
see [Verification](#verification) for exactly what was and was not proven.
**Severity:** high — the user-visible symptom is "I enabled shell tools and the
agent still cannot execute anything", with no error anywhere. The underlying
`extra` loss is **not** shell-specific and affects every live config update.
**Component:** `src/api/persistent_app.py:7107-7121`,
`src/core/loader.py:2868-2891`, `src/api/persistent_session.py:864-868`,
`src/tools/shell/shell_tools.py:356-362,769-773`.

## Symptom

Thread `config_override.tools.shell` enumerated all four members:

```json
"tools": {"shell": ["cancel_command", "run_command", "shell_execute", "shell_read"]}
```

The agent bound exactly one. From the pod log (`src/tools/registry.py:911`, the
08:40:04Z reload):

```
Loaded 97 tools: [... 'git_tags', 'shell_read', 'srw_cloud_status', 'kb_write', ...]
```

No `run_command`, no `shell_execute`, no `cancel_command`. Immediately after:

```
Loaded 97 tools for persistent session          (persistent_session.py:1757)
Re-derived 97 tools after backend swap (VirtualOverlayBackend)   (persistent_session.py:1802)
```

`get_session_context` simultaneously reported `Supports shell: True` and
`Shell manager available: True`, and `shell_read` worked and returned a live
prompt — so the session presents as shell-enabled while being unable to execute.
The agent correctly diagnosed its own toolset, tried
`use_skill('delegate-a-job')` (rejected — not in its skill menu), and fell back
to `create_worker_job` to get the work done on a shell-capable worker. **The
delegation was a rational consequence of this bug, not a model error.**

## The mechanism

The bound set is the **intersection** of two independently-resolved views of
`shell.mode`, read from two different snapshots of the same key.

**Read site 1 — the name list.** `_load_tools_for_backend` calls
`get_all_tool_names(self.config)` (`persistent_session.py:1510`), which reads
the **current** config and aliases the two halves (`loader.py:4669-4678`):

```python
shell_config = config.extra.get("shell", {})
mode = shell_config.get("mode", "stateless") if isinstance(shell_config, dict) else "stateless"
if mode == "stateless":
    names = ["run_command" if n == "shell_execute" else n for n in names]
elif mode == "persistent":
    names = ["shell_execute" if n == "run_command" else n for n in names]
```

**Read site 2 — the tool factory.** `create_shell_tools` reads
`context.get_config("shell", {})` (`shell_tools.py:356-362`) and returns
different objects per mode (`shell_tools.py:769-773`):

```python
if mode == "persistent":
    return [shell_execute, shell_read, srw_cloud_status]
else:
    return [run_command, cancel_command, shell_read, srw_cloud_status]
```

That context config is a **boot-time snapshot** taken once in `_setup_tools`
(`persistent_session.py:1248`): `tool_config = {**self.config.extra, ...}`.

### Why the two disagree

`self.config` is replaced on a live update while `tool_context.config` is not.
`persistent_session.py:864-868` synchronizes exactly one key:

```python
if self.tool_context is not None:
    # use_skill authorizes by the CURRENT scoped menu, not by stale
    # workspace bytes. Keep its long-lived ToolContext synchronized on
    # every backend/config rebind.
    self.tool_context.config["_resolved_skills"] = scoped
```

The comment states the invariant; the code implements it for one key. `shell`
— and every other `extra` key — keeps its boot value.

### Why the *current* config lost `shell.mode`

This is the root cause, and it is not shell-specific. Live `config.update`
(`persistent_app.py:7107-7121`) round-trips the config through `asdict`:

```python
base_dict = dataclasses.asdict(_session.config)   # emits a top-level "extra" key
merged = deep_merge(base_dict, effective_override)
_apply_session_tool_group_markers(merged, effective_override)
...
new_config = load_agent_config_from_dict(merged, deployment_dir=...)
```

`load_agent_config_from_dict` collects extras by exclusion, and `"extra"` is
**not** in `known_fields` (`loader.py:2868-2891`):

```python
known_fields = {"$schema", "agent_id", "display_name", ..., "image_quality"}
extra = {k: v for k, v in data.items() if k not in known_fields}
```

So the incoming `data["extra"]` is itself treated as an extra key. The result is
`new_config.extra["extra"] = {"shell": {...}, ...}` and
`new_config.extra["shell"]` **no longer exists**. `.get("mode", "stateless")`
then lands on the stateless floor.

The de-nesting repair already exists — in `load_config_from_resolved`
(`loader.py:5306-5329`), whose comment names the exact victim:

```python
# Fix double-nesting from pre-fix serialized configs:
# Old serialize_resolved_config() stored extra as {"extra": {shell, ...}},
# which load_agent_config_from_dict() wraps into extra["extra"].
```

The live-update path never calls it.

### The intersection collapses to one tool

`config/model_config_matrix.yaml:372` maps family `gpt-5.6` →
`shell_mode: persistent`, injected into `data["shell"]["mode"]` at
`loader.py:792-802`. So at boot the session was correctly persistent-mode; after
the tools-only update the name list fell to stateless while the factory kept
persistent:

| name list | factory | bound |
|---|---|---|
| stateless | stateless | `run_command, cancel_command, shell_read` |
| persistent | persistent | `shell_execute, shell_read` |
| persistent | stateless | `cancel_command, shell_read` |
| **stateless** | **persistent** | **`shell_read`** ← observed |

Only one combination yields the observed bind, which fixes the direction without
needing a repro: the name list was stateless, the factory persistent.

Note that a *consistent* config works under either mode — which is exactly what
`tool_policy.py:95-98` relies on when it argues that enumerating both
`run_command` and `shell_execute` is "redundant, not dangerous". The thread
enumerated both and still lost the category. Redundancy cannot help when the two
halves are resolved against different snapshots.

### Third contributing defect

`_apply_settings_matrix` is re-applied only when the LLM fragment changes
(`persistent_app.py:7111-7117`):

```python
if effective_override.get("llm"):
    override_llm_keys = set(effective_override["llm"].keys())
    _apply_settings_matrix(merged, override_llm_keys, _session.config._deployment_dir)
```

A **tools-only** update therefore never re-derives per-family defaults. Family
defaults are a function of the merged config's model, not of which fragment
happened to arrive, so `shell_mode`, `limits.image_tokens` and
`limits.pdf_render_dpi` all go unrefreshed on any non-LLM update.

## Why it stayed invisible

The bind filter drops configured-but-unbuilt names silently. Nothing compares
the requested `tool_names` against what was actually bound, so a category can
empty itself with no warning. `ENUMERATE_ONLY_CATEGORIES`
(`tool_policy.py:99`) forces `shell` to enumerate its members precisely so
membership changes are reviewable — but nothing verifies the enumeration was
honored at load time. Diagnosis required reading `registry.py:911` enumerations
out of pod logs.

## Fix

Ordered by value. 1 and 2 are the ones that matter; 3–5 are correctness debts
this exposed.

### 1. Make the silence impossible

In `_load_tools_for_backend` (`persistent_session.py:1496`), diff the requested
`tool_names` against the set actually bound and log the delta at WARNING. One
line of output would have reduced this investigation to seconds, and it catches
the whole class rather than this instance. This also closes the loop
`ENUMERATE_ONLY_CATEGORIES` opens.

### 2. Fix the round-trip, not the call site

Add `"extra"` to `known_fields` in `load_agent_config_from_dict` and merge
`data["extra"]` into the computed extra (incoming top-level keys winning, to
preserve current precedence). Pin the invariant that is actually missing:

```python
load_agent_config_from_dict(dataclasses.asdict(cfg)).extra == cfg.extra
```

Fixing the loader repairs every present and future caller. Patching only
`persistent_app.py:7119` leaves the landmine armed for the next one. Once the
round-trip is idempotent, the de-nesting block in `load_config_from_resolved`
survives only to read legacy `resolved_config` JSONB rows and should say so.

### 3. Stop `tool_context.config` from going stale

Refresh the whole dict when `self.config` is replaced
(`persistent_session.py:864-868`) instead of patching `_resolved_skills` alone.
Worth noting this is what converted a wrong-but-consistent config into a
*disagreement*: with both sides stale the session would have had a working
stateless shell.

### 4. Ungate the settings matrix

Re-apply `_apply_settings_matrix` on any config change, not only when an `llm`
fragment is present (`persistent_app.py:7113`).

### 5. Structural — resolve shell mode once

Have `create_shell_tools` accept the already-resolved mode (or the resolved name
list) rather than re-deriving it from its own config read. While two sites
independently resolve the same key and the bind is their intersection, a
disagreement can silently empty a category; item 1 makes that loud but does not
make it impossible.

## Verification

**Implemented 2026-08-11 (items 1 and 2 only).**

- **Item 2** — `src/core/loader.py`: `"extra"` and `"_deployment_dir"` added to
  `known_fields`, plus a `setdefault` hoist of a serialized `extra` sub-dict so
  top-level unknown keys keep winning. `_deployment_dir` was leaking into
  `extra` by the same mechanism and is fixed alongside.
- **Item 1** — `src/api/persistent_session.py:1683-1697`: `_load_tools_for_backend`
  now diffs the backend-filtered `tool_names` against the names that actually
  bound and logs `N configured tool(s) did not bind: …` at WARNING.

**Unit** — 5 new tests in `tests/test_config_extra_round_trip.py` (round-trip
identity, no `extra["extra"]`, `shell.mode` survival, no `_deployment_dir`
leak, empty-extra idempotence) and 2 in
`tests/test_persistent_session.py::TestConfiguredButUnboundToolsAreReported`
(warns naming each unbound tool; silent when all bind). All 7 were watched
failing first. Full suite: **15112 passed, 11 failed** — those 11
(`test_vm_chart_manifest_contract`, `test_database_phase1`,
`test_mcp_capabilities`) reproduce identically on a stashed clean tree and are
unrelated local-env failures.

**k3d live gate** — both scripts replay `persistent_app.py:7107-7121` verbatim
against the real `config/session_base.yaml` + `config/model_config_matrix.yaml`
shipped in the image, with a **tools-only** override on model `gpt-5.6-sol`, and
were run inside the deployed agent image on cluster `k3d-srw`. The bind gate
reconstructs `load_tools`' literal intersection (`registry.py:513-533`) using the
real `create_shell_tools`, with the factory reading the BOOT `extra` and the name
list coming from the POST-UPDATE config — the exact split that causes the bug.

| image | boot mode (factory) | post-update mode (names) | **bound shell tools** |
|---|---|---|---|
| `tilt-5395b98b4cef7aec` (pre-fix, negative control) | `persistent` | `None` | **`['shell_read']`** |
| `tilt-1e9ec5da5b533e2a` (post-fix) | `persistent` | `persistent` | **`['shell_execute', 'shell_read']`** |

The pre-fix row reproduces the production symptom exactly — `shell_read` alone,
no way to execute anything — and the post-fix row restores an executing tool.

**Not proven by this gate:** the WebSocket `config.update` frame plumbing and the
orchestrator→agent transport were not exercised; the gate begins at the merge.
Both are unchanged by these fixes, and the defect lives entirely in the merge, but
a full UI-driven session gate has not been run.

## Regression tests

Landed (see [Verification](#verification)):

- `load_agent_config_from_dict(asdict(cfg)).extra == cfg.extra` for a config
  carrying `extra.shell.mode` — the round-trip identity.
- A live `config.update` preserves unrelated `extra` keys (guards the general
  `extra` loss, not just shell).
- Requested-vs-bound delta warns naming each miss, and stays silent when every
  configured tool binds.

Still owed:

- The bound-set assertion currently lives in a throwaway k3d script, not in the
  suite. Port it: after a **tools-only** live update on a persistent-mode family,
  the bound set contains an executing shell tool, not just `shell_read`.
- Same assertion under a `stateless` family, so the alias is covered both ways.

## Operational note

Re-toggling the checkbox re-enters the same path and does not help. The
workaround until this lands is to end and resume the session, forcing a full
`_setup_tools` so both read sites see one fresh config.

Verify any session's real toolset with:

```
kubectl -n superhuman-remote-worker logs <agent-pod> -c agent | grep "Loaded .* tools:"
```

## Related

- `docs/issues/tool_configuration_defects_and_fix_roadmap.md` — the consolidated
  tool-config entry point. This is a **different seam**: the config is correct
  and the UI is honest; the bind drops names.
- `docs/issues/session_tool_group_enablement_is_computed_in_two_places.md` — the
  same "two independent resolutions of one fact" shape, one layer up.
- `docs/issues/live_permission_mode_change_never_persisted.md` — another live
  session-update path that does not converge.
