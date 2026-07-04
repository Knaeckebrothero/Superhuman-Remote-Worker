---
tags:
  - issue
  - fix-spec
  - jobs
  - workspace-lifecycle
  - agent-resilience
  - remote-backend
---

# Fix spec — agent fast-freezes on a dead workspace (P1 / Issue 5)

**Status:** Designed 2026-07-04, not yet implemented. Work on `develop`.

**What this fixes:** when a workspace (pod or VM) becomes unreachable mid-run, the
agent must raise `WorkspaceUnavailableError` so the job freezes cleanly as
`workspace_unavailable` — **quickly** (seconds) and **by exception type** (not by
string-matching). Today it does neither: the error is flattened into an ordinary
tool-result string, so the model retries it forever, and each failed attempt burns
~15 min in a nested connect-retry storm. Critic `e7e6971f` made ~21 tool calls over
~39 min against a reaped pod instead of freezing (see incident in the canonical doc).

**This is P1 in the ranked chain.** It is a standalone *resilience* fix — it does
**not** fix the reaper that triggered the 2026-07-04 incident (that is P0), nor the
stuck-`reviewing` parent (P2). It makes workspace loss **from any cause** (reap,
node drain, eviction, OOM, stale-IP churn) survivable instead of a silent hang.

**Boundary — recovery already exists, we only have to reach it:**
`orchestrator/main.py:10589` already consumes `error.type == "workspace_unavailable"`:
pod jobs delete the dead pod, keep the PVC, re-dispatch for reattach + checkpoint
resume (capped at `WORKSPACE_RECOVERY_MAX_ATTEMPTS=3`, then **fail loud**); VM jobs
take the legacy VM-recovery arm. P1 builds none of that — it only makes the death
reliably and quickly *land* on `agent.py:1012` (`error_state{type: workspace_unavailable,
recoverable: True, should_stop: True}`).

**Two graph paths — both must be covered.** Jobs run `src/graph.py` (ToolNode); the
interactive **chat** session runs a *separate* hand-rolled loop in
`src/persistent_graph.py` (these have diverged before — see the RemoveMessage-strip
bug). `persistent_graph` has its **own** tool-exec flatten
(`persistent_graph.py:1818` — `except Exception: result_str = f"Tool execution error: {e}"`),
so a graph.py-only fix would leave chat sessions with the *identical* spin. Part 1 must
patch both. (Good news: `persistent_graph.py:645-671` already wraps the turn in a
handler that surfaces a propagated exception via `callbacks.on_error` + persists it —
so once the inner flatten is guarded, chat surfaces cleanly with **no crash**.)

**Related:**
[`reviewing_parent_pod_reaped_under_critic.md`](reviewing_parent_pod_reaped_under_critic.md)
(canonical chain — this is its Issue 5 / Fix #4+#6) ·
[`critic_failure_leaves_parent_job_stuck_reviewing.md`](critic_failure_leaves_parent_job_stuck_reviewing.md)
(June Bug 2/3 origin) ·
[`loop_job_workspace_lost_wedged_in_recovery.md`](loop_job_workspace_lost_wedged_in_recovery.md)
(the recovery consumer this feeds)

---

## Root cause (confirmed in code)

**Two independent failure levers; the "cheap half" only pulls one.**

### Lever A — the flatten (why the job never freezes)
The detection chain is: tool raises → tool swallows into a string → `graph.py:4063`
decides "is this a workspace death?" by `if "WorkspaceUnavailableError" in msg.content`.
The swallowed strings never contain the class name, so the watchdog misses and the
model treats it as a normal failed command → retries indefinitely.

Swallow sites (blanket `except Exception as e: return f"…: {e}"` — `str(e)` is the
message only, no class name):
- `src/core/backends/remote.py:897` — `shell_run` poll loop:
  `except WorkspaceUnavailableError: return f"SSH connection lost during command execution: {command}"`
- `src/tools/workspace/filesystem.py` — every file op (read/list/delete/search/move/
  rename/copy/mkdir/rmdir/…): blanket `except Exception as e: return f"Error …: {e}"`
- `src/tools/workspace/files.py` — same pattern
- `src/persistent_graph.py:1818` — the **chat** turn loop's tool exec:
  `except Exception: result_str = f"Tool execution error: {e}"`
- (sweep the tool layer during impl; the T1/T5 tests below catch any missed site)

Verified nothing external depends on the flattened strings (only `remote.py` defines
them; the only substring control-flow in `graph.py` is the `WorkspaceUnavailableError`
match we replace). Safe to remove.

### Lever B — the retry storm (why even a clean freeze is ~15 min late)
`connect()` (`remote.py:214`) retries `max_retries=5` internally with backoff, and
`_ensure_connected()` (`remote.py:280`) **wraps `connect()` in another `max_retries=5`
loop** → up to **25 attempts × `connect_timeout=30s`** ≈ ~15 min for a *single*
dead-workspace tool call. The retries exist to tolerate the sshd boot window (daemon
registration → sshd ready), but they fire identically for a pod that is **gone**
(`NXDOMAIN` — DNS won't resolve) as for one that is merely **booting**.

---

## Design (Tier B — true fast-freeze)

### Part 1 — Stop the flatten (type-based propagation)
Replace the fragile string-match with real exception propagation so **any**
backend-touching tool freezes cleanly.

1. **Un-swallow at the tool layer.** Before each blanket `except Exception`, add a
   guard that re-raises the workspace error:
   ```python
   except WorkspaceUnavailableError:
       raise
   except Exception as e:
       return f"Error …: {e}"
   ```
   Apply across `remote.py:897` (delete the string-return), `filesystem.py`,
   `files.py`, and any other tool module that calls the `WorkspaceBackend`. Mechanical,
   grep-guided; ~15–20 sites. Safe: `connect()` exhausts its retry budget *before*
   raising, so by the time the error escapes a tool the workspace is genuinely gone.
2. **ToolNode: propagate the type.** `graph.py:3785` change
   `ToolNode(tools, handle_tool_errors=True)` → a callable that **re-raises
   `WorkspaceUnavailableError`** and formats every other exception with the existing
   default template. Now the error propagates out of `tool_node.ainvoke`, bubbles
   through the run_tools node → out of `self._graph.ainvoke` (`agent.py:1000`) → caught
   by the `isinstance` at `agent.py:1012` → `error_state{type: workspace_unavailable}`.
3. **Delete the dead string-match.** Remove `graph.py:4057-4069`
   (`if "WorkspaceUnavailableError" in msg.content`) — now superseded by type propagation.
4. **Cover the chat path (`persistent_graph.py`).** (a) Guard the tool-exec flatten at
   `1818`: `except WorkspaceUnavailableError: raise` **before** the blanket `except
   Exception`. (b) Add a `WorkspaceUnavailableError` branch to `_user_facing_turn_error`
   (`216`) with an actionable message ("Your workspace became unavailable and is being
   recovered — resend to reconnect."). The existing turn handler at `645-671` then
   catches the propagated error → `on_error` + persist → the turn ends cleanly instead
   of the model spinning. No new handler needed.

> **Impl note — propagation mechanism + fallback.** Step 2 relies on LangGraph's
> `handle_tool_errors` callable being able to *re-raise* (the callable runs outside
> ToolNode's catch, so raising propagates). If the pinned LangGraph version doesn't
> propagate a raising callable, fall back to: keep `handle_tool_errors=True` (its
> default template stringifies with `repr(e)`, which **does** contain the class name)
> and **keep** the `graph.py` string-match block. This still works once step 1 makes
> the tools re-raise instead of returning a bare message — the difference is only
> whether detection is by type (preferred) or by the class name in `repr`. T1 is the
> gate either way.

Payoff: **resilient to workspace loss from any cause** — shell, SFTP file I/O,
`exec_command`, git — not just the one shell path.

### Part 2 — Make the freeze fast (bound the retry storm)
1. **De-nest.** `_ensure_connected` calls `connect()` **once** (connect owns the whole
   retry budget); drop its outer `for attempt in range(max_retries)` loop. Kills the
   `max_retries²` blowup and centralizes retry policy in one place.
2. **Classify the cause & fail fast on "gone."** In `connect()`'s except handler, branch
   on the error:

   | Cause | Meaning | Retries |
   |---|---|---|
   | `socket.gaierror` (NXDOMAIN, "Name or service not known"); `OSError` errno `EHOSTUNREACH`/`ENETUNREACH` (no route / net unreachable) | pod **gone** | **0 — raise immediately** |
   | `ConnectionRefusedError` (ECONNREFUSED — sshd not up yet) | **booting** | full `max_retries=5` × 30s (keep the ~150s boot-window tolerance) |
   | `socket.timeout` / `TimeoutError`; `paramiko.SSHException`; other `OSError` | **ambiguous** | small cap (**2**) then raise |

   Rationale: a name that won't resolve is a destroyed pod, not a booting one — the
   reaper/`NXDOMAIN` case fails in seconds. `ECONNREFUSED` is exactly what the retries
   were built for, so it keeps the full budget. Timeouts/protocol errors are ambiguous
   → a short cap avoids both a false-fast and a 15-min stall.

Result: a dead-workspace tool call raises `WorkspaceUnavailableError` in ~seconds →
clean freeze → orchestrator recovery.

> **Shared by both paths.** Part 2 lives in `RemoteBackend.connect()`/`_ensure_connected()`,
> which every tool call goes through — jobs *and* chat. So the fast-fail covers the chat
> path for free; only Part 1's flatten fix needs the chat-specific step above.
> **Safe at startup:** the chat setup path (`persistent_session.py:385-423`) wraps
> `connect()` in its own 5-min outer retry loop that catches `Exception` regardless of
> cause, so `connect()` fast-failing on a transient boot-window `gaierror` just makes the
> outer loop iterate faster within the same budget — no early give-up. The fast-fail only
> bites the mid-run `_ensure_connected` path, which is exactly the target.

### Part 3 — Rename the misleading "VM" strings (P3 / Issue 2)
`RemoteBackend` is the single SSH backend for **both** sandbox pods and VMs; hardcoding
"VM" cost real triage time on the incident. Rename the agent/log-facing strings to
"workspace":
- `remote.py:222-224` `"Failed to connect to VM {host}:{port} …"` → `"… to workspace {host}:{port} …"`
- `disconnect()` `"Disconnected from VM {host}"` / `_init_shell`/`_check_blocked`
  "requires a VM runtime" → "workspace" / "container runtime"

Scope: strings that surface to agents/logs. Do **not** churn internal comments or the
`vm_*` config keys — "VM" is still correct for the legacy VM tier.

---

## Testing (TDD — write the failing test first)

- **T1 — the regression that would have caught this.** A fake `WorkspaceBackend` whose
  **file read** (and separately, a shell call) raises `WorkspaceUnavailableError` →
  drive the graph/agent path → assert it yields
  `error_state{type: "workspace_unavailable", recoverable: True, should_stop: True}`,
  **not** a retryable `ToolMessage`. Exercising a *file* op (not just shell) makes the
  test fail if any tool-layer swallow site is missed.
- **T2 — de-nest.** A dead-host `connect()`/`_ensure_connected()` makes exactly N attempts
  (not N²) and raises within a bounded wall-clock.
- **T3 — classify.** `gaierror` → raises immediately (0 retries); `ConnectionRefusedError`
  → retries up to `max_retries`; `socket.timeout` → retries up to the small cap (2).
- **T4 — rename.** The surfaced `WorkspaceUnavailableError` message says "workspace",
  not "VM".
- **T5 — chat path.** In `persistent_graph`'s turn loop, a tool raising
  `WorkspaceUnavailableError` propagates out of `_execute_turn` and reaches the turn
  handler (→ `on_error`), rather than being flattened into a retryable `ToolMessage`;
  `_user_facing_turn_error` returns the workspace-recovery message (not a raw string).

## Out of scope
- **P0** — reaper reaps the parent pod under a live critic (the incident trigger):
  [`reviewing_parent_pod_reaped_under_critic.md`](reviewing_parent_pod_reaped_under_critic.md) §Fix.
- **P2** — `reviewing`/`pending_review` timeout watchdog for stuck parents.
- **P4** — unseeded V2 workspace (separate signature, needs a repro first).
- A turn/exec-level deadline for hangs that **don't** raise `WorkspaceUnavailableError`
  (zombie pod, SSH open but unresponsive) — deferred; overlaps P2.
- **Live chat-session workspace auto-recovery** (reconnect/reprovision the pod for an
  in-flight chat session without a resend). P1 makes chat *stop spinning and surface
  cleanly*; auto-reprovisioning a live session needs its own design (the job-path
  recovery machinery doesn't directly apply) — fast-follow.
