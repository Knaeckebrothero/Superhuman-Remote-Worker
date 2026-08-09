---
tags:
  - feature
  - sessions
  - datasources
  - grants
  - security
aliases:
  - config drift
  - resume without them
  - deleted connector resume
related:
  - "[[datasource_redesign]]"
  - "[[sessions]]"
  - "[[settings_design]]"
---

# Session config drift on resume

**Status:** Designed 2026-08-09. Not implemented.
**Origin:** Live incident on dev — session `1930dec9` returned 403 on every resume
and the button appeared dead.

## 1. The problem

A session's stored configuration names things that can disappear underneath it:
a connector can be deleted, a project membership revoked, a capability grant
withdrawn. Today every one of those collapses into a single non-enumerating
denial, and the session is permanently unusable with no way back.

The triggering case: `threads.metadata.datasource_ids` for `1930dec9` listed two
connectors, one of which (`d7555d5d-…`) had been deleted from `datasources`.
`resume_thread` (`orchestrator/main.py:29908`) calls
`_revalidate_thread_datasource_ids`, which reaches
`authorize_datasource_selection`
(`orchestrator/services/datasource_policy.py:169`):

```python
by_id = {str(row["id"]): row for row in rows}
if len(by_id) != len(normalized_ids):
    raise DatasourceUnavailableError()      # -> HTTP 403
```

Two ids requested, one row returned, hard denial. Verified against the live dev
database by calling the real function in the orchestrator pod:

```
policy rows returned: 1 for 2 requested
   MISSING: {'d7555d5d-ce46-49e2-b1fa-8235d720badc'}
BOTH  (what resume sends): DENIED  (DatasourceUnavailableError -> HTTP 403)
ONLY-SURVIVOR           : ALLOWED -> 1 connector(s)
```

The missing-row check runs *before* `allow_admin_explicit_override` is consulted,
so an admin is denied too.

### Why the button looked dead

`persistent-chat.service.ts:2333` swallows every resume error:

```js
} catch (err) {
    // /resume may 409 if the thread isn't actually 'ended' (e.g. a
    // double-click). Fall through to connect() either way — its
    // cold-start path is self-healing.
}
```

Written for the benign double-click 409, it also eats the 403. `connect()` then
runs against a still-`ended` thread and achieves nothing. No toast, no error, no
console message beyond the raw network 403.

### Blast radius (dev, 2026-08-09)

24 threads across 3 users reference 9 distinct deleted connectors.

| User | Threads |
| --- | --- |
| `overlygenericaddress@pm.me` | 17 ended + 4 created |
| `marcel.hart@stud.fra-uas.de` | 1 ended |
| `maximilian.jurkowski@stud.fra-uas.de` | 1 ended + 1 suspended |

`created` threads are affected too: attach (`main.py:4527`) runs the same
revalidation.

### How the references became dangling

`delete_datasource` (`orchestrator/database/postgres.py:9318`) is a hard
`DELETE FROM datasources` with no cleanup of `threads.metadata.datasource_ids`.
Every session that referenced the connector keeps the dead uuid forever.

### There is no self-service recovery

The three exits are mutually blocked:

- resume → 403.
- attach → same revalidation, refuses.
- settings pane → the picker *does* render an unavailable placeholder the user
  could untick, but `updateConfig()` dispatches a `config.update` **WebSocket
  frame to the agent**. An ended session has no agent, so `_sendControl` queues
  the frame forever. There is no REST fallback.

The backend *does* have the right endpoint —
`PATCH /api/persistent/threads/{id}/config`, whose docstring states "Ended
threads are editable" — but the cockpit never calls it. No `patch<…>` to a
thread config exists in `api.service.ts`.

### Intent and implementation disagree

`_revalidate_thread_datasource_selection`'s docstring (`main.py:27479`) says
deleted ids "are naturally omitted by `resolve_datasources_for_thread`". The
author believed deletion was a benign drop. The strict count check — correct for
*caller-supplied* lists at create time, where it prevents a uuid-enumeration
oracle — silently made it fatal on the revalidation path, where the ids come from
the user's own persisted row and no oracle exists.

## 2. Three drift families, three inconsistent behaviours

| Family | Checked at | Today's failure |
| --- | --- | --- |
| Project mounts deleted / membership revoked | resume + attach | 403 |
| Connectors deleted / revoked / out of scope | resume + attach | 403 |
| Grants (`vm_workspace`, `shell_tools`, `delegation`, `datasource_tools`, …) | **attach only** | `GrantDenied` → attach returns `False` → session silently never connects |

Grant drift does not even produce a 403 — it hangs at "Connecting…". Unifying
these is the point of this design, not merely fixing the connector case.

This means resume starts checking grants, which it does not do today. Grants are
evaluable at resume time — `resolve_grants_for` needs only the owner and the
project ids — but `evaluate()` wants the *merged* config, not the raw
`metadata.config_override`. `collect_config_drift` therefore has to build the
same merged config attach would, via `_resolve_session_config`. Sharing that
construction with attach is a hard requirement of §3.1: if the collector merges
differently from the enforcer, the dialog will disagree with what actually
happens on attach.

## 3. Design

One prompt enumerating everything that drifted, with two ways forward:

```
Parts of this session's configuration aren't available anymore:

  • Deleted connector — KurortEngine
  • Revoked connector — a connector you no longer have access to
  • Missing grant — Shell tools

[ Start a new session ]        [ Resume without them ]
```

### 3.1 One enumerator, not a fourth copy of the rules

This codebase already has enablement logic computed in two places
(`docs/issues/session_tool_group_enablement_is_computed_in_two_places.md`). The
drift list must therefore be *derived from the code that enforces*, never
restate it.

Today's policy functions collapse everything into one generic raise, so invert
them: the policy layer gains a reporting entry point and the enforcing function
becomes a thin wrapper over it.

```python
async def classify_datasource_selection(...) -> list[ItemVerdict]   # new, per-item

async def authorize_datasource_selection(...):                       # behaviour unchanged
    verdicts = await classify_datasource_selection(...)
    if any(v.denied for v in verdicts):
        raise DatasourceUnavailableError()   # create/PATCH keep the generic raise
    ...
```

One implementation of the rules; the enumerator and the enforcer are two readers
of the same verdicts. The same shape applies to projects, and `evaluate()`
(`src/core/capability_grants.py:170`) already returns per-item strings.

New module `orchestrator/services/config_drift.py`:

```python
@dataclass(frozen=True)
class DriftItem:
    id: str      # stable ack key: "connector:<uuid>" | "project:<uuid>" | "grant:<key>"
    kind: str    # connector | project | grant
    reason: str  # deleted | revoked | out_of_scope
    label: str   # display text; disclosure rules in §5

async def collect_config_drift(db, thread, *, owner) -> list[DriftItem]
```

### 3.2 Acknowledge, do not mutate

`metadata.config_drift_ack: {"<item id>": "<reason>"}`.

Matching is on `id` alone; the reason is stored for audit. A connector that goes
revoked → deleted must not re-prompt, because the effect is identical.

The stored config is never rewritten. If the connector is recreated or the grant
re-issued, the item returns automatically on the next attach — no repair step.
New drift is by definition not in the ack map, so it prompts.

### 3.3 Connectors and grants acknowledge differently

| Kind | What "resume without them" means |
| --- | --- |
| connector / project | Drop it from the selection; the session runs with the rest. |
| **grant** | You cannot "run without a workspace". The offending **config key is neutralised** in the effective merged config and falls back to the platform default: `workspace.backend: vm` → default (sandbox), `tools.shell` → off. |

A grant ack is a *downgrade*, not a removal. That is precisely why the dialog has
to name it rather than applying it silently.

## 4. API contract

### 4.1 Status code: 428, not 409

`POST /resume` already returns 409 for "Thread is already {status}", and the
cockpit deliberately swallows 409. Reusing it would land drift in a catch block
that ignores it. **428 Precondition Required** states exactly what is meant — the
request must carry the acknowledgment precondition — and any client that does not
yet understand it fails loudly instead of silently.

### 4.2 Request

```
POST /api/persistent/threads/{thread_id}/resume
{ "acknowledge": ["connector:d7555d5d-…", "grant:shell_tools"] }
```

The body stays optional; `{}` remains valid and is the no-drift happy path.

### 4.3 Response on drift

```json
428 Precondition Required
{
  "code": "config_drift",
  "detail": "Parts of this session's configuration are no longer available",
  "drift": [
    {"id": "connector:d7555d5d-…", "kind": "connector",
     "reason": "deleted", "label": "KurortEngine"},
    {"id": "grant:shell_tools", "kind": "grant",
     "reason": "revoked", "label": "Shell tools"}
  ]
}
```

### 4.4 Acknowledgment is checked against freshly computed drift

The server recomputes drift on the acknowledged POST and proceeds only if every
currently-drifted id appears in `acknowledge`:

```python
if not {item.id for item in drift} <= set(acknowledge):
    -> 428 with the current list
```

Subset rather than equality, so an item that *recovered* between prompt and
confirm does not force a pointless re-prompt, while an item that newly drifted is
never silently acknowledged.

### 4.5 Order of operations

Validation stays ahead of mutation, as it is today:

1. `require_thread_owner`
2. status must be `ended`, else 409 (unchanged)
3. `drift = collect_config_drift(...)`
4. drift not covered by `acknowledge` → **428**, nothing mutated
5. merge the ack into `metadata.config_drift_ack`
6. `resume_thread()`, then the existing cloud/officer/mount logic unchanged

Attach reads `config_drift_ack` and skips or downgrades acknowledged items.
Non-acknowledged drift still fails closed exactly as today.

Because the acknowledged ids are removed from `selected` *before* resolution,
`_require_exact_datasource_resolution` still sees a matching set and its
fail-closed guarantee is untouched.

### 4.6 Programmatic clients

`src/shared/orch_surface/client.py:2785` calls `raise_for_status()`, so 428 will
raise. `resume_persistent_thread` gains an optional `acknowledge` argument, and
the raised error enumerates the drifted labels so an agent can act on it. The MCP
tool description documents the two-step.

## 5. Disclosure rules

The caller has already passed `require_thread_owner`, so they own the thread and
its persisted config. What must stay hidden is the *current* state of things
belonging to other people.

| Item | Label | Reasoning |
| --- | --- | --- |
| Connector, `deleted` | Its name, from a tombstone (§5.1) | The row is gone; naming it reveals nothing about anyone's present holdings. |
| Connector, `revoked` / `out_of_scope` | Generic: "a connector you no longer have access to" | It still exists and belongs to someone. Naming it would confirm its existence and current name — a real oracle. |
| Project | Generic: "a project you no longer have access to" | Same reasoning. |
| Grant | The grant's own name, from `evaluate()` | A grant is the user's own capability; no third-party information. |

Because revoked items all render the same generic string, two of them would
produce two identical lines. Items sharing a label collapse into one line with a
count ("2 connectors you no longer have access to"). Each still carries its own
`id` in `drift`, so the acknowledgment stays per-item.

### 5.1 Tombstones for deleted-connector names

A deleted row cannot supply its own name, so `delete_datasource` writes a small
append-only tombstone (`id`, `name`, `deleted_at`, `deleted_by`) in the same
transaction in which it scrubs references.

Tombstones only help going forward. The 24 already-bricked threads have no
tombstone, so their deleted connectors render as the bare uuid — the same
placeholder the settings-pane picker already shows for unavailable rows.

## 6. Reference cleanup on delete

Independently of the ack flow, `delete_datasource` should scrub the id from
`threads.metadata.datasource_ids` in the same transaction. The ack flow makes
deletion *survivable*; cleanup stops dangling uuids accumulating in the first
place. Both are wanted: cleanup cannot help sessions whose connector is revoked
rather than deleted, and it cannot retroactively repair the existing 24.

## 7. Failure modes

| Situation | Behaviour |
| --- | --- |
| Drift collection itself throws | Fail closed — 403, as today. Never auto-proceed from an unknown state. |
| Ack names an id that is not currently drifting | Ignored by the subset rule; not an error. |
| Resume double-clicked | Second call sees status != `ended` → existing 409. Unchanged. |
| Thread with no drift | 200, byte-identical to today. The happy path does not change. |
| Item recovers between prompt and confirm | Subset rule proceeds; the recovered item is simply used again. |

## 8. Cockpit changes

1. **Fix the blind catch** at `persistent-chat.service.ts:2333`: discriminate 428
   (open dialog) / 409 (benign, fall through) / anything else (surface an error).
   This alone converts the dead button into a visible failure and is worth
   landing even on its own.
2. **Drift dialog** rendering the list, with "Resume without them" and "Start a
   new session".
3. **"Start a new session"** navigates to session-create prefilled from this
   thread's still-valid config — expert, model, project, surviving connectors —
   so the user does not rebuild it by hand.

`PersistentChatComponent` is unmountable in specs (NG0951), so the
status-discrimination logic must be a pure function tested in isolation. Note
that `tsc -p tsconfig.json` checks nothing in this repo — use `tsconfig.app.json`.

## 9. Testing

**Unit — `collect_config_drift`:** deleted connector, revoked connector, deleted
project, revoked membership, each grant violation, and the empty case.

**Unit — regression guard on the refactor:** `authorize_datasource_selection`
still raises identically for create/PATCH after being rewritten as a wrapper.
This protects the non-enumeration property.

**API:** 428 shape; full ack → 200; partial ack → 428; superset ack → 200; no
drift → 200.

**Attach:** acknowledged connector skipped; acknowledged grant downgraded;
non-acknowledged drift still fails closed.

**Cockpit:** 428 opens the dialog; 409 does not and falls through.

**Live gate on dev:** thread `1930dec9` — resume → 428 naming the dead connector
→ acknowledge → 200 → session attaches and runs with KurortEngine only.

## 10. Out of scope

- Revoking a grant *mid-session*, while the agent is already attached. This
  design covers the resume/attach boundary only.
- A drift badge on session cards before Resume is clicked. `collect_config_drift`
  is a plain function, so exposing it behind a `GET` later is a thin addition
  rather than a rewrite.
- Repairing the 24 existing threads by hand. They are fixed by the same flow
  once it ships, which is the point of testing against `1930dec9`.
