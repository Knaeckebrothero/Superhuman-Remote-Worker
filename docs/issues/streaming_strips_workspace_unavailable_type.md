# Streaming path strips `workspace_unavailable` error type — dead-workspace jobs hard-fail instead of recovering

**Status: FIXED (this doc's fix implemented; see §Fix)**
**Found: 2026-07-17**, diagnosing dev-cluster job `4eba7f2f-3e24-4b52-82c3-1929ce8c6771`
("Design the UI theme and complete mockup suite for Hotel Rheinland ERP"), which
died after 2 days / 10,055 audit entries with
`SSH command failed on 100.64.25.224: ChannelException(2, 'Connect failed')`.

## Symptom

A VM-backed job whose workspace dies mid-run ends `failed` with the raw
SSH error as `error_message`, and the completion-side cleanup **deletes the
VM** (snapshot skipped — tailnet unroutable from the orchestrator). The
designed outcome for exactly this failure is the `/complete` recovery arm:
pause → delete dead VM → reprovision → resume from checkpoint.

## Root cause

The workspace-unavailable design propagates the failure **by exception
type**: `RemoteBackend` raises `WorkspaceUnavailableError`, the tool node
re-raises it (`_handle_tool_errors_reraise_workspace`), and
`agent.py:process_job`'s `except` classifies it into a typed error dict
(`{"type": "workspace_unavailable", "recoverable": true}`) that the
orchestrator's `/complete` recovery arm routes on.

That `except` only guards the **non-streaming** (`ainvoke`) path. In
streaming mode `process_job` *returns the generator immediately*;
exceptions raised during **iteration** propagate out of
`_process_job_streaming` (which was `try/finally` with no `except`) into
the app layer's `async for`, whose generic `except Exception` reported

```python
{"error": {"message": str(e)}}
```

— no `type`, no `recoverable`. The orchestrator's recovery arm checks
`error.get("type") == "workspace_unavailable"`, sees a plain error,
skips recovery, marks the job `failed` via `determine_job_status`, and
step-7 cleanup (`_archive_and_cleanup_workspace` → `release_vm`) tears
down the workspace. Worker jobs run in dual mode (streaming), so the
typed path effectively never ran for them.

Four app-layer sites had the untyped shape: `dual_app._run_job`,
`dual_app` resume, `app.py` job run, `app.py` resume.

## Fix (implemented)

1. **Primary — `src/agent.py:_process_job_streaming`**: added an
   `except Exception` that mirrors the non-streaming classification and
   **yields the typed error state** instead of letting the exception
   escape the generator. The app layer then reports it through its
   normal completion path with the type intact.
2. **Defense-in-depth — `completion_error_payload()`** in
   `src/core/workspace_backend.py`: canonical builder for the
   `/complete` error dict (preserves `type`/`recoverable` via
   `isinstance`). All four app-layer `except` sites now use it, so any
   exception that still escapes a generator (or is raised by the app
   layer itself) keeps its classification.

Not changed: orchestrator `/complete` (its routing is correct once the
type arrives); `CancelledError`/`GeneratorExit` handling (both are
`BaseException`, untouched by the new `except Exception`).

## Acceptance

- A `WorkspaceUnavailableError` raised mid-stream reaches `/complete` as
  `error.type == "workspace_unavailable"` → job **pauses** and the VM/pod
  recovery arm runs (no terminal `failed`, no `release_vm` teardown).
- A generic exception mid-stream still fails the job (`type: job_error`).
- Regression tests: `tests/test_streaming_error_type.py`.

## Related

- `docs/done/search_files_full_repo_grep_wedges_ssh_tool_node.md` — L2
  produces exactly this exception type on wedge escalation; before this
  fix its freeze → re-dispatch intent silently degraded to a hard fail
  on streaming workers.
- `docs/issues/agent_workspace_pod_resource_headroom.md` — the VM-side
  death (sshd refusing new session channels, daemon heartbeat stopped)
  that triggered the incident is the resource-pressure problem, not this
  bug; this bug turned it from a ~5-minute recovery into a lost job.
