# Agent / workspace pod resource headroom — should we raise the limits?

**Status:** Filed — investigation complete. Original recommendation (2026-06-26): no increase needed, based on a **network-bound** session. **Superseded in part (2026-06-29):** a real **build/test** workload (job `19707fa1`, ERP + repeated `pytest`) — exactly the CPU/RAM-heavy case the original explicitly hadn't measured — **likely OOMKilled its workspace pod**, killing the SSH workspace and triggering the recovery wedge in `loop_job_workspace_lost_wedged_in_recovery.md`. The headroom question is now a real bug surface, not just informational. See **§Update 2026-06-29** below.
**Found:** 2026-06-26, prompted by live session `7692637b-9c60-4698-9875-b57ec34e66a6` (gpt-5.5, cloud-mounted) where `run_command` web-scraping felt slow and hit the shell "still running" interrupt — raising the question of whether agent/workspace pods are under-provisioned.
**Severity:** Low / informational. No current resource starvation; this is a "should we pre-emptively add headroom" question, not a bug.
**Component:** helm chart `agent.resources` · `orchestrator/services/container_provisioner.py` (workspace pod defaults) · `src/tools/shell/shell_tools.py` (`run_command` timeout / quiet-interrupt)

---

## The question

During a real session the agent ran shell commands (web scraping) that took a while and occasionally returned a "still running" interrupt. Natural hypothesis: the agent/workspace pods have thin resource limits and are being starved/throttled. Should we give them more?

## Measurements (live, session `7692637b`, main cluster)

| Where | CPU limit | CPU throttling (cgroup `cpu.stat`) | Memory | Restarts |
|---|---|---|---|---|
| **Agent pod** (`srw-agent-j-…`, LLM loop) | 1 core | 7 / 42,799 periods = **0.016 %** (2.5 s total / 3 h) | 540 Mi / 2 Gi = **26 %** | 0 |
| **Workspace pod** (`ws-thread-…`, `run_command` runs here) | 2 cores | 39 / 22,088 periods = **0.18 %** (31 s total / 77 m) | 210 Mi / 4 Gi = **5 %** | 0 |
| **Nodes** (×4, 32 cores each) | — | **2–7 % CPU used** | 21–50 % mem | — |

Both pods have large headroom, throttling is negligible, memory is barely touched, and there were **no OOM kills, SIGTERMs, or restarts**. The cluster is ~95 % idle — bigger limits would sit unused.

## Why the commands felt slow (none of it is pod resources)

1. **Network-I/O-bound scraping.** The slow `run_command`s curl dozens/hundreds of remote pages through the egress proxy — they *wait on the network*, which CPU/RAM cannot accelerate.
2. **The "timeout" is the shell tool's design, not a kill.** A quiet command returns a **"still running" interrupt after ~30 s** (`default_timeout: 120 s`, max 600 s — `src/tools/shell/shell_tools.py:351`). The command keeps running in the workspace; the tool just hands control back to the model. Confirmed: 0 restarts, no timeout/SIGTERM in the agent log.
3. **LLM latency.** One turn spent **86 s** in gpt-5.5 (`codex-proxy`), which is remote — unrelated to pod sizing.

## Verdict

**Pods are not the bottleneck; raising limits will not speed up these commands or stop the "timeout."** The real levers are:
- Let long scrapes complete: pass an explicit `timeout` (≤600 s) on `run_command`, or `shell_read()` to page output, instead of taking the ~30 s quiet interrupt. (Prompt/behavior.)
- **Parallelize fetches** — network-bound scraping scales with concurrency, not cores. Biggest available speedup.

## If we still want headroom (optional, low-risk on idle nodes)

Current values and the exact knobs:

| Pod | Knob | Current request | Current limit |
|---|---|---|---|
| Agent | `helm/values.yaml` → `agent.resources` (~line 161) | cpu 250m / mem 512Mi | **cpu 1000m / mem 2Gi** |
| Workspace (on-demand) | `orchestrator/services/container_provisioner.py:175-178` (hardcoded defaults `cpu=500m, memory=1Gi, cpu_limit=2000m, memory_limit=4Gi`) | cpu 500m / mem 1Gi | **cpu 2000m / mem 4Gi** |

If pre-emptive headroom is wanted (e.g. for future CPU-heavy workspace steps — builds, large parsing — none seen here):
- **Agent:** bump `agent.resources.limits.cpu` 1000m → 2000m (it's the smaller of the two and does token streaming + compaction + embedding). Memory is fine at 26 %. Clean helm value → `helm upgrade`.
- **Workspace:** bump `cpu_limit` 2000m → 4000m. Note this is a **hardcoded default in `container_provisioner.py`**, not a helm value — to make it configurable it should be threaded through values/env first (small refactor). Worth doing if we ever want per-tier sizing.

Risk: minimal — nodes are 32-core and ~95 % idle, and requests (what actually reserves capacity / affects scheduling) would stay modest. But this is **speculative headroom, not a fix for the observed symptom.**

## Open questions

- Is there any real workload today that is CPU-bound in the workspace (vs network-bound)? If not, there's no measured case for a workspace bump.
- Should the workspace pod resources be promoted from a `container_provisioner.py` hardcode to a helm value / per-tier setting, so sizing is operable without a code change?

## Update 2026-06-29 — a real build/test workload OOMKilled the workspace pod (job `19707fa1`)

The 2026-06-26 measurement was a **network-bound** scraping session (workspace at 5 % mem). Job `19707fa1` was the opposite: a long DEVELOPER loop building an ERP and repeatedly running `pytest` over a growing repo. Its **workspace pod went SSH-unreachable mid-run**; the agent caught `WorkspaceUnavailableError` and the job wedged in recovery (full story: `loop_job_workspace_lost_wedged_in_recovery.md`). k8s events aged out, so the exact cause is unprovable for this incident — but the config makes OOMKill the leading hypothesis, and exposes three latent problems.

### Why OOMKill / eviction is the leading hypothesis (ranked)
1. **Container OOMKill on the hardcoded 4 Gi limit (most likely).** Workspace pod resources are hardcoded `req cpu 500m / mem 1Gi`, `limit cpu 2000m / mem 4Gi` (`orchestrator/services/container_provisioner.py:172-178`, injected at `:1038-1044`) — **un-overridable** (grep confirms no `WORKSPACE_CPU/MEM` env and no Helm value; only `WORKSPACE_IMAGE` is chart-injected). A pytest/build workload over a growing repo readily spikes past 4 Gi. The 99 MB snapshot says the *repo* was small, so this was process RSS, not disk.
2. **Node memory-pressure eviction.** QoS is **Burstable** (requests ≠ limits) with **no `priorityClassName`** anywhere (grep empty across `helm/ orchestrator/ src/`). Under node pressure the kubelet evicts by usage-above-requests: the workspace requests 1 Gi but bursts to 4 Gi → biggest offender → evicted first; the agent pod (req 512Mi, low usage) ranks lower → **survives**. This exactly matches the observed asymmetry (agent fine, workspace dead).
3. **livenessProbe self-kill under load.** The workspace pod has `livenessProbe: tcpSocket:30022` (`container_provisioner.py:1080-1084`, default `failureThreshold:3`/`timeout:1s`) **with `restartPolicy: Never`** (`:1007`). Under CPU/swap starvation, 3 missed 1 s TCP accepts make kubelet kill the container — and `Never` means **it never restarts**. A liveness probe under `restartPolicy: Never` can only kill, never heal — a latent misconfiguration.

**Ruled out:** the orchestrator reaper / idle-suspension did NOT kill it — both are status-gated to non-`processing` jobs (`workspace_manager.py:38-49`, `workspace_suspension.py:803`), and `is_reachable` is never used in `is_healthy` so a busy-but-briefly-unreachable pod can't be force-reaped. Death was external (kubelet/kernel).

### Instrumentation hole (why we can't prove it, and why the next one will be just as opaque)
The discriminating SSH error string (`str(e)` — "Connection reset" vs "No route to host" vs `EOFError`, which separates OOMKill from eviction from sshd-crash) has **no durable home**:
- The recovery handler keeps only `error.type`, discarding `error.message` (`main.py:10141-10146`).
- The agent's per-job log file is written to its **own emptyDir `/workspace`** (`agent_provisioner.py:1224`), while `get_job_log` reads the orchestrator's **shared PVC** (`main.py:18075`) — different volumes, so agent logs are invisible to `get_job_log` in K8s and die with the pod. (This is why `get_job_log` for `19707fa1` returns "Log file not found".)
- No `agent_audit` row (the error is caught outside the audited node), and the path sets `error`, not `freeze_data`, so `job_frozen.json` doesn't capture it either.

### Fixes (sizes)
- **Capture the cause (XS, do first).** Persist `error.get("message")` into the recovery context/`error_message` and the warning log (`main.py:10134`). Optionally, on recovery, do a one-shot `read_namespaced_pod` on `workspace-<job[:12]>` and store `terminated.{reason,exit_code,signal}` — **the OOM-vs-eviction classifier already exists for agent pods** (`agent_provisioner.py:689-707`, RBAC at `helm/templates/orchestrator/rbac.yaml:20-23`); apply it to the workspace pod. This would have made this incident diagnosable post-hoc.
- **Make workspace resources Helm-configurable + bump (S).** Thread cpu/mem through ConfigMap env → `ContainerProvisioner.create_workspace` defaults (the `workspace_lifecycle.py:63` `**(ws_config or {})` seam can carry per-expert/per-job overrides). Raise mem **request** 1Gi→2-3Gi and **limit** 4Gi→6-8Gi — bumping the *request* both reserves headroom and improves the eviction ranking without a cluster-wide priority scheme (the pragmatic homelab move; `priorityClass`/Guaranteed only reorder victims on a single tight node).
- **Fix the livenessProbe (XS).** Drop it, or set `restartPolicy: OnFailure`, or raise `failureThreshold`/add `timeoutSeconds` — under `Never` it only adds a self-kill vector with no upside.
- **Don't re-dispatch into the same grave (handled in the sibling doc).** A fresh pod inherits the same 4 Gi + emptyDir; if the cause was OOM it dies again → silent re-dispatch loop. The bounded recovery cap + fail-loud in `loop_job_workspace_lost_wedged_in_recovery.md` (Tier-1) is the escalation.

## Related

`loop_job_workspace_lost_wedged_in_recovery.md` (the recovery wedge this pod death triggered) · `snapshot_restore_dead_for_jobs.md` (why the captured snapshot couldn't save the work) · `resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md` (same session debugging pass) · memory `project_workspace_tier_upgrade` (lite/sandbox/vm tier sizing)
