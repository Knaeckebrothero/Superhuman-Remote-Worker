---
tags:
  - feature
  - agent
  - workspace
  - tools
  - contacts
aliases:
  - virtual directories
  - virtual filesystem
  - overlay backend
related:
  - "[[contacts_registry]]"
  - "[[agent_skills]]"
  - "[[workspace_storage_state_topology]]"
---

# Virtual Directories

> Agent-visible directories (`tools/`, `contacts/`) served live from in-process and orchestrator state through the workspace file tools — nothing written to the workspace filesystem, nothing stale after a snapshot restore, nothing leaked into repos or backups.

**Status:** Design approved 2026-07-30 (brainstorm + prior-art review). Not yet implemented.
**Filed:** 2026-07-30

## Motivation

Agent-guidance content is currently **materialized as real files** on the workspace:

| Piece | Where (cite symbols — line numbers drift) |
|---|---|
| `tools/README.md` + `tools/<name>.md` written at boot | `generate_workspace_tool_docs` calls in `src/agent.py` (worker) and `src/api/persistent_session.py` (session) |
| Rendering of index + per-tool docs | `src/tools/description_manager.py` — `generate_tool_index`, `generate_tool_description` |
| Deferred tools carry short descriptions in context, full doc in workspace | `apply_description_overrides`, registry `defer_to_workspace` |
| Planned `contacts/<slug>.md` materialization | [[contacts_registry]] §Agent surface (approved 2026-07-29, unimplemented) |

Materialization has a recurring failure class:

- **Staleness.** Workspaces persist through snapshots/restores; nothing prunes docs for removed tools, and mid-lifecycle tool changes (virtual→sandbox workspace-upgrade re-derive, session tool-group overrides) leave docs describing the wrong tool set.
- **Leakage.** Generated dirs sit next to project files; `skills/` leaked onto a loop's `main` branch once (caught by k3d E2E), and the contacts spec had to mandate a `_LOOP_MAIN_GITIGNORE` entry to keep names/emails/phone numbers out of project artifacts, plus accept PII in workspace snapshots.
- **Write ambiguity.** Agents can edit generated files; edits are silently overwritten at the next regeneration.
- **Freshness ceiling.** File projections are only as fresh as the last seeding run — the contacts spec documents "linked mid-session, invisible until next start" as a known limitation.

Virtual directories remove the class: the content is *served* at read time, never stored.

## Prior art (2026-07-30 review)

- **Anthropic, "Code execution with MCP"** — tools presented as a filesystem tree the agent lists/reads on demand (150k→2k token reduction claim). Their tree is generated into an ephemeral sandbox; our workspaces persist, which is exactly why we serve instead of write. <https://simonwillison.net/2025/Nov/4/code-execution-with-mcp/>
- **LangChain deepagents** — filesystem tools run against a pluggable virtual backend; `CompositeBackend` routes path *prefixes* to different backends. Same shape as this design (theirs is read-write working memory; ours is a read-only projection). <https://docs.langchain.com/oss/javascript/deepagents/context-engineering>
- **Dust.tt** — production synthetic filesystem over live enterprise data (Slack/Notion/GitHub as `list`/`cat`/`search` trees), served live with caching; lesson adopted: virtual reads must flow through the normal read caps/pagination. <https://www.zenml.io/llmops-database/building-synthetic-filesystems-for-ai-agent-navigation-across-enterprise-data-sources>
- **arXiv 2607.17598** — one level of index→leaf routing scales; "a second, deeper routing level never helps and sometimes breaks accuracy outright." Virtual prefixes stay flat in v1. <https://arxiv.org/abs/2607.17598>
- **ToolFS / Mirage** — FUSE-based full VFS visible to the shell; the road not taken (mount/daemon infrastructure on every pod and VM image).

## Scope decisions (user, 2026-07-30)

- **General mechanism, two v1 consumers:** `tools/` and `contacts/`. Skills untouched (skill scripts execute via the shell and need real files).
- **Tool-layer only.** Virtual dirs are visible through the file tools (`read_file`, `list_files`, `search_files`, …), **not** through the shell (`run_command` executes on the workspace pod/VM over SSH against the real filesystem). No FUSE.
- **Live read-through for contacts** (short TTL cache) — erases the contacts spec's staleness limitation. Tools content is inherently live (rendered from the in-process tool list).
- **Same paths as today** — `tools/` and `contacts/` at the workspace root; the virtual layer owns those subtrees; prompts and instruction files keep working unchanged.
- **Approach: overlay backend** at the `WorkspaceManager` seam (over per-tool interception and over FUSE), confirmed against prior art.

## Architecture

**Seam.** New `VirtualOverlayBackend` (`src/core/backends/overlay.py`) wraps the real backend where `WorkspaceManager` stores it (`self._backend`). Local, remote-SSH, and virtual-tier backends are wrapped identically; file tools keep calling `context.workspace_manager` and never learn the overlay exists. Subagents (scholar/critic, spawn_subagent reader) inherit it through the shared context.

**Idiom.** Same as `SubdirBackend` (`src/core/backends/subdir.py`): a plain delegating wrapper, *not* a `WorkspaceBackend` subclass — `__getattr__` forwards everything not overridden, so future backend-interface growth delegates by default. Overridden are exactly the path-touching methods: `read_file`, `write_file`, `append_file`, `exists`, `is_file`, `is_dir`, `list_dir`, `search_files`, `stat`, `resolve_path`, `mkdir`, `delete_file`, `delete_directory`, `move`, `copy`. Each override: if the workspace-relative path (either endpoint for `move`/`copy`) starts with a registered prefix segment → the overlay handles it (serve, reject, or copy-out, per §Semantics); else delegate.

**Provider contract** — deliberately the whole surface:

```python
class VirtualDirProvider(Protocol):
    prefix: str                                  # "tools", "contacts"
    def entries(self) -> dict[str, EntryMeta]    # filename -> {size, mtime}
    def read(self, subpath: str) -> str | None   # None = not found
```

`list_dir`, `exists`, `is_file`, `is_dir`, `stat`, glob patterns, and `search_files` (grep over `entries()` + `read()`) are derived generically in the overlay. A provider is ~40 lines and cannot be internally inconsistent (no way for `exists` and `read` to disagree). Virtual prefixes are **flat** — no subdirectories in v1. Rendered content flows through the normal `read_file` size caps/pagination; `stat` reports rendered length; `resolve_path` returns the synthetic absolute path under the workspace root.

**Registration.** At the two boot paths where `generate_workspace_tool_docs` is called today: worker (`src/agent.py`) and session (`src/api/persistent_session.py`). Tools provider always; contacts provider only when the job/session has a project. Kill switch `VIRTUAL_DIRS_ENABLED` (env, default `true`): when off, the overlay is not installed — see Failure modes.

## Providers

### ToolsProvider

- Holds a **callable returning the currently loaded tools** plus the existing `DescriptionManager`. `entries()` = `README.md` + `<name>.md` per current tool; `read()` renders on demand via the untouched `generate_tool_index` / `generate_tool_description` — content byte-identical to today's files, rendering stays in one place.
- Because it reads the current tool list per call, mid-lifecycle tool changes (workspace-upgrade re-derive, session tool-group overrides) are reflected immediately with no regeneration step.
- Registered before `apply_description_overrides` runs, so full docstrings are captured (same ordering as today's materialization).

### ContactsProvider

- **Data path:** one thin orchestrator internal endpoint, `X-Internal-Key` authed, keyed by **job/thread identity — not caller-supplied project_id**. The orchestrator derives the project binding server-side (same trust posture as `send_message` resolution), so an agent can never read another project's contacts. Called via the agent's existing orchestrator client.
- **Cache:** one in-process fetch shared by `entries()` and `read()`, ~60s TTL, ~3s client timeout so a `read_file` can never hang a turn.
- **Rendering:** the file format from [[contacts_registry]] — frontmatter (`name`, `display_name`, `addresses` incl. raw addresses per the 2026-07-28 decision, `projects`), body = `notes`. Plus a `README.md` index (display name + channel chips per contact) mirroring `tools/` — one-read discovery, still no `list_contacts` tool. Slugs per the contacts spec: `_safe_component` kebab, deterministic `-2` collision suffix via `created_at` ordering.
- Without a project, `contacts/` is not registered and the path falls through to the real filesystem like any other.

### Sequencing

The overlay + ToolsProvider are independently shippable and go **first** (they migrate an existing surface). The ContactsProvider **depends on the contacts registry** ([[contacts_registry]] — tables, internal endpoint, resolver), which is approved but unimplemented; it ships as the agent-surface slice of that feature, against the overlay contract defined here. Nothing in the overlay changes when it lands — a provider registers, that's all.

## Semantics

- **Full subtree ownership.** A registered prefix answers everything under it, including names the provider lacks: `read_file("tools/removed_tool.md")` is a clean not-found, never a fall-through to a stale real file. Partial fall-through would resurrect the staleness bug. Matching is on the first segment of the workspace-relative path — `main/tools/` inside a cloned repo is untouched.
- **Listings.** Root listing delegates to the real backend, then merges the virtual prefixes in (deduped against real leftover dirs of the same name). Inside a prefix, listings come purely from `entries()`. No "(virtual)" markers in listing output; instead the rendered instruction files (`workspace.md` template) state that `tools/` and `contacts/` are virtual, tool-readable only, and invisible to the shell.
- **Search.** Scoped inside a prefix → provider-content grep only. Root-scoped → real results merged with provider matches in the same `path:line:text` format, `SEARCH_RESULT_HARD_CAP` applied to the merged set.
- **Mutations.** `write_file`, `append_file`, `mkdir`, `delete_file`, `delete_directory` on a virtual path, and `move` touching one in either direction, raise `VirtualPathError` with a teaching message: *"tools/ is a virtual, read-only directory generated from the live tool registry; its files cannot be modified. Copy a file out if you want an editable version."* Exception: `copy` **from** virtual **to** real works (provider-read + inner-write) — the escape hatch the message names.
- **Boot sweep of leftovers.** Nothing writes real `tools/` anymore, so boot runs a one-shot, logged, non-fatal sweep through the *inner* backend: if real `tools/README.md` exists and its first line is the generated marker (`# Available Tools`), delete the directory — old snapshots converge and the shell stops showing stale docs too. No marker → leave it (user's own dir) and log. `contacts/` never shipped as files; nothing to sweep.
- **Code migration.** Delete the two `generate_workspace_tool_docs` call sites; keep `DescriptionManager` rendering functions as the provider engine; delete the file-writing wrappers (`generate_workspace_docs`, `generate_workspace_tool_docs`) once both callers are gone. `_LOOP_MAIN_GITIGNORE` keeps its `tools/` line as legacy protection for old snapshots; `contacts/` is never added.

## Failure modes

| Case | Behaviour |
|---|---|
| Contacts fetch fails, cache warm | Serve stale + log warning (TTL keeps retrying) |
| Contacts fetch fails, no cache | Reads/listings return "contacts temporarily unavailable" tool-error string; boot never blocks |
| Contacts fetch slow | ~3s client timeout, then the stale/error path above |
| All contacts unlinked mid-session | Next TTL window: directory holds only the README index reading "no contacts linked" |
| Provider raises unexpectedly | Overlay catches, logs, surfaces a readable tool error; the agent loop never crashes on a virtual read |
| Mutation attempt | `VirtualPathError` teaching message, rendered as normal tool error text |
| Real leftover dir collides with a prefix | Shadowed by design; `tools/` additionally converged by the marker-checked boot sweep |
| `VIRTUAL_DIRS_ENABLED=false` | Overlay not installed; **no** fallback materialization (that path is deleted). Deferred tools degrade to short descriptions only. Emergency-switch behavior, not a supported mode |

## Testing

- **Overlay unit matrix:** every overridden method × virtual/real path (+ `move`/`copy` cross-combinations); full-ownership (unknown name under prefix → not-found, never inner); root-listing merge + dedupe; search merge respecting `SEARCH_RESULT_HARD_CAP`; copy-out allowed; mutations raise; `__getattr__` delegation (guards against backend-interface drift).
- **ToolsProvider:** README byte-identical to `generate_tool_index`; entries track a changed tool list without re-registration (upgrade re-derive case); registration order captures full docstrings pre-override.
- **ContactsProvider:** rendering matches the contacts-spec format + README index; deterministic slug collisions; one fetch per TTL window; stale-serve and no-cache-error paths; empty-project case.
- **Boot sweep:** marker → deleted; no marker → preserved + logged; sweep errors non-fatal.
- **Orchestrator endpoint:** internal-key required; project derived from job/thread identity; cross-project access denied.
- **Live gate on local k3d** before dev deploy: session — root listing shows `tools/`, read `tools/README.md`, write attempt rejected with the teaching error, link a contact mid-session and see it appear within TTL; worker job smoke of the same. CI (Py3.12) is the merge gate.

## Out of scope (v1)

Skills docs migration (scripts need real files; revisit after v1 proves out) · shell visibility / FUSE · subdirectories inside virtual prefixes · "(virtual)" markers in listing output · additional providers (datasources, experts, memory) · a materialization fallback mode · write-through virtual files.

## Companion change to the contacts spec

[[contacts_registry]] §Agent surface is amended (same commit as this spec): materialized `contacts/<slug>.md` files are replaced by the ContactsProvider projection. Struck with the mechanism: the `_LOOP_MAIN_GITIGNORE` requirement, snapshot-PII concern, agent-edit-overwrite behavior, the staleness limitation, and the `CONTACTS_MATERIALIZE_ENABLED` gate (superseded by `VIRTUAL_DIRS_ENABLED`). File format, raw-address decision, no-`list_contacts` decision, DB schema, API, resolver, and Cockpit sections are untouched.

## Decision log

- **2026-07-30:** General virtual-directory mechanism; v1 = `tools/` + `contacts/`; skills stay materialized. (User.)
- **2026-07-30:** Tool-layer visibility only — shell does not see virtual dirs; FUSE rejected. (User.)
- **2026-07-30:** Contacts served live (read-through + ~60s TTL) rather than boot-snapshot. (User.)
- **2026-07-30:** Keep today's root paths (`tools/`, `contacts/`); virtual layer owns the subtrees; boot sweep converges old snapshots. (User.)
- **2026-07-30:** Overlay-backend seam (SubdirBackend idiom) over per-tool interception, confirmed against deepagents `CompositeBackend` / Dust.tt prior art; flat one-level trees per arXiv 2607.17598. (User.)
