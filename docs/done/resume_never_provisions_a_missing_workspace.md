---
tags:
  - issue
  - orchestrator
  - dispatcher
  - workspace
  - recovery
---

# Resume ships a workspace-backed job at an agent without provisioning anything — a job whose VM/container never came up gets a backend with no address and dies at `init_workspace`

**Status:** FIXED 2026-07-27 — committed in `9bb24cea` (swept in alongside an
unrelated retention-sweeper change), pushed, and an ancestor of the deployed
image `sha-5eb436e`. Job `4435994d` did recover and ran to `reviewing`.
**Not yet confirmed that the Resume button itself exercised the fix** — the job
was also recoverable by a manual `context - 'vm'` + status reset, and the two
are distinguishable only by whether `context.last_vm` is present (the shed
stashes; the manual SQL did not). Worth checking on the next occurrence.
**Severity:** high — every workspace-backed job that dies *before* its workspace
exists is unrecoverable from the UI, and clicking the obvious button makes it
worse (see "Collateral damage").
**Component:** `orchestrator/main.py` — `_resume_job_on_agent` (VM injection
~2889, container injection ~2915), `POST /api/jobs/{job_id}/resume`;
`orchestrator/database/postgres.py` `shed_workspace_context`.
**Same agent-side symptom, different root cause:**
`docs/issues/subjob_inherits_stale_workspace_container_snapshot.md` (subjob
copies the parent's `workspace_container` by value). This is a **third**
instance of that symptom string — do not assume the subjob diagnosis.

**Motivating incident:** job `4435994d` had just failed VM provisioning
(`docs/done/job_description_newline_breaks_vm_template_render.md`). Resume was
clicked at 2026-07-27 08:01. It failed again, differently:

```
workspace.backend='vm' but no workspace.remote config was provided.
The orchestrator must inject SSH credentials pointing at a provisioned
workspace container or VM.
```

## Root cause — a missing `else`

`_resume_job_on_agent` injects the SSH address only when the workspace is
already live:

```python
vm_ctx = _get_vm_context(job)
if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
    ws["backend"] = "vm"
    remote = ws.setdefault("remote", {})
    remote.setdefault("host", vm_ctx["ssh_host"])
```

There is no `else`. When the workspace is not live the injection is **silently
skipped**, but the job's own `config_override` still says
`workspace.backend='vm'`. The agent receives a backend with no host to dial and
raises at `src/agent.py:1896`. The container tier (~2915,
`context.workspace_container`) has the identical guard-with-no-else, so both
tiers are affected.

**Nothing in the resume path provisions anything — only the dispatcher does.**
Resume exists to reattach to a workspace that already exists; it has no
provisioning step anywhere in it. So resume is not "broken for VM jobs": it
cannot recover a job that never got a workspace, which is a different thing and
the reason the button looked like it should work.

## Why re-queueing alone was not enough

This is the half that is easy to miss. `queue_job_for_resume` only **merges**
context, so a parked `context.vm` survives the re-queue. The dispatcher then
reads `status: 'failed'` → `decide_vm_action` returns `VM_PARKED`
(`orchestrator/services/dispatch_guards.py:125`) → `_fail_vm_parked_job` →
instant re-fail with the *original* message. Recovery requires shedding the
stale context as well, which is exactly what the park error tells a human to do
by hand ("clear context.vm and re-queue the job").

## Collateral damage

The failed resume **overwrites `jobs.error_message`**, replacing the actionable
provisioning diagnostic with a confusing downstream symptom. On `4435994d` the
original YAML error survived only because `context.vm.error` happened to retain
a copy. Anyone without that would have lost the trail entirely.

## Fix shipped

1. `_resume_missing_workspace(job) -> 'vm' | 'sandbox' | None` — pure predicate,
   mirroring the `_resume_reject_should_requeue` precedent so it is directly
   unit-testable.
2. Guard in `_resume_job_on_agent`, **before any DB or HTTP work**: refuses
   (`False` is that function's existing "caller should queue" contract) and
   sheds the stale context. Covers all three call sites — the Resume endpoint,
   the dispatcher (~5878) and `POST /api/jobs/{job_id}/assign/{agent_id}`, which
   now returns 502 instead of a silently broken agent run.
3. Pre-flight in the `resume_job` endpoint, **before agent selection** — sheds
   and queues without picking an agent at all. The ordering is load-bearing: the
   "no agents available" branch returns early, so a shed placed after it would
   leave the parked context in place and the dispatcher would just re-park.
4. `shed_workspace_context(job_id, key)` — drops `context.<key>`, **stashing**
   the old value as `context.last_<key>`.

Shedding also resets the retry budget: `provision_attempts` lives inside
`context.vm`, and the container tier's `needs_create` keys off an absent status.

The dispatcher's `VM_PARKED` → fail behaviour was deliberately **not** touched.
It exists to stop hot-retry loops against the shared VM cluster; resetting is
the explicit, user-initiated resume's job, not the dispatch loop's.

## Design notes / gotchas

- **Why a stash, not a delete.** `delete_job_context_keys` already exists and
  would have been the obvious reuse, but dropping the key outright destroys
  `context.vm.error` — usually the only surviving record of why provisioning
  failed, precisely because a failed resume clobbers `error_message`. The stash
  to `last_<key>` mirrors `queue_job_for_resume`'s `last_freeze_data`.
- **Livelock avoided.** An earlier draft sheds only in the endpoint. The
  dispatcher also calls `_resume_job_on_agent` and sheds nothing, so the guard
  alone would have bounced such a job every tick, refusing each time and never
  rebuilding. The guard therefore sheds too, making every caller self-healing.
- **`_job_needs_sandbox` short-circuits to `False`** the moment a container
  claims `ready`, masking a ready-but-address-less container — the exact state
  that strands an agent. The predicate checks the *explicit* backend first, then
  falls back to the needs-predicates.
- **The shed swallows its own errors.** The guard runs outside
  `_resume_job_on_agent`'s try/except and the dispatcher's call site does not
  wrap it, so an unhandled DB error there would take out the dispatch loop.
- **Sibling backstop.** `_dispatch_job_to_agent` (~2573) already refuses a
  workspace-backed job with no remote, but *fails* it. Correct there — the
  dispatcher was supposed to have resolved the workspace — and wrong for resume,
  where the user explicitly asked to re-provision.

## Tests

- `tests/test_resume_missing_workspace.py` — predicate across both tiers, lite
  exemption, JSON-string context/override, and the guard (asserting
  `resolve_datasources_for_job` is never reached, since asserting only the
  return value passes vacuously — the function already returned `False` via its
  broad `except`).
- `tests/test_shed_workspace_context.py` — real Postgres via testcontainers:
  key dropped, old value stashed, siblings untouched, idempotent.
- `tests/test_resume_endpoint_delegation.py` — endpoint sheds the right key per
  tier, queues instead of delegating, preserves feedback, and still resumes
  healthy jobs directly.

## Related

- `docs/done/job_description_newline_breaks_vm_template_render.md` — the
  provisioning failure that exposed this.
- `docs/done/job_resume_direct_path_skips_credential_injection.md` — the
  previous Resume-button bug in the same endpoint.
- `docs/issues/session_vm_backend_never_attaches.md` — session-side cousin.
