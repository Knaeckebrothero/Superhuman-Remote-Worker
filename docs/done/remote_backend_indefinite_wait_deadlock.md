---
tags:
  - issue
  - fix-spec
  - jobs
  - workspace-lifecycle
  - agent-resilience
  - remote-backend
---

# Fix spec — RemoteBackend `_exec` channel-window deadlock wedges jobs forever

**Status:** DONE — implemented, reviewed ("Ready to merge"), pushed and
verified working 2026-07-10/11. Commits (post-push SHAs):
`fb1a1992` (drain loop + `RemoteCommandTimeoutError`), `cc5fbfce` (heavy-op
timeouts), search cap + `a0131d4f` (capped summary only at exact cap),
`ab90cc8a` (keepalive + SFTP timeout + honest read_file timeout), `74865bb6`
(SFTP timeouts no longer masquerade as missing paths; drain deadline binds
under sustained output). The detection-net non-goal has since landed
separately — see Non-goals.

**What this fixes:** any `_exec` command whose output exceeds paramiko's channel
window (2 MiB default) deadlocks the agent thread **permanently**, wedging the
job in `processing` while heartbeats keep it looking alive. The same
"no deadline anywhere" defect exists on the SFTP side (`read_file`/`write_file`
hold `_sftp_lock` with no channel timeout and no transport keepalive). This
spec makes every RemoteBackend wait bounded.

**Relationship to P1:** `agent_fast_freeze_on_dead_workspace.md` covers
*connect-time* failures by exception type. This spec covers *established-channel*
hangs — the complement. It deliberately does **not** freeze the job on a
command timeout (a slow grep is not a dead workspace; see §Timeout semantics).

## Incident (2026-07-10, main cluster)

Job `2dbe6854-8b4f-4c63-9315-f761076cd7e1` ("Design a UI for the Hotel ERP
System", loop tactical job) sat in `processing` for ~8 h with zero LLM
iterations. Diagnosis chain, all live-verified:

- Iteration 97 (2026-07-09 22:32:10Z) issued `read_file` + 5 × `search_files`.
  Audit shows the `Tool Call:` entries but **no matching `Tool [ok]` results**,
  and no LLM request after doc 36050.
- py-spy dump of agent pid 7 (pod `srw-agent-j-5389665f`): thread parked in
  `recv_exit_status (paramiko/channel.py:400)` ← `_exec (remote.py:386)` ←
  `search_files (remote.py:616)`, locals showing `query: "role"`,
  `timeout: 60`, and the Event wait at `timeout: None`.
- On the workspace VM: the grep (pid 3478) alive for 8 h 16 m, state S,
  wchan **`pipe_write`** — blocked writing output nobody reads.
- Measured output of that grep: **2,319,835 B** — ~10 % over paramiko's
  2,097,152 B default window. Sibling queries (`permissions`, `settlement`,
  `wallet`, `retention`) measured 5–90 KB, which is why the tool "usually
  works".

**Unstick (runbook):** kill the remote command's process on the workspace VM.
The `bash -c '… || true'` wrapper converts signal death to exit 0, the exit
status reaches the channel, `_exec` returns the buffered output, and the job
resumes with no work lost. Verified: iteration 98 completed 46.8 s after the
kill.

## Root cause

`_exec` (src/core/backends/remote.py:378) calls
`stdout.channel.recv_exit_status()` **before** reading stdout:

1. `recv_exit_status()` waits on a `threading.Event` with **no timeout** — the
   `timeout=` passed to `exec_command()` only arms the socket timeout used by
   `read()`, which is never reached.
2. Output larger than the channel window fills paramiko's in-memory buffer;
   window credit is only returned by reading, which never happens. The remote
   command blocks on `pipe_write`, can never exit, and the exit status the
   agent is waiting for is never sent. Mutual deadlock.

### Secondary effects (both observed)

- **Invisible to `get_stuck_jobs`:** the heartbeat thread keeps refreshing the
  job row, so `updated_at`-based staleness never fires.
- **Blocks deploy drains:** the 04:55Z drain intent ("freeze at next phase
  boundary") never fired because the graph never reached a boundary — the
  wedged job pinned a stale-image agent indefinitely.

## Fix design

### 1. `_exec` drain loop (core)

Replace the `recv_exit_status()` → `read()` sequence with one loop over the
channel:

```python
chan = stdout.channel
out, err = [], []
deadline = time.monotonic() + timeout
while True:
    while chan.recv_ready():
        out.append(chan.recv(65536))
    while chan.recv_stderr_ready():
        err.append(chan.recv_stderr(65536))
    if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
        break
    if time.monotonic() > deadline:
        chan.close()
        raise RemoteCommandTimeoutError(...)
    time.sleep(0.05)
exit_code = chan.recv_exit_status()  # already ready; returns immediately
```

- **Drain stderr too**: extended-data (stderr) shares the channel window;
  draining only stdout leaves the sibling deadlock reachable.
- **Wall-clock deadline** checked each 50 ms poll; `_exec` runs in executor
  threads, so the blocking sleep is fine.
- On deadline: `chan.close()` (frees the remote side) then raise.
- **Output cap**: stop accumulating past 5 MiB, append an
  `[output truncated at 5 MiB]` marker, keep draining to let the command
  finish. Prevents agent-RAM blowups from runaway commands.
- `recv_exit_status()` is only called once ready, so it cannot block.

### 2. Timeout semantics

- New `RemoteCommandTimeoutError` — **must not** subclass
  `WorkspaceUnavailableError`. A slow command is not a dead workspace; tripping
  the P1 fast-freeze path would freeze whole jobs over one heavy query.
  It falls through to the tools' generic `except Exception` and surfaces to
  the model as an ordinary tool error ("command timed out after 60s"), which
  the model can adapt to (narrow the query, different path). If the workspace
  is genuinely gone, the next operation's connect path classifies that and
  freezes correctly.
- **Timeouts now actually bind**, which is a behavior change: heavy ops that
  silently ran long on the 30 s default must get explicit generous values in
  the same commit — `rm -rf` / `cp -a` → 300 s, `mv` / `du` → 120 s. tmux ops
  stay 30 s; `search_files` stays 60 s.

### 3. `search_files` output cap

Append `| head -n 2000` to the grep pipeline (display cap is
`max_search_results` = 50, so 40× headroom; ~400 KB worst case). When the cap
is hit, the summary line reads "2000+ matches (capped)" instead of a
false-precision total. `head`'s exit code (0) is the pipeline's; grep's
SIGPIPE death is harmless under the existing `|| true`.

### 4. SFTP + transport hardening

After `connect()`:

- `transport.set_keepalive(15)` — a dead connection now errors instead of
  waiting forever.
- `self._sftp.get_channel().settimeout(60)` — bounds every SFTP read/write.
  (Per-socket-op timeout, not total: large-but-flowing transfers are fine.)

Care point: `read_file` converts `IOError` → `FileNotFoundError`, and a socket
timeout **is** an `OSError`/`IOError` — catch `socket.timeout` before that
conversion and surface it as "workspace I/O timed out", not a misleading
"file not found". This matters doubly because these ops hold `_sftp_lock`; a
hung SFTP call wedges *all* file operations, not just one.

### 5. Rejected alternative: graph-level tool timeout

`asyncio.wait_for` around tool execution was considered and rejected: tools
run in executor threads, threads cannot be cancelled, and an abandoned thread
may hold `_sftp_lock` — trading one wedge for a worse one. Deadlines belong at
the I/O layer where the blocking happens.

### 6. Testing

Extends the existing paramiko-mock pattern in `tests/test_workspace_backends.py`:

- **Deadlock regression**: mock channel that signals exit-status-ready only
  after the buffer is drained — fails (times out) on current code, passes with
  the fix.
- Timeout path raises `RemoteCommandTimeoutError` and closes the channel.
- stderr is drained; large stderr does not stall the loop.
- Output-cap truncation marker.
- Built grep command contains the `head` cap; capped summary line renders.
- `set_keepalive` + SFTP `settimeout` applied on connect.
- `read_file` SFTP timeout surfaces as I/O-timeout error, not
  `FileNotFoundError`.

CI is the gate (local env is noisy — see repo practice).

### 7. Rollout

Normal `develop` → CI → agents cycle out via drain. No config, no schema, no
orchestrator changes. Wedged pre-fix agents can be unstuck with the runbook
above.

## Non-goals (separate slices) — status as of move to done/

- **Detection net: LANDED separately** (`03675d28`, 2026-07-10): heartbeats
  now carry `graph_progress`, and
  `mark_stalled_working_agents_by_graph_progress` transitions agents stuck
  in `working` without graph progress back to `ready` after a configurable
  stall window — closing the "heartbeat masks a wedged graph" blind spot
  this incident exposed (a wedge like job `2dbe6854` now self-detects
  instead of sitting invisible for 8 h).
- **Drain watchdog:** largely superseded — the graph-progress stall handler
  releases a pinned agent by marking it `ready`, and with I/O deadlines now
  binding, silent graph wedges should no longer occur. Revisit only if a
  drain is ever again observed blocked for hours.
- **Small deferred cleanups** (cosmetic, from final review): invalidate the
  shared SFTP session after a `socket.timeout` (a late response on the
  still-open channel can desync some paramiko versions); honest timeout
  messages in `delete_file`/`_get_home_dir` (currently raw `TimeoutError`,
  loud but unpolished); structural "capped" signal from the backend instead
  of inferring from `total == SEARCH_RESULT_HARD_CAP` (unparseable grep
  lines can drop the capped notice).
