# Session `contacts/` never registers when the project came from `threads.project_id`

**Status:** **RESOLVED on `develop`** by `4f54f599` (2026-08-05), incidentally — see §Resolution.
Re-verified live on local k3d 2026-08-09; does not reproduce. **Still present on `main`/prod**
(`4f54f599` is develop-only and carries no tag), so it ships with the next release cut.
Found by the virtual-directories cloud-sync live gate on local k3d, 2026-08-04.
**Severity:** Medium — a session silently shows the agent *no contacts at all* on a project that has them. No error anywhere; the directory simply isn't there.

## Symptom

A session created through Cockpit's **"Default project"** path, on a project with a linked contact, gives the agent nothing:

```
read_file("contacts/README.md")
→ Error: File not found: threads/b1923994-…/contacts/README.md
     (resolved from workspace root; you passed "contacts/README.md")

list_files("")  → no "contacts" entry
```

The same contact, on a session created by **explicitly selecting** a project, works — the agent log shows `Registered virtual provider: contacts`.

## Root cause

Two independent sources record a thread's project, and the client-side registration guard reads only one of them.

`src/api/persistent_session.py` (the ContactsProvider registration):

```python
if orchestrator_url and thread_id and self.project_id:
```

`self.project_id` is `project_ids[0]` (`persistent_session.py:299`), and `project_ids` is built by the orchestrator from **`thread_mounts`** — `[m["source_ref"] for m in mounts if m["mount_kind"] == "project"]` (`orchestrator/main.py:23629`). It never consults `threads.project_id`.

The two session-creation paths populate *different, mutually exclusive* sources. Measured on local k3d:

| Session | created via | `threads.project_id` | `thread_mounts` | `contacts/` registered |
|---|---|---|---|---|
| `b1923994` | "Default project" | **set** | **0 rows** | **no** |
| `f9b63a66` | explicit "Slice C Gate" | NULL | 1 row | yes |

Two further pre-existing threads on that cluster have a mount and a NULL column, confirming the divergence is normal, not a one-off.

The **server side already handles both**: `resolve_project_for_agent` (`orchestrator/database/postgres.py`) queries `thread_mounts` first and falls back to `threads.project_id` — added deliberately during the virtual-directories fix wave, because a mount-only thread would otherwise resolve to nothing. So the endpoint would have answered this correctly. The client simply never asks.

## Why the tests missed it

`tests/test_resolve_project_for_agent.py` covers all four *server* branches, including the column fallback. Nothing covers the *client* guard, and no unit test can — it depends on which of two DB tables a particular Cockpit flow happened to write.

## Resolution

Fixed a day after this was filed, by a commit that was solving something else:
`4f54f599` "feat(datasources): add project scopes and auto attach defaults"
(2026-08-05). It widened the lazy backfill in `_thread_project_ids`
(`orchestrator/main.py:25087`) from

```python
legacy_ids = metadata.get("project_ids") or []
if not legacy_ids:
    return []
```

to also consult the durable column:

```python
fallback_ids = list(dict.fromkeys([
    *([str(thread["project_id"])] if thread.get("project_id") else []),
    *(str(value) for value in (metadata.get("project_ids") or [])),
]))
```

Its stated motive — *"Connector authorization must not depend on cloud
availability"* — is the same root condition by another name: a default-project
thread whose cloud mount row could not be built had no `thread_mounts` row, and
before this change nothing else spoke for its project. The client guard was
never touched; none of the three options below was taken.

Note the fallback landed **after** the 08-04 gate. An earlier commit
(`a07a6083`, 08-02) touched the same function, which makes `git log -S` on the
fallback line report both — dating the *area* rather than the line is what makes
this look, on a quick read, as though the fallback predated the gate. It did not.

### Re-verification, local k3d, 2026-08-09

Cockpit → New Session → "Default project" pre-selected → Create (session
`da1995b3`), driven through Playwright, with the same "Anna Weber" contact still
linked to the default project.

| Check | 2026-08-04 (`b1923994`) | 2026-08-09 (`da1995b3`) |
|---|---|---|
| `threads.project_id` | set | set |
| `thread_mounts` | **0 rows** | `project_default:2d37c971` |
| `Registered virtual provider: contacts` | **absent** | present |
| `list_files("")` | no `contacts` entry | `contacts/` with `README.md`, `anna-weber.md` |
| `read_file("contacts/README.md")` | **File not found** | `# Contacts` / `- [Anna Weber](anna-weber.md) — email` |

The mount row is written 43 ms after the thread row, i.e. the backfill
materializes it on first access rather than the creation path having changed.

### Wider blast radius, also fixed

`project_ids` gates more than contacts. While it was empty, the same session also
skipped the knowledge store (`persistent_session.py:1184`) and the Neo4j graph
tier (`:1227`), and left `tool_context.project_ids` empty (`:1300`) — so a
default-project session lost its project knowledge base too, equally silently.
The same fix restores all of them.

## Fix options (historical — none were taken)

1. **Let the server decide (recommended).** Drop `self.project_id` from the guard and register whenever `orchestrator_url` and `thread_id` exist. The endpoint already derives the project server-side and returns `{"contacts": []}` when there is none, which renders "No contacts are linked to this project." Cost: `contacts/` becomes reserved on project-less sessions too, which deviates from the spec line "without one, `contacts/` must not be reserved at all" — that line should then be amended.
2. **Widen the client guard** to consider the thread's `project_id` as well as `project_ids`. Keeps the spec's "only when there is a project" property, but requires the column to reach the session object.
3. **Fix the divergence upstream** so both creation paths write a `thread_mounts` row. Addresses the root cause and would fix any other consumer of `project_ids`, but has a blast radius well beyond contacts.

(1) is the smallest correct change; (3) is the real cure. They are not exclusive.

## Reproduction

1. Cockpit → Contacts → add a contact, link it to the user's **default** project.
2. Cockpit → New Session → leave **"Default project"** selected → Create.
3. Ask the agent to `list_files("")` and `read_file("contacts/README.md")`.
4. Observe: no `contacts` entry, file-not-found. Agent log has `Registered virtual provider: tools` but no `contacts` line.
5. Repeat with a session where the project is selected explicitly → `contacts` registers and renders.

## Related

- `docs/features/virtual_directories.md` — §Live gate, and the Registration paragraph this contradicts.
- `tests/virtual_directories_test_coverage.md` §2.1 — the cloud-sync gate that surfaced it.
- `docs/done/contacts_registry.md` — the agent-surface contract.
