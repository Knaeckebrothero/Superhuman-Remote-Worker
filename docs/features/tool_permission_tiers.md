---
tags:
  - security
  - agent-architecture
  - persistent-sessions
related:
  - "[[settings_design]]"
  - "[[sudo_permissions]]"
  - "[[sudo_approval_gate]]"
  - "[[persistent_session_permission_check_race]]"
  - "[[sessions]]"
---

# Tool Permission Tiers for Interactive Sessions

> **Status: Design.** Replaces the binary "shell vs. everything" permission gate with a per-tool risk classification, and redefines the three session permission modes as a monotonic tier ladder so reads are always free and only consequential actions require approval. Ships with built-in defaults; a user-facing customization layer is designed-for but deferred (Layer 2).

## Problem

Interactive sessions gate tool calls through `_loop_permission_check` (`src/api/persistent_app.py:2841`). The mode decides what auto-approves:

1. **`supervised` gates everything — including reads.** The default mode asks the user to approve `read_file`, `list_files`, `git_status`, `web_search`, every time. That is pure friction with no safety payoff: a read has no side effect to approve.

2. **The only classification is a hardcoded shell set.** `auto_accept` auto-approves every tool *except* `shell_tools = {"run_command", "shell_execute", "shell_read"}` (`persistent_app.py:2865`). "Is it a shell tool?" is a proxy for risk that is simultaneously **too strict** (supervised blocks harmless reads) and **too loose** (`auto_accept` silently auto-approves `delete_file`, `delete_directory`, `send_message`, and datasource writes like `sql_execute` / `cypher_execute` / `mongo_update`).

3. **Tools carry no notion of risk.** The ~100 tools across 17 categories have `category`, `phases`, and `description` metadata, but no read/write/risk field anywhere (`src/tools/registry.py`). There is nothing to drive a smarter gate.

4. **The three modes aren't a coherent ladder.** The jump from "ask for everything" (`supervised`) to "ask only for shell" (`auto_accept`) is large and arbitrary, which is why the labels don't communicate what they actually do.

## Design Principles

- **Reads are always free.** No tool without side effects ever requires approval, in any mode.
- **Classification is intrinsic and co-located.** A tool's risk is a *fact about the tool*, declared next to its definition — not a user preference and not a second list maintained elsewhere.
- **Fail-closed.** An unclassified tool defaults to the most restrictive tier (gated). A forgotten classification can only ever make the system *more* cautious, never silently auto-approve.
- **Monotonic ladder.** Each mode auto-approves a strict superset of the mode below it, so the modes form an intelligible "less ↔ more autonomy" spectrum.
- **Defaults and overrides are separate layers.** System defaults live in code/config. Per-user overrides (future) live in `users.settings`. The two are never conflated.
- **Design the seam, defer the surface.** The gate consults a per-user/session override layer from day one. That layer is empty until the customization UI ships, so this feature is shippable on its own with zero rework cost for Layer 2.

## Solution

### 1. The tier model

Three tiers, nested by side-effect severity:

`read` ⊂ `write` ⊂ `consequential`

| Tier | Meaning | Representative tools |
|---|---|---|
| **`read`** | Observes state. No mutation anywhere. | `read_file`, `list_files`, `search_files`, `git_log`/`git_status`/`git_diff`, `web_search`, `extract_webpage`, `kb_read`/`kb_search`, `sql_query`/`cypher_query`/`mongo_query`/`webdav_read`, `task_list`, `browser_navigate`/`browser_snapshot`/`browser_screenshot`, `shell_read`, orchestrator `get_*`/`list_*` |
| **`write`** | Mutates the sandboxed, git-versioned workspace or internal project state. Reversible. | `write_file`, `edit_file`, `create_directory`, `move_file`/`rename_file`/`copy_file`, `download_paper`/`download_from_website`, `cite_*`/`annotate_source`/`tag_source`/`generate_bibliography`, `kb_write`/`kb_update`/`kb_export`, `task_add`/`task_complete`, `browser_click`/`browser_type`/`browser_select` |
| **`consequential`** | Irreversible, escapes the sandbox / acts on the outside world, executes arbitrary code, or consumes external resources. | `delete_file`, `delete_directory`, `run_command`, `shell_execute`, `send_message`, datasource writes (`sql_execute`, `cypher_execute`, `mongo_insert`/`mongo_update`, `webdav_write`/`webdav_delete`), `delegate_work`/`resume_delegation_child`, orchestrator job control (`create_worker_job`, `cancel_worker_job`, …) |

**The assignment rule (so new tools self-classify):** if a tool only observes, it's `read`; if it mutates recoverable state inside the workspace/project, it's `write`; if it is irreversible, outbound, arbitrary-execution, or resource-consuming, it's `consequential`.

**Notes on edge cases:**
- **Shell *execution* folds into `consequential`** (`run_command`, `shell_execute`). This means `auto_accept` *keeps* gating shell execution (as today) **and** newly gates the deletes/sends/datasource-writes it currently lets slip — the too-loose half of the bug, fixed for free.
- **`shell_read` is `read`, deliberately.** It only reads buffered scrollback — no side effect. So you approve the command (`run_command`), then the agent reads its output freely. The gate is on *executing*, not on *reading results*.
- **`browser_direct` is split by intent:** navigation/observation (`browser_navigate`, `browser_snapshot`, `browser_screenshot`, `browser_scroll`, `browser_back`) is `read`; page interaction (`browser_click`, `browser_type`, `browser_select`) is `write`. Gating every click would make `auto_accept` unusable for browsing.
- **Datasource dual-mode tools are classified by tool name, not by the datasource's `read_only` flag.** `*_query` = `read`, `*_execute`/`*_insert`/`*_update` = `consequential`. This is a safe over-approximation that avoids threading runtime datasource state into the gate.

The full per-tool table is `read`/`write`/`consequential` examples above; the remaining tools are tagged during the implementation classification pass, backstopped by the CI test in §6.

### 2. Mode redefinition: the ladder

A new `mode_tier_policy` declares which tiers each mode auto-approves. Everything not listed is gated through the existing approval path.

```yaml
interactive:
  permission_mode: supervised
  mode_tier_policy:              # tiers that auto-approve; the rest gate
    supervised:  [read]
    auto_accept: [read, write]
    autonomous:  [read, write, consequential]
  narration_mode: auto
  idle_timeout_minutes: 30
```

| Mode | Auto-approves | Gates | Change from today |
|---|---|---|---|
| `supervised` | `read` | `write`, `consequential` | **Reads stop prompting** (the fix). Writes/consequential still gate. |
| `auto_accept` | `read`, `write` | `consequential` | Reads + writes free as before; **now also gates `delete`/`send`/datasource-writes** (was: only shell). |
| `autonomous` | everything | — | Unchanged. |

This is the monotonic ladder: `supervised` = "review every change," `auto_accept` = "writes are free, ask before anything irreversible/outbound," `autonomous` = "don't ask."

### 3. Where the defaults live (three homes)

There are two different kinds of default, and they belong in different places:

**(a) Tool classification — intrinsic — in each tool's metadata.** `TOOL_REGISTRY` is assembled from ~15 `get_*_metadata()` functions (`src/tools/registry.py:61-85`), one per tool package. Each tool's metadata dict gains a `risk_tier` field alongside `category`/`phases`:

```python
"delete_file": {
    "category": "workspace",
    "risk_tier": "consequential",   # NEW
    "description": "...",
},
```

Lookup is fail-closed: `TOOL_REGISTRY.get(name, {}).get("risk_tier", "consequential")`. A new or unknown tool is gated until explicitly classified. A standalone classification YAML was rejected: it would be a second source of truth that silently drifts when a tool is added (the registry is code, assembled across packages), whereas a missing-from-YAML tool gives no fail-closed guarantee.

**(b) Mode policy — the tunable "setting default" — in config.** `mode_tier_policy` joins `permission_mode` in:
- `config/defaults.yaml` → `interactive:` block (lines 329-333 today), the source experts extend via `$extends`.
- `src/core/loader.py` → `InteractiveConfig` (line 1500) gains a `mode_tier_policy` field with a hardcoded fallback (the ladder above), used when the YAML key is absent. *Both* parse sites must set it (`loader.py:~2005` and `~2207` — note these are duplicated today; the implementation should set the field in both, and ideally dedupe the two `InteractiveConfig(...)` constructions).
- `config/schema.json` → validate the `mode_tier_policy` shape (line 666 region).

This flows through the existing resolution chain (defaults → expert → project → user) for free.

**(c) Overrides — per-user/session — in `users.settings` (DEFERRED).** Layer 2 will store `users.settings.persistent_agent.tool_policies` (a `tool`-or-`category` → `auto`/`ask`/`deny` map), per-user, with a per-session counterpart in thread metadata. It is **empty for Layer 1**. No migration: `users.settings` is already JSONB.

### 4. The permission gate

`_loop_permission_check` (`persistent_app.py:2858-2867`) gains the tier lookup and an override hook that is a no-op today:

```python
mode = _session.permission_mode

if mode == "autonomous":
    return True                                    # unchanged fast path

tier = get_risk_tier(tool_name)                    # fail-closed → "consequential"

override = _lookup_override(tool_name, tier)       # Layer 2 seam; returns None today
if override is not None:
    return _decide_from_override(override)          # honored subject to the floor (§5)

if tier in _session.mode_tier_policy[mode]:
    return True                                    # tier auto-approves in this mode

# else: gate via the durable thread_permission_requests path (unchanged)
```

The resolved `mode_tier_policy` is carried on `PersistentSession` alongside `permission_mode` (initialized from `config.interactive.mode_tier_policy` at setup, `src/api/persistent_session.py`), so a runtime `mode.set` switch just changes which policy key is read.

The `thread_permission_requests` table, LISTEN/NOTIFY pump, Phase-5 wake-path decision reuse, and the cockpit approval card are all **untouched** — gated tools flow through exactly the path they do today.

### 5. Safety floor (designed, deferred)

`autonomous` approves everything in Layer 1 — that is its existing contract ("runs without asking"). The *floor* concept (a subset of `consequential` tools that always confirm, even in `autonomous`, and that no user override can lower) matters only once user overrides exist, since its job is to stop a customization from removing every guardrail. We reserve an `always_gate` flag on tool metadata for Layer 2 and do **not** enforce it now.

## Behavior changes & migration

- **No DB schema change.** Uses the existing `thread_permission_requests` path and the existing `users.settings` JSONB.
- **Old configs/experts that don't specify `mode_tier_policy`** inherit the ladder from the `InteractiveConfig` fallback automatically.
- **`supervised` becomes more permissive** (reads no longer prompt) — strictly less friction, safe.
- **`auto_accept` becomes stricter** for non-shell consequential tools: `delete_file`, `delete_directory`, `send_message`, and datasource writes that previously sailed through will now prompt. This is a bugfix, but it is a visible behavior change for anyone relying on `auto_accept` to auto-delete or auto-send — call it out in the release note.

## Out of scope (deferred to Layer 2)

Designed-for (the override hook and `users.settings` shape exist) but not built here:
- The customization UI — a settings panel over tools/categories with per-entry level selectors, extending `AgentSettingsComponent`.
- Per-user/session override persistence and precedence (session > user > mode default).
- Safety-floor enforcement (`always_gate`).

## Testing

- **CI guard:** assert every `TOOL_REGISTRY` entry has an *explicit* `risk_tier` (so we never silently lean on the fail-closed fallback for a tool we forgot).
- **Tier lookup:** unknown tool → `consequential`; known tools → expected tier.
- **`mode_tier_policy` resolution:** default ladder applies; expert/project override replaces it; absent key → dataclass fallback.
- **Gate decision tree** (extend `tests/test_thread_permissions_phase3.py`): `read` auto-approves in `supervised`; `write` gates in `supervised` but auto-approves in `auto_accept`; `consequential` gates in `auto_accept` but auto-approves in `autonomous`; unclassified tool gates everywhere except `autonomous`; the (empty) override hook is a no-op.

## Open questions

- Final tier for a few ambiguous tools (`delegate_work`/`resume_delegation_child`, orchestrator job-control, knowledge `kb_export`) — resolve during the classification pass; defaults proposed above.
- Field name `risk_tier` and whether to align its vocabulary with the sudo gate's `low`/`medium`/`high`/`critical` risk categories (different domain — agent tools vs. shell commands — so kept distinct here).
- Whether `browser_type` that submits a form should be `consequential` rather than `write` (it can POST externally). Proposed: keep `write` for usability; revisit if abuse surfaces.
