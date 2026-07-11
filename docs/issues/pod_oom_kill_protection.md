# Pod OOM-kill protection — stop treating OOMs one cause at a time

**Status:** Open — proposal. Several distinct OOM incidents have now been root-caused and fixed *individually*, but there is no systemic guardrail: the next unbounded allocation still SIGKILLs a pod, and for the orchestrator that still means a control-plane outage. This doc argues for defense-in-depth so an over-allocation degrades instead of crashing.
**Found:** recurring; filed 2026-07-11 after an operator/debug process (`kubectl exec … python` bulk-reading ~1,238 KB files) OOM-killed a live orchestrator pod. HA (`replicas: 2`) absorbed it with no user-visible outage, but it was the third+ OOM class on this component.
**Severity:** Medium (resilience). No single instance below is unfixed, but the *pattern* is high-blast-radius: the orchestrator is the dispatch/heartbeat/status authority, so one runaway allocation can `CrashLoopBackOff` both replicas and wedge dispatch cluster-wide. Self-reinforcing (client retries → fresh pod → OOM again).
**Component:** memory limits/requests (`helm/templates/orchestrator/deployment.yaml` `2Gi`; `agent.resources`; workspace pod defaults in `orchestrator/services/container_provisioner.py`) · any handler that materializes an unbounded set into RAM · operator/debug access pattern (`kubectl exec` into the critical pod) · k8s scheduling (requests vs limits, overcommit) · observability (no OOMKilled alert).
**Related:** `audit_metadata_config_duplication_ooms_orchestrator.md` (bulk-audit materialization → 2–3 GB transient → OOM, FIXED) · `agent_workspace_pod_resource_headroom.md` (build/test `pytest` likely OOMKilled a workspace pod) · memory `orchestrator_resume_oom_and_restore_buffering` (restore reads whole snapshot tar into RAM; limit bumped 1Gi→2Gi) · memory `orchestrator_ha_state` (replicas:2 + PDB — what saved us this time).

---

## The pattern (why one more point-fix isn't enough)

Every OOM so far has been a *different* unbounded allocation, each fixed on its own:

| # | Where | Trigger | Fix applied | Doc |
|---|---|---|---|---|
| 1 | orchestrator | `/audit/bulk` materializes ~476 MB of config-bloated rows | strip config write-side + delete bulk endpoints | `audit_metadata_config_duplication_ooms_orchestrator.md` |
| 2 | orchestrator | resume reads whole snapshot tar into RAM | bump limit 1Gi→2Gi (capture still buffers) | memory `orchestrator_resume_oom_and_restore_buffering` |
| 3 | workspace pod | build/test (`pytest`) RSS spike | headroom question, partly open | `agent_workspace_pod_resource_headroom.md` |
| 4 | orchestrator | operator `kubectl exec` bulk-read shared the pod cgroup | (this doc) — run heavy work off the critical pod | — |

The limit bump in #2 is telling: raising the ceiling doesn't add protection, it just moves where the next unbounded thing crashes. We keep playing whack-a-mole on *causes* while the *failure mode* (unbounded RAM → SIGKILL → crashloop → control-plane down) is unchanged. The goal of this doc is to make that failure mode non-fatal by default.

## Proposed protections (defense-in-depth)

Roughly ordered cheapest → most involved. None are mutually exclusive.

**A. Keep heavy work out of the critical pod's cgroup.**
- Anything `kubectl exec`'d into the orchestrator pod counts against its 2Gi limit and can SIGKILL the main process (incident #4). Guardrail: run ad-hoc scans/backfills/migrations from a **throwaway `Job`/debug pod or locally via port-forward**, never `exec` into orchestrator/agent pods for anything that touches bulk data. Cheap to adopt (it's a runbook rule); worth a `CONTRIBUTING`/runbook line + a memory note.
- Where an exec is unavoidable, `oom_score_adj` the child high so the *debug* process is killed first, not the server.

**B. Bounded-memory coding patterns (the real root of #1/#2/#3).**
- No handler should `fetch()`-materialize an unbounded set. Enforce: streaming/`cursor` iteration, hard `LIMIT` + pagination, and lean projections (don't SELECT fat columns you won't return). #1's real fix was this; apply the same lens to restore/capture (#2) and any future bulk path. A lightweight "does this load an unbounded N into RAM?" check in review would catch most.

**C. Right-size requests, not just limits.**
- Set `resources.requests.memory` close to steady-state and `limits` with real headroom over the *measured* p99, per component (orchestrator idles ~190 MiB but spikes; workspace build/test is the heavy case). Today requests are far below limits, so the scheduler overcommits nodes and a spike can also trigger node-pressure eviction, not just cgroup OOM. Measure first (`kubectl top` + cgroup `memory.peak`), then size.

**D. Survive the kill gracefully.**
- Orchestrator: keep `replicas: 2` + PDB (already in place — it's why #4 was a non-event) so one OOM never drops the control plane. Confirm the readiness probe fails fast and the surviving replica serves through the restart.
- Workspace pods (#3): a workspace OOM kills the SSH session and wedges recovery — size these for the build/test case or detect+reprovision instead of wedging.

**E. Observe it.**
- Alert on `reason=OOMKilled` / `RestartCount` increments and on pods sitting >~80% of their memory limit, so we catch a new unbounded path from telemetry instead of from a crashloop. We currently find these by noticing an outage.

## Suggested first step

Cheapest high-value combo: **A (runbook rule) + E (OOMKilled alert)** — together they turn the *next* OOM from "surprise control-plane incident" into "a logged, attributed event we can fix calmly." B and C are the durable fixes but need per-path work; track them as they come up.
