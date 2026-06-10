# Persistent threads — double-provisioning race binds two agents to one thread

**Date:** 2026-05-22
**Status:** **Resolved 2026-05-23** — Approach 1 (advisory lock) + Approach 4 (reject duplicate registrations) shipped on `develop`; verified end-to-end on the dev cluster (one agent per thread observed via `mcp__orchestrator__get_persistent_thread`).
**Component:** `orchestrator/main.py` (thread create path + register endpoint)
**Severity:** High — fresh sessions sometimes silently stop responding to new user messages.

## Resolution

Both call sites (`_provision_or_assign` in `create_thread`, `_reprovision` in `resume_thread`) now wrap the entire body in `async with postgres_db.thread_advisory_lock(tid):` and re-fetch `threads.agent_id` inside the lock; the second arrival observes the binding and exits. The `register_agent` handler takes the same lock and refuses (HTTP 409, deleting the just-registered row) if a *different live* agent already owns the thread. Agent lifespan captures the register return value: on a 409, `_attach_session` is skipped so the orphan pod doesn't compete with the legitimate owner.

The legacy WS-proxy provision call site at `main.py:13892` no longer exists — it was deleted in Plan Task 13 of the direct-session-WebSockets refactor before this fix landed.

See `cockpit/.../persistent-chat.service.ts` (resume flow), `orchestrator/main.py:_provision_or_assign`/`_reprovision`/`register_agent`, `src/api/persistent_app.py` (dedicated_register_ok gate), and `tests/test_orchestrator_client_register.py::test_register_raises_on_409_for_thread_bound` + `tests/test_persistent_app.py::TestExitDuplicateProvisionHelper`.

## Correction (2026-06-10) — the orphan was NOT "harmless", and the race wasn't fully closed in May

A live incident on 2026-06-03 (thread `2c5894c9`) surfaced two errors in the original write-up:

1. **The May 23 advisory lock did not actually close the race.** It re-fetched `threads.agent_id` inside the critical section, but on the fresh-pod path `agent_id` isn't written until the new pod calls `/register` — *after* the lock is released (released early to avoid deadlocking register's own lock acquisition). So two provision paths could still both observe `agent_id IS NULL` and each create a pod, which is exactly what happened on 2026-06-03. This was closed on **2026-06-10** (commit `4830d122`) by writing a timestamped `threads.metadata.agent_pod` marker *inside* the lock at provision time (`agent_provisioner._set_thread_context`) and having both provision entrypoints (`provision_or_assign.py`, `routers/sessions.py`) skip provisioning when `agent_pod_provisioning_in_progress()` is true.

2. **The orphan pod was NOT "harmless until reaped"** (see the corrected line in the Recovery section below). The per-session Service (`session_router.py`) selects pods by the `srw.io/thread-id` label with `publishNotReadyAddresses: true`. The losing pod kept that label, so it stayed a live Service endpoint despite never passing readiness — black-holing ~50% of the cockpit's connection attempts (`curl session-<tid>:8001/ready` from the orchestrator returned a 200/503 mix on 2026-06-03). The "Establishing connection" hang resolved only when a retry happened to land on the winner. Fixed by making the loser **exit cleanly on the 409**: `OrchestratorClient.register` raises `DuplicateThreadBinding` → `persistent_app._exit_duplicate_provision` → `os._exit(0)` (restartPolicy: Never → pod Completed), so it drops out of the Service endpoints immediately instead of lingering. (This supersedes the original "skip `_attach_session`" handling, which de-conflicted the binding but left the orphan in the Service.)

**Still optional (defense-in-depth, not yet done):** make the Service select only the *bound* pod (e.g. a `thread-bound` label stamped after register wins) so a not-ready orphan can never be an endpoint regardless of how it arose. With the two fixes above an orphan should now be both rare and self-evicting, so this is no longer load-bearing.

## Summary

On a brand-new persistent thread, two paths in the orchestrator independently fire `agent_provisioner.provision_agent` when neither sees a bound agent yet:

1. **`POST /api/persistent/threads`** (thread creation) — line 11692, fires `_provision_or_assign` in an `asyncio.create_task`.
2. **`/ws/persistent/{thread_id}` proxy** (cockpit opening the WS) — line 13892, fires `_ws_provision` in an `asyncio.create_task`.

Both check `thread.get("agent_id")` first, but neither writes a placeholder, and the cockpit opens its WS within a few hundred milliseconds of `POST /api/persistent/threads` returning — long before the create-path task has registered an agent and updated `threads.agent_id`. So both call sites observe a NULL `agent_id` and each provisions a pod.

Both pods come up, both call `POST /api/agents/register` with the same `thread_id`, and the register handler at line 10370-10374 unconditionally writes `threads.agent_id = result['agent_id']`. Last writer wins; the loser ends up with `agents.thread_id` still pointing at the thread but `threads.agent_id` pointing at the *other* pod.

That asymmetry is the bug. From the orchestrator's point of view both pods think they own the thread, but the WS proxy that's already been established was dialed to whichever pod was registered first, while subsequent REST forwards (e.g. `POST /input`) go to whichever pod `threads.agent_id` currently points at. When those differ, user messages land in a pod whose persistent loop never started (no WS attached → `handle_persistent_websocket` was never invoked → `_loop_task` was never created at `persistent_app.py:1554`), and the queue at `_loop_user_queue` fills up with messages that nothing drains.

## Symptom (observed 2026-05-22 on dev cluster, thread `a081eeba-f25f-43c2-8e60-ca111b52db70`)

User created a fresh session, waited for the workspace, sent a message. The session card showed "connected", composer was active, but pressing send had no visible effect — no `turn.started`, no thinking, no token. Tried sending two more messages; same silence. No error in the cockpit, no banner, no spinner. Just nothing.

DB state at investigation time:

```sql
SELECT id, hostname, pod_ip, status, thread_id FROM agents
WHERE thread_id = 'a081eeba-f25f-43c2-8e60-ca111b52db70';

                  id                  |       hostname       |   pod_ip    | status  | thread_id
--------------------------------------+----------------------+-------------+---------+-----------
 04292ffd-8661-4254-8eaf-3dc29eb3c477  | srw-agent-s-4bfd8f2b | 10.42.3.153 | session | a081eeba…
 4d5a6cec-f6c1-4979-b370-cfd455909937  | srw-agent-s-c1c30ee4 | 10.42.1.162 | session | a081eeba…
```

```sql
SELECT id, agent_id FROM threads WHERE id = 'a081eeba-…';
                  id                  |               agent_id
--------------------------------------+--------------------------------------
 a081eeba-f25f-43c2-8e60-ca111b52db70  | 4d5a6cec-f6c1-4979-b370-cfd455909937
```

Orchestrator log (filtered to this thread):

```
09:20:33  Thread workspace created: ws-thread-a081eeba-f25
09:20:35  GET /api/persistent/threads/.../messages 200      ← cockpit connect() starting
09:20:35  WebSocket /ws/persistent/... [accepted]           ← cockpit WS opens
09:20:41  agent #1 (04292ffd) registers
09:20:42  agent #2 (4d5a6cec) registers                     ← 1 s later
09:20:54  Proxying WS for thread … to ws://10.42.3.153:8001/ws/chat   ← dial agent #1
09:22:01  POST /api/persistent/threads/.../input 200 (24ms) ← user sends msg 1
09:22:51  POST /api/persistent/threads/.../input 200        ← msg 2
09:23:57  POST /api/persistent/threads/.../input 200        ← msg 3
```

Agent #1's access log shows the WebSocket open from the orchestrator and nothing else:

```
INFO:     10.42.2.200:45892 - "GET /ready HTTP/1.1" 200 OK
INFO:     10.42.2.200:45902 - "WebSocket /ws/chat" [accepted]
```

Agent #2's access log shows exactly the three `/input` POSTs the orchestrator forwarded, but the loop start banner never appears:

```
INFO:     10.42.2.200:46164 - "POST /api/input HTTP/1.1" 200 OK
INFO:     10.42.2.200:56138 - "POST /api/input HTTP/1.1" 200 OK
INFO:     10.42.2.200:57046 - "POST /api/input HTTP/1.1" 200 OK
```

No `Persistent loop started` line for agent #2 — the loop is only spawned by `handle_persistent_websocket` at `persistent_app.py:1554`, which agent #2's WS endpoint was never hit by.

The same pattern shows up earlier the same morning in thread `7a4856a8-…` (two agents `88a701e5` and `e1952041` registered 1 s apart, both bound) and on `505247dd-…` (four registrations queued up over 8 minutes the previous evening, all now offline, all had been bound to the same thread). It's a recurring race, not a one-off.

## Root cause

### The two provisioning call sites

`orchestrator/main.py:11643-11700` — `create_thread` (POST `/api/persistent/threads`):

```python
async def _provision_or_assign(tid, cfg, co, pids, ds_ids):
    idle_agent = await _find_idle_persistent_agent()
    if idle_agent:
        ok = await _send_session_attach(idle_agent, tid, ...)
        if ok: return
    pod_name = await agent_provisioner.provision_agent(
        purpose="session", thread_id=tid, config_name=cfg
    )
    ...

asyncio.create_task(_provision_or_assign(thread_id, ...))
```

`orchestrator/main.py:13846-13892` — `persistent_ws_proxy` (WebSocket `/ws/persistent/{thread_id}`):

```python
if not thread.get("agent_id"):
    if agent_provisioner.is_available:
        async def _ws_provision(tid, cfg, thr):
            idle_agent = await _find_idle_persistent_agent()
            if idle_agent:
                ok = await _send_session_attach(idle_agent, tid, ...)
                if ok: return
            await agent_provisioner.provision_agent(
                purpose="session", thread_id=tid, config_name=cfg
            )

        asyncio.create_task(_ws_provision(thread_id, config_name, thread))
```

Both blocks have the same structure: check `agent_id`, fall through to `provision_agent`. Neither takes a lock, neither writes a placeholder into `threads.agent_id` to claim the slot. When two `asyncio.create_task` calls race on a fresh thread, both win.

The registration path at `main.py:10369-10376` doesn't help — it overwrites `threads.agent_id` unconditionally:

```python
if registration.agent_mode == "persistent" and registration.thread_id:
    try:
        await postgres_db.update_thread_agent(
            registration.thread_id, result["agent_id"]
        )
    except Exception as bind_err:
        logger.warning(f"Thread binding failed (non-fatal): {bind_err}")
```

So the second-to-register agent overwrites the binding the first one wrote.

### The asymmetry between WS proxy and REST forward

After both agents exist, the orchestrator routes by two different mechanisms:

- **WS proxy** (`main.py:14001-14049`): dials `ws://{pod_ip}:{pod_port}/ws/chat` for whichever agent was bound *at the time the WS was accepted*. Once `websockets.connect` succeeds, it relays for the lifetime of the connection — even if `threads.agent_id` later flips to a different pod.
- **REST forward** (`_resolve_thread_for_forwarding` at `main.py:12302-12347`): re-reads `threads.agent_id` fresh from Postgres on every call and dials the current pod.

The WS open is "sticky" to whichever pod was bound first; subsequent register calls flip `threads.agent_id` to point at the other pod. Now `POST /input` routes to the loser and the loop on the winner sits idle.

### Why neither pod can detect the duplicate

`persistent_app.py:_attach_session` (the agent-side lifespan startup) doesn't check whether *another* agent is already bound to the thread. It writes its own thread_id field and proceeds. The agent has no out-of-band channel to discover its peer.

The orchestrator could detect this on the second registration (the thread already has an `agent_id`), but the current code blindly overwrites. The WS proxy could detect this too — the stale-binding recovery at `main.py:13820-13843` checks "is the bound agent offline / ready-without-session" but doesn't check "is there a *different* agent also bound."

## Why this is high-severity

1. **Silent.** No error reaches the cockpit. The composer accepts input, the POST returns 200, but nothing happens. Users perceive it as "the agent stopped working" with no recovery hint.

2. **Both pods cost resources.** Each `srw-agent-s-*` pod is a full FastAPI agent with LLM clients, embeddings, tools, neo4j connection — non-trivial memory and connection-pool footprint. Doubling that per thread amplifies cluster load on every new session that hits the race.

3. **Recovers slowly.** The loser pod's heartbeat keeps it `status='session'` (not idle), so it doesn't get reaped by any pool-eviction logic. It sits there draining resources until something explicit (idle workspace suspension, manual delete, or thread end) tears it down.

4. **Likely contributed to the original disconnect symptoms.** Yesterday's thread `7a4856a8-…` had the same two-agent state. The "WS proxy ended" cycles at 08:02:56 / 08:03:01 / 08:03:46 on that thread could plausibly trace back to the two pods racing on `threads.agent_id`, the WS proxy's stale-binding recovery clearing the binding when the bound agent's `thread_id` field looked off, and the cockpit's chat-page re-init firing connect() on the resulting state churn. See [[persistent_chat_lost_assistant_turn_on_mid_turn_reload]] for the visible-symptom side of that interaction.

## Recovery (live, until a real fix lands)

For a stuck thread, either action works:

**A. Re-point the binding at the agent that holds the WS** (in-place):

```sql
UPDATE threads
SET agent_id = '<agent-id-of-pod-with-active-WS>'
WHERE id = '<thread-id>';
```

Next REST forward goes to the right pod. ⚠️ **Correction (2026-06-10):** the orphan was *not* "harmless until reaped" — until the loser-exit fix landed it stayed in the per-session Service endpoints (`publishNotReadyAddresses`) and black-holed ~50% of connection attempts. See the "Correction (2026-06-10)" section above. With that fix the loser now exits on the 409, so this manual recovery is no longer needed for new sessions.

**B. Kill the orphan pod** (uses existing recovery path):

```bash
kubectl delete pod -n superhuman-remote-worker <orphan-pod-name>
```

`main.py:13820-13843` ("Thread X: bound agent is offline — clearing stale binding") fires on the next WS reconnect, clears `threads.agent_id`, and triggers a fresh attach.

**C. End and recreate the thread.** Cheapest for a session with no user-visible work yet.

## Possible fixes

Listed without prejudgement; not mutually exclusive.

### 1. Per-thread DB row lock around provisioning

Both call sites take an advisory lock keyed on `thread_id` before checking `agent_id` and before calling `provision_agent`. Postgres advisory locks (`pg_advisory_xact_lock`) are cheap and work across orchestrator restarts.

```python
async with postgres_db.acquire() as conn:
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", thread_id
        )
        thread = await ...  # re-fetch agent_id under the lock
        if thread["agent_id"]:
            return  # someone else won
        # ...provision...
```

Pros: structurally race-free; minimal code change. Cons: holds a Postgres transaction open for the duration of `provision_agent` (which can take 10–60 s on a cold image pull) — that's a long-lived lock that blocks anything else trying to acquire it for the same thread.

### 2. Atomic claim via UPDATE … WHERE agent_id IS NULL

Write a sentinel into `threads.agent_id` ("PROVISIONING") in a single UPDATE that succeeds only if it was NULL. The losing caller observes 0 rows updated and bails. Once the agent registers, the sentinel is replaced with the real agent_id.

```python
async with postgres_db.acquire() as conn:
    rows = await conn.execute(
        "UPDATE threads SET agent_id = $2 "
        "WHERE id = $1 AND agent_id IS NULL",
        thread_id, PROVISIONING_SENTINEL_UUID,
    )
    if rows == "UPDATE 0":
        return  # lost the race
```

Pros: lock-free, holds no transaction. Cons: needs a UUID sentinel (the column is a FK to `agents.id` — would need a real "provisioning" row in `agents` or a relaxation of the FK). Schema change.

### 3. Deterministic pod naming + idempotent provision

Today, `agent_provisioner.provision_agent` generates a random pod name (`srw-agent-s-<random>`). If pod name were deterministic on `thread_id` (e.g. `srw-agent-s-<thread-id-prefix>`), the second `provision_agent` call would observe the pod already exists and become a no-op.

Pros: no DB lock, no schema change, recovery story stays the same. Cons: K8s pod-name uniqueness becomes a load-bearing invariant — if the previous session's pod is still terminating, the new provision fails. Solvable (wait for terminating pods to clear, or include a generation suffix), but adds complexity.

### 4. Reject duplicate registrations in `register_agent`

On the second `POST /api/agents/register` for the same `(thread_id, agent_mode='persistent')`, return 409 and have the agent shut itself down. The orchestrator already has the data it needs in the `agents` table query.

Pros: stops the split-brain at the point of binding. Cons: by the time the second agent registers it's already booted (cost of starting the pod was paid); the orchestrator still needs to clean up the orphan pod. And this doesn't prevent the WS proxy from dialing the wrong pod if the timing flips — it just makes the orphan visible.

### Recommendation

Approach **1 (advisory lock)** is the smallest surface-area change and structurally race-free. The long-held lock concern (10–60s during image pull) is real but only matters if a *second* attempt at the same thread arrives during provisioning — and we *want* the second attempt to wait, since racing was the bug. The lock holder can also release before the pod is fully booted (release after writing a sentinel into a fast path), keeping the critical section short.

Approach **4 (reject duplicate registrations)** is worth doing *anyway* as defense-in-depth, regardless of which primary fix lands. It costs near-zero and turns a silent bind-overwrite into a loud 409 in the agent's startup logs.

## What's NOT the fix

- **Adding more cleanup on the orphan side.** Detecting and reaping the loser pod is a workaround for a state that shouldn't exist. Fix the race that creates it; don't normalize having two pods per thread.
- **Removing the WS-proxy provisioning fallback.** That fallback exists for the case where the cockpit re-opens a thread whose `create_thread` provision task failed or whose agent went offline. It's load-bearing for resume flows; the goal is to make it observe an in-progress provision rather than to delete it.
- **Cockpit-side workaround (e.g. wait longer between create and WS).** Doesn't address the root cause and just shifts the race window. Resume flows would still hit it.

## Open questions

1. Does `agent_provisioner.provision_agent` currently have any internal idempotency we can lean on, or does it always create a new pod? (Read `services/agent_provisioner.py` before designing the fix.)
2. Is `persistent_provisioner.create_agent_pod` (the legacy path at `main.py:12217`, `:13261`, `:13898`) on the same race? It looks structurally similar but goes through a different module.
3. Are there other thread states (resume from suspended, magic-link wake at `:13261`) where the same race fires?

## References

- `orchestrator/main.py:11679-11700` — `create_thread` provisioning task.
- `orchestrator/main.py:12302-12347` — `_resolve_thread_for_forwarding` (uses `threads.agent_id`).
- `orchestrator/main.py:13820-13843` — WS proxy stale-binding recovery (doesn't catch duplicates).
- `orchestrator/main.py:13846-13892` — WS proxy provisioning fallback.
- `orchestrator/main.py:14001-14049` — WS proxy main loop (the "sticky" dial).
- `orchestrator/main.py:10344-10379` — `register_agent` (the unconditional `update_thread_agent`).
- `src/api/persistent_app.py:1540-1554` — loop only starts on first WS connect.
- `src/api/persistent_app.py:1379-1401` — `handle_api_input` (REST input handler — puts into `_loop_user_queue`).
- [[persistent_chat_lost_assistant_turn_on_mid_turn_reload]] — the visible-symptom side; the reducer drops events when split-brain causes mid-turn reconnects.
