---
tags:
  - issue
  - fix-spec
  - orchestrator
  - database
  - cockpit
---

# `isinstance(context, dict)` guards that never parse: always-False branches and at least three dead code paths

**Filed:** 2026-07-27, found while auditing persistence conventions for the
verification redesign.
**Status:** Mechanism CONFIRMED in code. Individual instances tagged below —
some verified dead by inspection, one needs a live check. UNFIXED.
**Severity:** medium — no data loss, but features that appear implemented are
silently inert, and one path raises.
**Component:** `orchestrator/database/postgres.py:400-418`, plus the call
sites listed below.

## The mechanism

The app Postgres pool registers **only** the pgvector codec on new
connections (`orchestrator/database/postgres.py:402-418`) — there is no
`jsonb` codec. asyncpg therefore returns `jsonb` columns as **raw JSON
strings**, and `get_job` hands the record over unparsed
(`postgres.py:900`: `return dict(row) if row else None`).

The **audit** pools *do* register a jsonb codec
(`orchestrator/database/audit_store.py:91-99`,
`src/database/audit_writer.py:90-99`), so the two databases behave
differently — audit reads give dicts, app reads give strings. That asymmetry
is itself a trap for anyone moving code between them.

Most call sites handle it: there are roughly 51 inline
`isinstance(x, str) → json.loads` blocks and at least nine independently
named module-local coercion helpers (`_parse_freeze_data`, `_coerce_jsonb`,
`_job_context`, `_coerce_context`, `_json_object`, `_mapping`,
`_thread_metadata`, and two different `_metadata`), all reimplementing the
same three lines with three different `except` tuples.

The bug class is the sites that **guard without parsing**:

```python
vm_ctx = context.get("vm") if isinstance(context, dict) else None
```

Since `context` is a `str`, the `isinstance` is always False, the expression
always evaluates to `None`, and the code below it is unreachable. The guard
looks defensive and is in fact a permanent off-switch.

## Instances

### 1. `GET /api/vms/{job_id}` always 404s — VERIFIED by inspection

`orchestrator/main.py:10205-10208`:

```python
context = job.get("context") or {}
vm_ctx = context.get("vm") if isinstance(context, dict) else None
if not vm_ctx:
    raise HTTPException(status_code=404, detail=f"No VM context for job '{job_id}'")
```

`job` comes from `require_job_access`, i.e. the app pool. The endpoint can
never return VM status — including the `?live=true` NATS request/reply path
below it.

### 2. VM freeze-on-pause is unreachable — VERIFIED by inspection

`orchestrator/main.py:9139-9144`:

```python
vm_ctx = (
    (job.get("context") or {}).get("vm")
    if isinstance(job.get("context"), dict)
    else None
)
if vm_ctx:
    await vm_provisioner.send_control(job_id, "freeze")
```

Pausing a VM-backed job never sends the freeze control to the management
daemon. This is consistent with the independently observed "VM suspend now
dead" state noted in `srw_session_vm_never_attaches.md`, and may be its
cause — worth checking before hunting elsewhere.

### 3. Subjob repo provisioning raises `AttributeError` — VERIFIED reachable, needs live confirmation

`orchestrator/services/job_provisioning.py:172-176`:

```python
parent = await postgres_db.get_job(parent_job_id)      # :142
...
"git_remote_url": parent.get("context", {}).get("git_remote_url", ""),
```

Chained on one expression, so no `isinstance` guard is even possible: if
`context` is a string (or SQL NULL), `.get` raises. There is no `try`/`except`
in the enclosing block, and `POST /api/jobs` calls it unguarded
(`main.py:8645`); the project-loop path at `:12592` does wrap it.

**Why this is not constantly firing:** the critic path open-codes its own
branch provisioning rather than calling `provision_job_repo` — see
`docs/issues/unify_scholar_critic_subjob_provisioning.md`, which tracks that
duplication as a separate defect. Unifying them, as that doc proposes, would
route the critic straight into this exception. **Fix this first, or the
unification will regress subjob creation.**

### 4. Cockpit snapshot status is always `undefined` — VERIFIED by inspection

The API deliberately preserves the raw string shape across the wire:
`result[field] = json.dumps(cleaned) if was_str else cleaned`
(`orchestrator/main.py:8099`). So `context` reaches the browser as a string,
and `cockpit/src/app/views/job-review/job-review.component.ts:698`:

```typescript
currentJob.context?.['snapshot']?.['status']
```

indexes a string with a non-numeric key → always `undefined`. There is no
client-side `JSON.parse` of `context` anywhere in `api.service.ts` or
`data.service.ts`.

The established convention for surfacing a JSONB sub-key to the UI is a
**derived scalar projected in SQL**, not shipping the blob — e.g.
`j.context->'snapshot'->>'status' AS snapshot_status`
(`orchestrator/database/postgres.py:840`). Note `get_visible_jobs` does not
select `j.context` at all.

## Why not just register a global jsonb codec?

Tempting, and it would fix all four at once — but it inverts the assumption
at ~51 sites that currently test `isinstance(x, str)` first. Those would all
start taking their else-branch, which is *usually* correct but has not been
audited. A global codec is a one-line change with a repo-wide blast radius
and no test coverage to catch a regression.

Safer sequence:

1. **Fix the four sites above individually** — small, verifiable, no blast
   radius.
2. **Consolidate the nine coercion helpers into one** shared utility with a
   single `except` tuple, and use it at every new read site. This is a
   prerequisite for any new JSONB key, including the
   `verification_rounds` record proposed in
   `verification_round_reset_spawns_blind_critic.md`.
3. **Then** evaluate the global codec as a separate, tested change, with the
   `isinstance(x, str)` sites converted to use the shared helper first so
   they become no-ops rather than behaviour changes.

## Related notes

- The hazard is documented in at least five places
  (`docs/done/job_cloud_export.md:114`,
  `docs/done/2026-06-18-user-defined-experts-slice-2-enforcement.md:541` —
  which flags `bool("false") == True` as a **silent privilege escalation**,
  `docs/done/config_override_credential_leak.md:78`, and two superpowers
  plans) but **never in `AGENTS.md`**, so nothing puts it in front of someone
  writing a new read site. Adding it there is the cheapest preventive step.
- `docs/features/database_roadmap.md:380-382` cites stale `postgres.py` line
  numbers for the atomic-context helpers; the file has drifted.

## Related

- `docs/issues/unify_scholar_critic_subjob_provisioning.md` — instance 3
  blocks it.
- `docs/issues/verification_round_reset_spawns_blind_critic.md` — the audit
  this came out of; its Slice 2 adds a new JSONB read site.
