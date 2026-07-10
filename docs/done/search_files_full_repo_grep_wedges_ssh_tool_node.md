---
tags:
  - issue
  - jobs
  - agent-lifecycle
  - workspace
  - remote-backend
  - ssh
  - tools
  - watchdog
  - livelock
---

# Parallel full-repo `search_files` greps can wedge the SSH tool node for hours, invisibly — the async heartbeat keeps firing so orchestrator orphan-detection never fires

**Status:** RESOLVED 2026-07-10 — all four layers (L0–L3) implemented,
lint-clean (`ruff` on all touched source), and unit/integration-tested (271
targeted tests green). Source committed in `de393722` (L0 `exclude_dirs`) and
`78493022` (L1 socket timeouts + L2 tool-batch timeout + L3 graph-progress
heartbeat). **Remaining:** test files are staged but uncommitted, and the *live*
k3d acceptance checks (force a real wedge, confirm timeout→reconnect→re-dispatch
and the stall-detector pause) are not yet run — see "Loose ends". Root cause was
a workspace SSH stall inside a **sync** tool call that the app-level `_exec`
deadline did not catch; the exact below-`_exec` block point was never reproduced
(see "Open questions") — L1 hardens it defensively. Safety is enforced by the
timeout layers (L1/L2) + observability (L3), **not** by restricting search scope.
As-built mapping below; original design in "Fix plan".

**Motivating incident:** job `2dbe6854-8b4f-4c63-9315-f761076cd7e1` — "Design a
UI for the Hotel ERP System", dev cluster `main`, image `sha-194cdf2`.
At iteration 73 (tactical phase 3) the `execute` LLM (`gpt-5.5`, audit #1591)
issued **5 parallel `search_files`** calls, two of which grep the entire cloned
repo root:

```
search_files({"path": "repos/Superhuman-Remote-Worker", "query": "CLI"})
search_files({"path": "repos/Superhuman-Remote-Worker", "query": "command-line"})
```

The tool node then blocked for **~8h10m** (11:21:50Z → 19:32:53Z) with all 5
calls stuck `pending`. The job stayed `processing` at `0.0%` the entire time. It
self-recovered only because an unrelated `version_upgrade` drain re-dispatched
it onto a fresh pod at 19:32.

## TL;DR

`search_files` → `RemoteWorkspaceBackend.search_files` runs a server-side
`grep -rn` over the target path (`src/core/backends/remote.py:706-731`). The
grep **excludes only binary file *extensions*** (`pdf,docx,png,jpg,gif,zip,db`)
— it has **no `--exclude-dir`** and **no `-I`**, so grepping the repo root
descends into `cockpit/node_modules`, `.git/objects/*.pack`, `venv/`, etc. Two
of those in parallel (plus three more) is a pathological load on the workspace
pod, and the SSH session wedged.

Because `search_files` is a **synchronous** tool executed in LangGraph
`ToolNode`'s thread pool, the blocked thread does **not** stall the agent's
asyncio event loop — the **5-second heartbeat kept posting normally**. The
orchestrator only orphan-pauses agents that *stop* heartbeating
(`agents marked offline after 3min`), so from its view the agent was healthy.
Net result: an 8-hour zero-progress stall that was invisible to every existing
watchdog. `get_job_progress` reporting `0.0%` was the only tell.

## Symptom

| Fact | Value |
|---|---|
| Job | `2dbe6854-8b4f-4c63-9315-f761076cd7e1` — "Design a UI for the Hotel ERP System" |
| Wedge window | **11:21:50Z → 19:32:53Z (~8h10m)** |
| Last audit before wedge | #1603 `memory_retrieve` @ 11:22:28Z (async observer draining) |
| First audit after wedge | #1609 `phase_complete` @ 19:32:53Z |
| Blocked calls | #1594–98 → 5× `search_files` (`pending` the whole window) |
| Results finally land | #1604–08 `Tool [ok] search_files` @ ~19:32Z (on the fresh pod) |
| Job status throughout | `processing`, `get_job_progress` = `0.0%` |
| Agent | stayed `working` + heartbeating the entire 8h (never orphaned) |

The 5 pending calls being audited as *issued but not completed* — while the
earlier iteration-0 tools (`read_file`/`list_files`/`kb_search`) show
`Tool [ok]` — is the fingerprint of a tool node blocked mid-batch.

## Mechanism (the full chain)

1. **Heavy grep.** `execute` issues 5 `search_files`; 2 target the repo root.
   The remote command (`remote.py:727-730`) is
   `grep -rni --exclude='*.pdf' … -- '<query>' <repo_root> 2>/dev/null | head -n 2000 || true`
   with **no directory excludes and no `-I`**. Over `node_modules` +
   `.git/objects` this recursion is enormous.
2. **SSH stall.** Run 5-in-parallel against one workspace pod, the SSH
   session wedges at a level *below* `_exec`'s guards. `_exec` is otherwise
   well-hardened: 60s wall-clock deadline, drains stdout+stderr, closes the
   channel on timeout (`remote.py:406-473`; see
   `remote_backend_indefinite_wait_deadlock.md`). The stall therefore sits in
   the connection layer, not the drain loop — most likely a stale-but-`active`
   transport (`is_connected()` returns `transport.is_active()`,
   `remote.py:362-366`; paramiko `set_keepalive(15)` *sends* keepalives but does
   not enforce a response deadline, so a black-holed TCP stays "active").
3. **Sync tool → thread pool.** `ToolNode` (`src/graph.py:3841`,
   `audited_tools` at `graph.py:3909`) runs the sync `search_files` in a worker
   thread. The block is confined to that thread.
4. **Heartbeat masks it.** The dual-mode heartbeat is an independent asyncio
   task; it kept posting every 5s, so the agent stayed `working`. Orphan
   detection (offline after 3min without heartbeat → auto-pause for
   re-dispatch) **never triggered**. The job sat `processing` / `0.0%`,
   invisible, for 8 hours.
5. **Accidental rescue.** At 19:32 a `version_upgrade` drain (a dev image
   rollout) set drain intent; the orchestrator paused the job, cleared
   `assigned_agent_id` and shed the freeze blob (`orchestrator/main.py:12277+`),
   and re-dispatched onto a **fresh pod**. That pod resumed from the LangGraph
   checkpoint, re-ran the 5 searches on a healthy SSH connection (they returned
   instantly, #1604–08), and the job flew through the phase boundary. It is
   healthy and progressing now.

## What this is NOT

- **Not** the `version_upgrade` drain livelock
  (`version_upgrade_drain_livelock.md`). That failure churns
  `processing ⇄ paused` every 1–2 min with an advancing `updated_at`; here the
  audit trail was **dead silent** for 8h — a genuine execution block, and the
  drain was the *cure*, not the disease.
- **Not** a `grep`-timeout error. `RemoteCommandTimeoutError` is not a
  `WorkspaceUnavailableError`, so a real 60s grep timeout returns a normal
  error string from `search_files` within a minute — not an 8h silent hang.

## Diagnosis gotcha

`get_frozen_job` reported `freeze_type: version_upgrade, phase: strategic,
phase_number: 4` — but the live run was **phase 3 tactical**, per the audit
trail. Treat `get_frozen_job`'s `version_upgrade` as synthesized/unreliable
(cf. `vm_ssh_readiness_probe_unroutable_from_orchestrator.md`) and trust the
`get_audit_trail` timestamps + DB `freeze_data`.

## Implementation (as built) — RESOLVED 2026-07-10

Commits: `de393722` (L0), `78493022` (L1 + L2 + L3). `ruff` clean on all touched
source; 271 targeted tests green across `test_search_files_summary`,
`test_workspace_backends`, `test_stuck_detection`, `test_stale_agent_detector`,
`test_api_agent_metrics`, `test_orchestrator_client`, `test_internal_auth`.

- **L0 — DONE.** `exclude_dirs: list[str] | None = None` threaded through tool
  wrapper (`filesystem.py:336`), `WorkspaceManager` facade (`workspace.py:728`,
  forwarded at `:745`), abstract base (`workspace_backend.py:195`), `remote.py:775`
  (builds shell-escaped `--exclude-dir` args at `:795-800`), `subdir.py:123`
  (forwards to parent), `virtual.py:290`, `scratch.py:138`, and all three test
  stubs. Default `None` = search everything, unchanged. Covered by
  `test_search_files_forwards_exclude_dirs_to_workspace`,
  `test_search_with_exclude_dirs_includes_flags`,
  `test_search_escapes_single_quotes_in_exclude_dirs`.
- **L1 — DONE (defensive; unreproduced).** `connect()` sets `SO_KEEPALIVE`,
  `TCP_KEEPIDLE`/`INTVL`/`CNT`, and `TCP_USER_TIMEOUT=10s`
  (`_TCP_USER_TIMEOUT_MILLIS`) on the transport socket (`remote.py:331-391`). A
  black-holed TCP now aborts in ~10s instead of hanging indefinitely.
- **L2 — DONE.** `_get_batch_tool_timeout` (max per-category cap from
  `config.limits.tool_category_timeouts`, clamped to `_TOOL_BATCH_TIMEOUT_SECONDS`
  outer ceiling; `graph.py:3908`) wraps `tool_node.ainvoke` in `asyncio.wait_for`
  (`graph.py:4152-4181`). On timeout → `_reconnect_workspace` (`disconnect()` +
  `connect()`) + tool-error `ToolMessage`; **second consecutive** timeout or
  reconnect failure → `WorkspaceUnavailableError` → `agent.py:1058` freeze
  `workspace_unavailable` → orchestrator pause + re-dispatch. Retry counter resets
  on any successful batch.
- **L3 — DONE.** `ToolContext.next_graph_progress` / `get_graph_progress`
  (`context.py:231-242`) incremented per completed batch (`graph.py:4188`);
  surfaced as heartbeat metric `graph_progress` (`app.py`, `dual_app.py`,
  `persistent_app.py`); stamped `graph_progress_seen_at` on change
  (`postgres.py:2686`); consumed by `mark_stalled_working_agents_by_graph_progress`
  (`postgres.py:3798`) via `stale_agent_detector` (`main.py:625`). Closes the
  "heartbeating but wedged" blind spot that made this incident invisible for 8h.

### Loose ends

- **Tests uncommitted.** `tests/test_workspace_backends.py`,
  `test_stuck_detection.py`, `test_internal_auth.py`, `test_orchestrator_client.py`
  (modified) and `test_api_agent_metrics.py`, `test_stale_agent_detector.py` (new)
  are unstaged; source is committed. Commit them before/with the next push.
- **Live k3d acceptance not yet run.** Only unit/integration tests were executed.
  The "Live" acceptance bullets below (force a real hang → confirm
  timeout→reconnect→re-dispatch for L2, and the graph-progress stall pause for L3)
  remain to be exercised on the cluster.
- **L2 reconnect runs on the event loop.** `_reconnect_workspace` calls blocking
  `disconnect()`/`connect()` directly in the async handler — now bounded by L1's
  `TCP_USER_TIMEOUT` (~10s) but could be offloaded to a thread for full
  event-loop safety. Minor.
- **L1 efficacy vs. the original incident is unverified** (never reproduced). If
  it recurs, the L2/L3 backstops will catch it in minutes rather than hours.

## Fix plan (layered — safety lives in the timeout layers, NOT in exclusion)

**Design decision (2026-07-10):** search scope is not a safety mechanism.
Excludes are **not** applied by default — the agent keeps full, unrestricted
search (including `.git`/`node_modules`/large trees/vendored code) exactly as
today. Not-wedging is enforced entirely by bounding *how long* any tool may run
(L1/L2) plus making stalls observable (L3). This keeps agent capability intact
while removing the hang-forever failure mode. (Rejected alternative: default
`--exclude-dir` — it silently removes the ability to search those dirs, and
puts safety in the wrong layer.)

### L0 — optional, agent-controlled `exclude_dirs` (capability, not a guardrail)

Add an optional `exclude_dirs: list[str] | None = None` parameter.
**Default `None` → no `--exclude-dir`, search everything — today's behavior,
unchanged.** When the agent passes e.g. `["node_modules", ".git"]`, each becomes
a `--exclude-dir` arg so it can focus a noisy search *itself*. Advertise it in
the tool docstring as a way to speed up / narrow searches over large repos. It
is an affordance for the model, never a default restriction.

**Full file surface — `search_files` is defined in 7 source spots + 3 test
stubs. The kwarg must thread through ALL of them or it is silently dropped: the
`WorkspaceManager` facade currently hard-codes a 3-arg delegation, and any
backend/stub whose signature the facade forwards to must accept it.**

- `src/tools/workspace/filesystem.py:332` — tool wrapper: add param + docstring;
  pass it in the `workspace.search_files(...)` call (currently 3 kwargs).
- `src/core/workspace.py:723` — `WorkspaceManager` facade: add param and forward
  to `self._backend.search_files(...)`. **Easy to miss — the current body is
  `return self._backend.search_files(query, path, case_sensitive)` and drops any
  extra kwarg.**
- `src/core/workspace_backend.py:190` — abstract base: add to the signature.
- `src/core/backends/remote.py:706` — **the only backend that uses it.** Build
  `--exclude-dir='<d>'` args next to the existing `excludes` block
  (`remote.py:721-730`), **shell-escaping each value exactly like `query`**
  (`d.replace("'", "'\\''")`) — they interpolate into the grep command string.
- `src/core/backends/subdir.py:118` — **delegates to `self._parent.search_files`;
  must forward the new kwarg** or it's dropped for subdir-wrapped workspaces.
- `src/core/backends/virtual.py:285`, `src/core/backends/scratch.py:133` — add
  the param; may honor it or accept-and-ignore (prod backend is always `remote`).
- Test stubs that must also accept the param: `tests/_fs_backend.py:104`
  (`FilesystemTestBackend`, drives WorkspaceManager tests),
  `tests/test_search_files_summary.py:24`, `tests/cloud_mount/test_workspace_cloud_guard.py:16`.

(Optional, orthogonal: add `-I` to skip binary files, or leave binaries
searchable — same principle, don't restrict by default.)

### L1 — transport-level deadline (most likely root cause — see Open Questions)

`_exec`'s 60s wall-clock deadline guards its read loop, but the 8h block sat
*below* it — most likely a `recv()` on a black-holed TCP with no OS socket
timeout. **Not yet reproduced**, so treat L1 as defensive hardening whose exact
efficacy against this incident is unconfirmed; it is still correct on its own
merits (a dead SSH transport should never hang a tool indefinitely). `set_keepalive(15)` only *sends* keepalives; paramiko does not tear the
transport down when responses stop. Fix: set `TCP_USER_TIMEOUT` / `SO_KEEPALIVE`
(or a socket timeout) on the transport socket in `connect()` so the kernel
aborts a dead connection in seconds and `recv()` raises instead of hanging. This
makes every SSH tool self-heal, not just `search_files`.

### L2 — tool-node wall-clock backstop (catch-all) — *the highest-leverage fix*

Wrap the batch execution at `src/graph.py:4099`
(`await tool_node.ainvoke(state)`) in a wall-clock cap. Caveats that make it
actually work rather than just reshape the hang:

- **Python can't cancel the worker thread.** The sync tool runs off-loop —
  proven here by the heartbeat surviving 8h — so `asyncio.wait_for` unblocks the
  graph but leaves the thread + SSH connection wedged. On timeout we **must**
  tear down and reconnect the workspace backend (`disconnect()` → `connect()`);
  closing the transport also lets the leaked `recv()` error out and the thread
  return to the pool. Without this, the next call re-wedges → repeated 15-min
  stalls + slow thread-pool exhaustion.
- **Per-category caps, not a flat number.** Search/read/write/list/kb/git should
  finish in seconds → cap ~60–120s so a wedge fails fast. Only long-runners
  (`run_command` builds/tests) get minutes. Use ~15 min as the absolute outer
  ceiling, not the default — a 15-min invisible stall still holds a workspace
  pod and burns budget.
- **Graduated escalation.** First timeout in a phase → reconnect + return a tool
  error `ToolMessage` (cheap retry on a healthy connection). Repeat, or reconnect
  fails → raise `WorkspaceUnavailableError`, which already flows
  `_handle_tool_errors_reraise_workspace` → `agent.py` → freeze
  `workspace_unavailable` → orchestrator pause + re-dispatch to a fresh pod. That
  is exactly the path that coincidentally rescued this job — L2 just triggers it
  deliberately instead of waiting 8h for an image rollout.
- **Batch caveat + how to reconcile with per-category:** line 4099 runs all N
  calls as one `ainvoke`, so the wrapper can only apply ONE timeout to the whole
  batch. Simplest correct approach: cap the batch at the **max category timeout
  among the calls present** (a batch mixing `search_files` + `run_command` gets
  the longer ceiling). True per-call caps require moving the timeout into the
  backend layer (`_exec` already has one). A batch-level timeout loses the fast
  results when one call hangs — acceptable for a backstop.
- **Config surface:** add the timeout knob to `LimitsConfig`
  (`src/core/loader.py:1481`, parsed at `loader.py:~2234` and `~2447`), with a
  default in `config/defaults.yaml:~231` and schema in `config/schema.json:~523`
  (alongside `progress_stall_threshold` / `max_tool_calls_per_phase`). Resolve a
  call's category via the existing `_get_tool_category` /`TOOL_REGISTRY`
  (`src/graph.py:3879`).

### L3 — progress-aware heartbeat (visibility)

Independent of timeouts: have the heartbeat carry a "last audit / graph-progress"
marker and let the orchestrator pause an agent that is heartbeating but has made
**no progress** for M minutes. This closes the general "alive but wedged" blind
spot — the thing that made this incident cost 8h instead of minutes — regardless
of which tool wedges or why.

### Ordering

L0 is trivial and independent (ship anytime — pure capability add, no behavior
change by default). **L2 (with reconnect + escalation) is the highest-leverage
safety fix** and the general backstop. L1 is the durable root-cause fix and
protects every SSH tool. L3 is the observability backstop for the next
unknown-unknown wedge.

## Acceptance criteria / verification

Per the repo's "verify locally before committing" gate (CLAUDE.md). Each layer
is independently shippable.

- **L0.** Unit: `exclude_dirs=None` produces a grep command with **no**
  `--exclude-dir` (byte-identical to today); `exclude_dirs=["node_modules",".git"]`
  produces one `--exclude-dir='…'` per entry, values shell-escaped; the kwarg
  survives the `WorkspaceManager` facade and `subdir` passthrough (assert the
  backend received it). Live: a `search_files` from a cockpit session still
  returns results (default path) and a narrowed search excludes the named dir.
  `pytest tests/test_search_files_summary.py -x` + any `test_remote*`/workspace
  search tests stay green.
- **L1.** Unit is hard without a black-hole; assert the socket options are set on
  the transport after `connect()` (`TCP_USER_TIMEOUT`/`SO_KEEPALIVE` present).
  Manual: `iptables -j DROP` the workspace SSH port mid-`_exec` (or kill the
  workspace pod) and confirm the tool call errors within the configured window
  instead of hanging. Mark efficacy-against-incident as "unverified" until repro.
- **L2.** Unit: monkeypatch a tool to `time.sleep(cap+ε)`; assert (a) the graph
  coroutine returns within ~cap, (b) `disconnect()`+`connect()` were called on
  timeout, (c) first timeout → tool-error `ToolMessage`, (d) repeat/reconnect-fail
  → `WorkspaceUnavailableError` raised → `agent.py` sets freeze type
  `workspace_unavailable` (`src/agent.py:1058`). Live: force a hang on the k3d
  cluster and confirm the job pauses + re-dispatches instead of stalling.
- **L3.** Unit: heartbeat payload carries a monotonically-advancing progress
  marker; orchestrator flags no-progress-for-M-min. Live: induce a wedge and
  confirm the orchestrator pauses the agent on the progress timeout (not just the
  3-min heartbeat-absence path).

## Open questions

- **Exact block point below `_exec`.** From code, `connect()` (bounded
  `connect_timeout=30` + capped retries) and `_exec`'s read loop (60s deadline)
  are both bounded. An 8h silent hang implies the thread was stuck in
  `exec_command` channel-open over a stale-`active` transport, or in a
  `recv()` that never became ready and never errored, or the workspace pod was
  hung at the OS/network level (D-state / node pressure). Needs a repro with
  paramiko transport logging, or a py-spy dump of a live wedged agent thread.
- **Did the 2 full-repo greps alone cause the stall, or did pod resource
  pressure?** Worth checking workspace pod CPU/mem around 11:21Z if metrics are
  retained.

## References

- `src/core/backends/remote.py:706-731` — `search_files` remote grep (the
  trigger; also where the L0 `exclude_dirs` param threads through)
- `src/core/backends/remote.py:406-473` — `_exec` hardened deadline (does not
  cover this block)
- `src/core/backends/remote.py:362-366`, `:329` — `is_connected()` /
  `set_keepalive`
- `src/graph.py:3841`, `:3909` — `ToolNode` / `audited_tools`
- `src/graph.py:3263-3290` — `version_upgrade` drain freeze at phase boundary
- `orchestrator/main.py:12277+` — paused-job agent-clear + freeze-shed on
  auto-redispatch
- Related: `remote_backend_indefinite_wait_deadlock.md`,
  `agent_fast_freeze_on_dead_workspace.md`, `version_upgrade_drain_livelock.md`
