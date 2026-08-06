---
tags:
  - issue
  - fix-spec
  - jobs
  - workspace-lifecycle
  - agent-resilience
  - remote-backend
---

# Fix spec — parallel tool burst exceeds sshd MaxSessions and masquerades as workspace death


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** All four slices shipped in `821c4359` — the same commit that filed this doc: channel semaphore, ChannelException reclassification before blanket SSHException, MaxSessions 16, recovery-arm probe-before-punch + counter reset; tests/test_workspace_recovery_probe.py 17/17 green live.

**Status:** Designed 2026-07-24 from the 2026-07-23 incident (job `52949749`,
"historische Kernwerke"). Work on `develop`.

## Incident

The job failed with `workspace unavailable; recovery exhausted after 3 attempts:
SSH command failed … ChannelException(2, 'Connect failed')` — but the workspace
pod was **healthy at every step**. Forensic chain (all times UTC, evidence in
`workspace_intervals`, `jobs.freeze_data`, and the per-pod agent logs archived
under `srw-snapshots/agent_logs/`):

1. **22:34:36** — during post-critic remediation, the LLM returned **14 parallel
   tool calls**. The agent executes tool batches concurrently; every remote
   operation opens an SSH *session channel* on the single shared paramiko
   transport. OpenSSH's default **`MaxSessions 10`** (we set no override) made
   sshd refuse the next open: `Secsh channel 145 open FAILED: open failed:
   Connect failed`.
2. `RemoteBackend._exec`'s blanket `except paramiko.SSHException` converted the
   refusal (`ChannelException` is an `SSHException` subclass) into
   `WorkspaceUnavailableError` → the agent reported `workspace_unavailable`.
   Proof of misdiagnosis: the same agent successfully initialized a remote
   shell on the same pod 400 ms *after* the report.
3. The orchestrator's G1 recovery arm **deleted the healthy pod** (PVC kept)
   and re-dispatched. The LangGraph checkpoint sits *on* the 14-call response,
   so every resume replayed the burst ~2 s after connecting (`Secsh channel 10
   open FAILED` — the 11th channel on a fresh transport) and refailed
   identically. Three attempts burned in 103 s → fail loud.
4. Collateral: the attempt-4 workspace pod booted into orphanhood (leaked,
   Running for 9+ h); the critic subjob is stuck `waiting` forever.

The failure is **deterministic on resume** — retrying without a fix reproduces
it exactly.

## Fix (4 slices)

### A. Channel semaphore — `src/core/backends/remote.py`

A `threading.Semaphore` owned by `RemoteBackend`, size
`WORKSPACE_SSH_MAX_CONCURRENT_CHANNELS` (default **10**), acquired around every
**short-lived** session-channel open (`_exec`; any per-operation SFTP opens)
and released in `finally` after an explicit `chan.close()` so the server-side
slot frees promptly. The 11th concurrent exec queues for milliseconds instead
of being refused; the model observes nothing.

Long-lived channels (persistent SFTP client, shell tabs, canvas forward
channels) are deliberately *not* semaphore-managed — they are the headroom
budget: ≤10 execs + ~2–4 persistent < 16 (slice C).

Layer choice rationale: tool-call-level caps can't hold the invariant (one
tool call may open several channels) and there are **three independent tool
fan-out sites** (`src/graph.py` ToolNode, `src/persistent_graph.py` chat loop,
`src/tools/delegation/light_runner.py`); the backend is the single seam that
covers them all.

### B. `ChannelException` reclassification — `src/core/backends/remote.py`

In `_exec`, catch `paramiko.ChannelException` **before** the blanket
`SSHException` arm:

- transport **alive** (`transport.is_active()`) → channel refusal, not pod
  death → retry the exec up to 3× (0.25 s / 0.5 s / 1 s). Retries exhausted
  with a live transport → raise an ordinary error (tool-result to the model),
  **never** `WorkspaceUnavailableError`.
- transport **dead** / socket error / EOF → `WorkspaceUnavailableError`
  exactly as today (real pod deaths keep freezing fast, per the P1 fast-freeze
  spec).

### C. `MaxSessions 16` — sshd configs

Added to the sshd block in `docker/Dockerfile.workspace` and its stated mirror
in the VM provisioning script. Running workspaces keep the old limit until the
image rolls; acceptable on dev.

### D. Recovery-arm hardening — `orchestrator/main.py` (G1 pod path)

Scoped to three cheap guards (readiness gate + inter-attempt backoff deferred
to a follow-up):

1. **Probe before punch:** on a `workspace_unavailable` report, the
   orchestrator TCP-probes `workspace-<id>:30022` (~3 s). Probe **succeeds** →
   the pod is not dead: skip the delete, keep the warm pod, pause +
   re-dispatch. The attempts counter still increments either way, so a
   pathological report-loop stays bounded at the same cap. Probe fails →
   delete + re-dispatch as today. Requires the workspace NetworkPolicy to
   admit orchestrator→workspace:30022 (verify; a false-negative probe degrades
   safely to current behavior).
2. **Counter reset:** any handled completion that is *not*
   `workspace_unavailable` resets `recovery_attempts` to 0 — today the counter
   only ever increments, so one recovered blip poisons the job forever.
3. **No leak on fail-loud:** the `attempts > cap` branch deletes the
   just-provisioned pod (PVC retained) before marking the job failed.

## Out of scope / follow-ups

- Readiness gate + backoff between recovery attempts (matters for real pod
  deaths; rarer).
- Cancel a `waiting` critic when its parent hard-fails (why job `51a4ce11`
  shows Waiting forever).
- Ops: delete leaked pod `workspace-52949749-618`, cancel the stuck critic,
  then salvage job `52949749` (reset `freeze_data.recovery_attempts`,
  re-dispatch — the PVC still holds all remediation work).

## Forensics crib (how to spot this class)

`paramiko.transport` log line `Secsh channel N open FAILED: open failed:
Connect failed` while the transport stays alive = **session-limit refusal from
sshd**, not pod death. A dead pod drops the transport (socket error/EOF); it
never sends a clean `CHANNEL_OPEN_FAILURE`. Cross-check `workspace_intervals`
(delete/recreate churn at recovery timestamps) and the absence of a suspension
snapshot in S3.
