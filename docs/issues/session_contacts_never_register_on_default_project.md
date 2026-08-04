# Session `contacts/` never registers when the project came from `threads.project_id`

**Status:** OPEN. Found by the virtual-directories cloud-sync live gate on local k3d, 2026-08-04.
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

## Fix options

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
