# Agent / workspace pod resource headroom — should we raise the limits?

**Status:** Filed — investigation complete. **Recommendation: no increase needed right now.** Documented as a deferred/optional headroom bump with the measurements and the exact knobs, so the decision is on record.
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

## Related

`resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md` (same session debugging pass) · memory `project_workspace_tier_upgrade` (lite/sandbox/vm tier sizing)
