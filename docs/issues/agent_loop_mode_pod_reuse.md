---
tags:
  - issue
  - lifecycle
  - agent-architecture
  - credentials
related:
  - "[[deprecate_docker_compose_stack]]"
  - "[[credential_broker]]"
  - "[[observability_and_quotas]]"
  - "[[lifecycle_session_agents_without_thread_never_drain]]"
---

# Worker/dual agent pods are reused across jobs in K8s (`--loop`), against the "one task then exits" design

**Filed:** 2026-06-20, from the usage-monitoring / rate-limiting design discussion
(the metering attribution question surfaced the lifecycle ambiguity). **Not a
live incident** — a deliberate-but-undocumented behavior + a latent
correctness/security question worth a dedicated look before the credential
broker lands.

> Line numbers were accurate on 2026-06-20 and will drift — re-grep
> `_should_loop` / `_reset_to_idle` / `AGENT_LOOP` / `get_available_agents`
> when acting on this.

## The divergence

Two parts of the codebase disagree about whether an agent pod is one-shot:

- `orchestrator/services/agent_provisioner.py:3-4` (module docstring): *"Each
  pod handles exactly one task (job or session), then exits (restartPolicy:
  Never)."* `restartPolicy: Never` is real (`:1349`).
- But the same provisioner builds the K8s pod command **with `--loop`**:
  `agent.py --port 8001 --loop` (`:241`).

`--loop` flips the agent out of one-shot mode. `agent.py:206-211` sets
`AGENT_LOOP=1` and logs *"agent will return to IDLE after task completion"*; the
docstring at `agent.py:166` states it plainly: *"Each pod handles one task then
exits **unless --loop is set**."* So in K8s the pod does **not** exit after a
job — and the dispatcher actively re-uses it:
`postgres.py:2529-2532` (`get_available_agents`) returns `status='ready'` agents
whose `last_completed_at` is `NULL` **or** older than the 30 s cooldown — i.e. it
re-hands a job to an agent that already finished one. `last_completed_at` is
stamped on the `working → ready` transition (`postgres.py:2182,2231`).

**Net effect (K8s):** a worker/dual agent runs job A → `_reset_to_idle()` →
back to `ready` → ~30 s later the dispatcher gives it job B, in the **same
Python process**. The pod is removed later by the reaper when idle/stale
(`reap_pods` `:589`, `_is_completed` `:746`, `scale_down_idle` `:869`), not
per-job.

## This is deliberate, not drift

It's tempting to call the `--loop` line a mistake. It isn't — there's a full
loop-mode state machine in `src/api/dual_app.py`:

- `_should_loop()` (`:321-323`) gates on `AGENT_LOOP`.
- `_reset_to_idle()` (`:326`) runs after **every** job (`:541-545`, and the
  resume path `:934-935`): it clears `_pod_state` / `_current_job_id` /
  `_current_job_task`, does session cleanup and file-handler cleanup, and pushes
  a final `ready` heartbeat — *then* exits only `if not _should_loop()`.
- The header diagram (`:6-12`) documents both modes explicitly:
  `IDLE → SESSION → detach → EXIT` (no-loop) vs
  `IDLE → SESSION → detach → IDLE` (loop).

So reuse is an intentional **latency optimization** (skip pod
provisioning/registration/tailscale-sidecar bring-up between jobs). The stale
artifact is the `agent_provisioner` docstring, which predates loop mode being
the K8s default. `--loop`'s *stated* justification in CLAUDE.md is narrower —
*"keeps the process alive after each job; required for bare-metal/Compose dev"* —
but the provisioner applies it to K8s too.

## Why it's worth a second look anyway

`_reset_to_idle()` resets **per-task** state, but the agent is the same
**process** across jobs, so anything at module/global scope or cached on
`app.state` survives the reset. Whether that's safe depends entirely on what is
rebuilt per job vs cached. Item 1 below (the load-bearing one) is now **verified
safe** (2026-06-21); 2–5 remain to confirm:

1. **Credential / LLM-client bleed — the load-bearing one. ✅ RESOLVED 2026-06-21.**
   Per-job creds (`api_key`/`base_url`) arrive in `JobStartRequest.config_override`
   and build the client via `loader._create_openai_llm`. **Verified the client is
   rebuilt per dispatch, not cached:** the orchestrator injects `config_override` on
   *every* dispatch → `config_dirty` is always true in `UniversalAgent.process_job`
   → `_create_phase_llms()` reruns (rebuilding strategic/tactical **and** aux via
   `_initialize_auxiliary_llm`) inside `_setup_job_workspace`, *before* the graph is
   built with the tool-bound clients. So a looped pod running job B builds fresh
   clients from job B's key — no job-A-on-job-B bleed. **The prerequisite for
   per-job/fleet keys is cleared:** the usage-monitoring gateway's Slice 2a shipped a
   shared fleet key on this basis ([[usage_monitoring_and_rate_limiting]]); Slice 2b
   then landed **per-(user, project) scoped keys** (per-job keys proved unnecessary —
   enforcement lives on shared team/user objects, so one key per (user, project) gives
   the same limits without per-job churn), minted per dispatch on the same basis.
   (Was: "if that client were cached across loop iterations,
   job B could run on job A's key/endpoint, making per-job ephemeral keys pointless.")
2. **Memory / RecallStore caches.** Project-scoped sharing is intended, but
   verify there's no cross-*project* or cross-*user* bleed when a pod serves
   job A (project X) then job B (project Y).
3. **Stuck-detection state.** Fingerprint history (`tool_name, args_hash`) and
   progress counters — confirm they're per-graph-invocation, not process-global
   (a stale fingerprint carried into job B would mis-fire loop detection).
4. **Graph / checkpointer instance.** AsyncSqliteSaver is keyed by job/thread,
   so state lookups should be safe, but confirm the in-memory saver/graph isn't
   accumulating across jobs.
5. **Any other module-global singletons** touched during a job (tool registry
   mutations, datasource handles, tmux/SSH backends).

If 1–5 are all rebuilt/reset per job, reuse is safe and the only fix is the
stale docstring. If any bleed, that's a real correctness/isolation bug today.

## The decision

Two coherent end states; pick one and make the code + docstring agree:

- **(A) Keep reuse (`--loop`), harden the reset.** Audit 1–5; ensure per-job
  client + config rebuild (needed for the broker anyway); fix the
  `agent_provisioner` docstring to describe loop mode. Keeps the latency win.
  **Recommended** unless the audit turns up bleed that's expensive to fix.
- **(B) Go one-shot (drop `--loop` in K8s).** Pod exits after each task; rely on
  `ensure_warm_pool` (`:539`) to keep MIN_AGENTS/AGENT_BUFFER **fresh**
  pre-provisioned pods for responsiveness (pre-provisioning ≠ reuse — they're
  orthogonal). Strongest isolation (fresh process per job, no bleed possible by
  construction). Costs: more create/delete churn and cold-start latency under
  burst beyond the warm buffer; **must verify persistent-session
  attach/detach/resume doesn't depend on the agent staying alive** (the
  dual-mode warm-pool reattach via `_find_idle_persistent_agent` assumes a live
  idle pod — see the related drain bug).

**Recommendation:** **(A)** — reuse is a deliberate optimization and the real
risk (state bleed) is fixable, and the per-job-client-rebuild work it requires
is a hard prerequisite for the credential broker either way. Do **not**
reflexively remove `--loop`; removing it trades a latency optimization for an
isolation guarantee we can also get by rebuilding per-job state, and it risks the
session reattach path. Revisit (B) only if the reset audit shows bleed that's
costly to close, or if [[deprecate_docker_compose_stack]] removes the
last `--loop` consumers and one-shot becomes simpler overall.

## What this does NOT affect

The usage-metering design that surfaced this is **unaffected** either way: agent
pods are explicitly out of the metered cost set (fixed cost), and the two
metered resources are single-owner regardless of agent reuse — LLM requests
self-attribute per-call (proxy key → job/user), workspaces are one-shot-per-job
/ one-thread-per-session. Recorded here only so the lifecycle decision and the
metering work don't get conflated.

## Verification (whichever path)

1. **Reset-completeness probe (path A):** run two jobs with *different*
   `config_override` LLM creds back-to-back on one looped pod; assert job B's
   outbound LLM calls use job B's key/endpoint, not job A's. (Easiest once the
   proxy exists — inspect which virtual key the second job presents.)
2. **Isolation probe:** two jobs on different projects/users back-to-back on one
   pod; assert no RecallStore/memory or datasource handle from the first is
   visible to the second.
3. **One-shot probe (path B):** drop `--loop` from `agent_provisioner.py:241` on
   k3d; run the README smoke path — jobs flip `created → processing →
   completed`, **and** sessions still spawn/attach/resume (the at-risk path).
   Watch warm-pool churn + cold-start latency.

## Related

- [[credential_broker]] — per-job ephemeral keys require per-job client rebuild;
  this audit (item 1) is a prerequisite. The broker is also the cleanest place
  to *detect* cred bleed (which key a reused pod actually presents).
- [[deprecate_docker_compose_stack]] — removes `--loop`'s original
  (bare-metal/Compose) justification; if it lands first, path B gets simpler.
- [[lifecycle_session_agents_without_thread_never_drain]] — the dual-mode
  warm-pool reattach machinery path B must not regress.
- [[observability_and_quotas]] — the metering work this surfaced from (unaffected).
