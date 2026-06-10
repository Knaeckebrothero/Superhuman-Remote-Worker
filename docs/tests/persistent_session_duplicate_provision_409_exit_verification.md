# Persistent session — loser-exits-on-409 (duplicate provision) — Verification Runbook

Verifies the fix that makes a **losing agent pod exit cleanly when it loses the
thread-provisioning race** (orchestrator returns HTTP 409 "thread already bound
to another live agent"), so it drops out of the per-session Service endpoints
instead of lingering as an orphan that black-holes ~50% of the cockpit's
connection attempts.

Background:
- Memory: `project_ngsw_buffers_sse.md` (Cause 4 — the publishNotReadyAddresses ×
  double-provision interaction, found live on 2026-06-03, thread `2c5894c9`).
- Root-cause + history: `docs/done/persistent_thread_double_provisioning_race.md`
  (see the "Correction (2026-06-10)" section).

**Status:** Fix implemented on `develop` (uncommitted as of 2026-06-10),
**unit-verified only** — this runbook is the not-yet-run cluster exercise.

## What the fix does (one line)

`OrchestratorClient.register` raises `DuplicateThreadBinding` on a thread-bound
409 → the dedicated-mode lifespan catches it and calls
`persistent_app._exit_duplicate_provision()` → `os._exit(0)`. Because agent pods
are `restartPolicy: Never`, the pod ends `Completed` (no restart loop) and its IP
leaves the `session-<tid>` Service endpoints.

Code touchpoints:
- `src/api/orchestrator_client.py` — `DuplicateThreadBinding`, `register()` raise.
- `src/api/persistent_app.py` — `_exit_duplicate_provision()` + the
  `except DuplicateThreadBinding` clause in `lifespan`.
- `orchestrator/services/session_router.py:236` — `publishNotReadyAddresses: True`
  on the per-session Service (the reason a not-ready orphan is a live endpoint).
- `orchestrator/main.py` (`register_agent`) — source of the 409.

## Why you can't just wait for it to happen

The provision-side race that *creates* the second pod was closed on 2026-06-10
(commit `4830d122`): a timestamped `threads.metadata.agent_pod` marker is written
inside the advisory lock and checked by both provision entrypoints
(`provision_or_assign.py:86`, `routers/sessions.py:156`) via
`agent_pod_provisioning_in_progress()`. So under normal operation a second pod is
no longer created, and you must **manually induce** a second agent pod bound to an
already-bound thread to exercise the 409 path.

This is fine: the fix under test is agent-side ("when an agent finds the thread
already owned, does it exit?"), and a manually-cloned pod hits exactly that path.

## Prerequisites

1. k3d cluster up: `k3d cluster start srw` (context `k3d-srw`, namespace `srw`).
2. The **agent image must contain the fix**. With Tilt running, a save to
   `src/api/*.py` triggers the ~50 s agent rebuild + Reloader bounce; confirm the
   agent image tag advanced (`kubectl -n srw get pod <a session pod> -o
   jsonpath='{.spec.containers[?(@.name=="agent")].image}'`). Without Tilt:
   rebuild + push the agent image and `helm upgrade`.
   - Tip: run the **FAIL** half of the table below *before* deploying the fix and
     the **PASS** half *after* — same procedure, opposite outcome — to prove the
     fix is what changed the behavior.
3. `jq` and `kubectl` on PATH.

## Procedure (Approach A — clone the live session pod)

This is the most reliable method; it carries the winner's `--thread-id`/
`SESSION_BOUND_THREAD_ID` and the `srw.io/thread-id` label, so the clone both
registers for the bound thread (→ 409) and would enter the Service endpoints (the
blackhole vector).

### 1. Start a session and capture the winner + thread id

Open a new session in the cockpit (`https://localhost/` → Sessions → New Session),
then:

```bash
kubectl --context=k3d-srw -n srw get pods -l srw/purpose=session -o wide
WIN=<winner-pod-name>     # e.g. srw-agent-s-adeb3a08, READY 1/1
TID=$(kubectl --context=k3d-srw -n srw get pod "$WIN" \
       -o jsonpath='{.metadata.labels.srw\.io/thread-id}')
echo "thread=$TID winner=$WIN"
```

Baseline — the Service should already point only at the winner:

```bash
kubectl --context=k3d-srw -n srw get endpoints "session-$TID" \
  -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{" -> "}{.targetRef.name}{"\n"}{end}'
```

### 2. Clone the winner pod under a new name (induces the second registrant)

```bash
kubectl --context=k3d-srw -n srw get pod "$WIN" -o json \
  | jq 'del(.status, .metadata.uid, .metadata.resourceVersion,
            .metadata.creationTimestamp, .metadata.ownerReferences,
            .metadata.managedFields, .spec.nodeName, .spec.priority)
        | .metadata.name = "srw-agent-s-duptest"' \
  | kubectl --context=k3d-srw -n srw apply -f -
```

### 3. Observe the loser

```bash
# Logs — expect the 409 then the clean-exit line, within ~8–15 s of boot:
kubectl --context=k3d-srw -n srw logs srw-agent-s-duptest -f
#   ... Orchestrator refused thread-bound registration for thread <TID> (409) ...
#   ... Lost the provisioning race for thread <TID> — ... exiting cleanly ...

# Pod phase — expect Running -> Completed (NOT a lingering 0/1 Running):
kubectl --context=k3d-srw -n srw get pod srw-agent-s-duptest -w
```

### 4. Confirm the Service is clean (no blackhole)

```bash
# Endpoints should settle back to the single winner IP:
kubectl --context=k3d-srw -n srw get endpoints "session-$TID" \
  -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{" -> "}{.targetRef.name}{"\n"}{end}'

# Probe the Service from the orchestrator — expect all 200, no 503 coin-flip:
for i in $(seq 1 8); do
  kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator -c orchestrator -- \
    curl -s -o /dev/null -w "attempt $i: %{http_code}\n" --max-time 4 \
    "http://session-$TID:8001/ready"
done
```

## Pass / fail criteria

| Observable | PASS (fix present) | FAIL (old behavior) |
|---|---|---|
| `srw-agent-s-duptest` log | `…(409)…` then `Lost the provisioning race … exiting cleanly` | `…pod will stay up but will NOT attach a session…` |
| Pod phase after ~20 s | `Completed` (0/1, terminated) | `Running` 0/1, lingers indefinitely |
| `session-$TID` endpoints | single winner IP | two IPs (winner + orphan) |
| 8× `/ready` probe | all `200` | mix of `200`/`503` (~50%) |
| Cockpit session | connects/stays connected | "Establishing connection" hangs / intermittent |

All five PASS rows must hold.

## Cleanup

```bash
# If the clone is still around (e.g. you ran the FAIL case on an unfixed image):
kubectl --context=k3d-srw -n srw delete pod srw-agent-s-duptest --ignore-not-found
# End the test session from the cockpit (or let the idle reaper collect it).
```

A `Completed` clone is harmless but will sit in `kubectl get pods` until deleted;
remove it so it doesn't confuse the next run.

## Notes & gotchas

- **Clone fidelity.** The clone differs from a "real" racer only in timing (the
  winner is already bound rather than racing simultaneously). That difference is
  irrelevant to what's under test — the agent-side reaction to finding the thread
  already owned. The clone never reaches `_attach_session` or a WS handshake (it
  exits during `register`, before either), so a valid session JWT is **not**
  needed for this test.
- **Don't expect a `python -c` shortcut.** Calling
  `agent_provisioner.provision_agent(...)` from a fresh `python -c` inside the
  orchestrator pod won't work — the provisioner singleton's DB/k8s clients are
  only initialized in the running app's lifespan, not in a new subprocess.
- **Exercising the *genuine* concurrent race (optional, invasive).** To reproduce
  the original double-creation rather than a post-bind clone, temporarily neutralize
  the marker guard (comment out the `agent_pod_provisioning_in_progress(cur)` branch
  in `provision_or_assign.py` and the twin in `routers/sessions.py`), redeploy, then
  open two sessions for the same thread in rapid succession. Revert afterward. Only
  do this if you specifically want to confirm the provision-side marker fix too.
- **The orphan must not touch shared state.** `_exit_duplicate_provision` cleans up
  only this pod's own agent record (best-effort `deregister`/`close`); it must never
  call any thread-scoped teardown (`_terminate_session`, `teardown_route`,
  `delete_agent_pod_by_thread`) — those belong to the winner. If a future edit adds
  such a call to the exit path, this test would manifest as the **winner** losing
  its Service/workspace when the clone exits; watch for that regression.
