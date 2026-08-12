# agent-6

# Agent-lifecycle control plane audit for stateless_agents.md

## 0. Headline: the lease already exists

The doc's "What's missing → Turn/batch lease" is partially **already built and live**. Migration `0054_jobs_execution_lease.sql` added `jobs.lease_expires_at` + partial index `jobs_lease_expiry_idx ... WHERE status='processing'`. The full loop is in place (docs/features/job_execution_lease.md, stages 1–3 implemented 2026-07-12):

- **Acquire**: `claim_job_for_agent` (postgres.py:5933-5977) — the single CAS every dispatch path funnels through: `UPDATE jobs SET status='processing', assigned_agent_id=$2, lease_expires_at=NOW()+180s ... WHERE assigned_agent_id IS NULL AND status IN ('created','paused') RETURNING id`. Comment: "Callers MUST claim before notifying the agent." `JOB_LEASE_PICKUP_SECONDS=180` covers dispatch POST + agent init + workspace connect; a dead pod simply never renews (this is what makes the dispatcher's claim-then-notify-without-rollback design sound).
- **Renew**: rides the agent heartbeat (postgres.py:5235-5260): `SET lease_expires_at=NOW()+90s WHERE id=$1 AND assigned_agent_id=$2 AND status='processing' AND (lease_expires_at IS NULL OR lease_expires_at < NOW()+60s)`. The `assigned_agent_id=$2` guard is the fence — an agent that lost the job cannot extend. Constants at postgres.py:574-586 (`JOB_LEASE_RUN_SECONDS=90`, `JOB_LEASE_RENEW_BELOW_SECONDS=60`, write-throttled to ~1 write/30s/job).
- **Expire**: `recover_expired_lease_jobs` (postgres.py:5504-5538) — pure DB-clock CAS, no join to `agents`, no sweep-ordering dependency; runs as isolated step 4b of `stale_agent_detector` (main.py:1607-1638). NULL leases are left to the legacy sweep.
- **Not yet done** (per the feature doc): stage 4 completion-CAS (a fenced-out agent's `/complete` still silently wins) and the `lost_lease` heartbeat intent; stage 5 demotion of `recover_orphaned_jobs` to assertion mode.

So a stateless batch driver does not need a new lease mechanism — it needs (a) a **pull-shaped claim** over the same CAS (today only the leader's dispatcher calls it, push-shaped), (b) renewal decoupled from the agent-row heartbeat (renew keyed by claim token instead of `assigned_agent_id`), and (c) `batch_boundary` release = exactly what `agent_release_job` (`PUT /api/jobs/{job_id}/agent-release`, main.py:11950-11975) already does: `pause_job` → clears assignment → `_trigger_dispatch()`.

**Tension to flag**: the lease design doc and several postgres.py docstrings say "renewed by the agent's 5s heartbeats (18 missed beats of slack)". The *actual* heartbeat interval is **60s** — hard-coded in the registration response (postgres.py:5073, 5096 `"heartbeat_interval_seconds": 60`) and consumed by the client (src/api/orchestrator_client.py:307, 399, 1148-1215). With 60s beats and renew-below-60s, steady-state remaining oscillates 90→30s; **one heartbeat missed/late by >30s can expire a healthy running job's lease**. For a stateless class with long tool calls this cadence mismatch must be resolved explicitly (heartbeat from the tool-wait loop, or a longer batch lease TTL).

## 1. Deletion ledger

### 1a. Agent identity & registration — DELETE for stateless class
- `POST /api/agents/register` endpoint (main.py:24808-24882) + `postgres_db.register_agent` (postgres.py:4982-5097). Hostname-keyed upsert; re-registration **pauses any processing jobs still assigned** (postgres.py:5028-5041) — a whole failure-recovery path that a stateless pod doesn't need (the lease covers it). Replaced by: nothing (pods have no identity).
- Persistent-mode duplicate-bind 409 dance inside registration (main.py:24840-24871, under `thread_advisory_lock`) — exists only because two pods can race to *own* a thread. Replaced by: turn lease/epoch fencing.
- `agents` table (schema_current.sql:4120-4138): `id, config_name, hostname, pod_ip, pod_port, pid, status(8-value CHECK), current_job_id, registered_at, last_heartbeat, last_completed_at, metadata(jsonb), agent_mode, thread_id, intents(jsonb), pod_uid, aux_degraded`. For the stateless class the row disappears; `threads.agent_id` (schema_current.sql:7255) and `jobs.assigned_agent_id` become lease metadata (`ON DELETE SET NULL` FKs already tolerate agent-row absence, postgres.py:7732-7733).
- `agents.pod_uid` exists solely so the session router can set ownerReferences on per-session Service/Ingress (schema comment schema_current.sql:4153) — deletable with the per-session route (see 1e).

### 1b. Heartbeat — SPLIT: delete the agent-slot half, keep the payload channels
`POST /api/agents/{id}/heartbeat` (main.py:27093-27212) + `postgres_db.heartbeat` (postgres.py:5099-5272) currently multiplexes SIX concerns:
1. Liveness → agent row `last_heartbeat` — **delete** (lease renewal replaces it).
2. Status assertion w/ draining/offline pin (postgres.py:5200-5204) — **delete** (no drain choreography).
3. **Job lease renewal** (postgres.py:5235-5260) — **keep**, re-keyed to the lease claim.
4. Job-status backstop (heartbeat *response* carries the job's DB status so an out-of-band cancel reaches a running agent; main.py:27150-27168) — **keep the function**: in pull mode the batch driver re-reads job status at each superstep or via the renewal response.
5. Steering pull: `pending_guidance` + `queued_replies` ride the response from `jobs.context` (main.py:27169-27196) — **keep**; already data-shaped, trivially becomes part of the renewal/claim response.
6. `graph_progress` metric → `metadata.graph_progress_seen_at` (postgres.py:5212-5218), feeding the graph-progress stall sweep — **keep the detector concept** (it catches renew-fine-but-stuck, which a lease can't), re-homed onto the job row.

### 1c. Dispatcher — SHRINK to enqueue for stateless class; keep VM/workspace pre-flight
`_try_dispatch_pending_jobs` (main.py:7158-7851), leader-gated `auto_assign_dispatcher` every 30s (main.py:7854-7874) + event `_trigger_dispatch` (main.py:7877-7887, gated on `is_leader`):
- **Keep (it's workspace lifecycle, not agent lifecycle)**: the entire per-job workspace pre-filter — VM decision machine VM_PROVISION/VM_WAIT/VM_RECYCLE/VM_GOLDEN_POLL/VM_HEADSCALE_POLL/parks (main.py:7210-7535), sandbox `ensure_workspace` state machine (main.py:7595-7676), scholar parent-pod inheritance (main.py:7186-7208, 7536-7553). **Important correction to the doc**: "the registered-agent path stays for VM-backed … modes" is imprecise — vm_backend.md:26 states shipped option 1: "**the agent pod runs on the main cluster, the VM is a remote workspace only**" (dispatch injects `workspace.backend='vm'` + `remote.host=vm_ctx['ssh_host']`, main.py:3664-3702; session VM upgrade likewise just provisions a workspace VM, main.py:26718-26772). The VM lane constrains *workspace* provisioning, not agent identity. What genuinely needs the registered-agent path during migration: bare-metal/Compose deploys (`agent.py --loop`, docker_provisioner static pool main.py:7564-7594), officer dedicated pods, and the un-migrated pinned-pod soak path.
- **Delete for stateless class**: agent matching — `get_available_agents` with the 30s cooldown `last_completed_at` predicate (postgres.py:6960-6994), stale-SHA filtering `_agent_sha_is_current` (main.py:7698-7717), phase 1.5 "provision pods for unmatched jobs then wait for them to register" (main.py:7762-7784). Replaced by: enqueue (NOTIFY) after workspace-ready.
- **Keep, re-shaped**: priority + preemption phase 2 (main.py:7786-7848, `get_preemption_candidates`) — in batch mode preemption degrades to "don't offer the next batch," which the doc already notes; the priority *ordering* moves into the claim query.
- The claim CAS at main.py:7736 is already the concurrency-safe kernel: "two transient leaders may both scan the same candidate, but only one CAS wins".
- Resume-vs-fresh lane split `resume_lane_applies` + checkpoint probe (main.py:7742-7756) — **deletable in the limit**: the doc's "one lane: every batch loads everything" point; today's fresh/resume asymmetry is the root of docs/done/fresh_job_dispatched_as_resume_skips_seeding.md.

### 1d. Stale-agent / orphan detection — DELETE most, keep 3 re-homed detectors
`stale_agent_detector` (main.py:1425-1658), post-incident shape: per-step isolation `_step` wrapper (main.py:1442-1457, from docs/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md — the 36h silent-failure incident).
- `mark_stale_agents_offline` 3-min timeout (postgres.py:5369-5394) — delete (no rows to mark).
- `mark_stuck_working_agents_ready` (postgres.py:7547-7575), `mark_stuck_session_agents_ready` (postgres.py:7614-7648), `reap_orphaned_session_agents` STOPGAP with jsonb grace-stamp choreography (postgres.py:7650-7722, main.py:1519-1544) — all delete; each exists only because a pod's self-reported state can wedge against DB truth.
- `mark_orphaned_threads_ended` / `mark_orphaned_threads_suspended` (postgres.py:7469-7545, main.py:1546-1580) — delete for stateless sessions (a thread is never "bound to a dead agent"); note officer threads are already exempted (postgres.py:7496-7500) — officer lane stays pinned.
- `recover_orphaned_jobs` 4-way sweep (postgres.py:5396-5502) — already scheduled for retirement by the lease doc ("demote to assertion after soak"); statelessness completes it.
- **Keep re-homed**: lease expiry (step 4b), graph-progress stall (opposite failure mode), and freeze-blob shed for auto-redispatch types (postgres.py:5477-5491, syncs with completion.py `AUTO_REDISPATCH_FREEZE_TYPES`).
- `gc_offline_agents` 24h (postgres.py:7724-7752) — delete with the table.
- Officer wake notifications on offline/orphan events (main.py:1470-1486, 1590-1637) — keep, re-sourced from lease-expiry events.

### 1e. Warm pool / provisioner / scale-down — DELETE for stateless class
`agent_pool_reconciler` (main.py:1661-1687) driving, per 60s tick:
- `ensure_warm_pool` (agent_provisioner.py:751-799): MIN_AGENTS floor + AGENT_BUFFER idle headroom, `_count_idle_agents` from the agents table (735-749). Replaced by: Deployment `replicas` / HPA.
- `reap_pods` (801-881): six pod GC categories (completed/crashed/tunnel_dark/stale/drained/unstartable) with per-category grace + pre-reap log capture (883-957) — replaced by: normal ReplicaSet semantics + `restartPolicy`; log capture concern moves to the job-log archive (already keyed by job).
- `scale_down_idle` (1081-1161) incl. the warm-pool-vs-scale-down oscillation guard (comment 1088-1093: the two loops literally fought, 1 pod/min churn for hours) — replaced by: nothing.
- purpose reservations + `_try_evict_for_reservation` (244-313, 1163-1235) — replaced by: two Deployments (interactive vs worker), which is also the doc's priority-lane answer.
- `provision_agent` per-session PVC block (329-362) + `_create_pvc` (1620-1694, `pvc-agent-s-<tid12>`, thread-labeled for the lifecycle reaper, fail-closed on quota 403) — **must be resolved, not deleted**: this PVC is the agent-side `/workspace` durability for sessions (memory: "session workspace wiped every idle cycle"). Stateless pods can't mount an RWO per-thread PVC. For lite sessions (S1) there's nothing to mount; for workspace sessions the answer is the doc's own premise — state lives on the *workspace pod* — so the ledger entry is "delete the agent-side PVC by finishing the externalization," which is a prerequisite, not a footnote.
- Drain: `lifecycle_reconciler_loop` (main.py:1690-1715) → `InstanceLifecycleReconciler.tick` with DisruptionBudget (lifecycle/reconciler.py:32-92); `AgentInstanceManager.signal_drain_pending` writes `intents.should_drain` jsonb (lifecycle/agent_manager.py:146-175), `drain` flips ready→draining (177-209); agent side `_handle_heartbeat_intents` (dual_app.py:363-430): idle→`os._exit(0)`, session→defer until parked then `_drain_suspend_session`, busy worker→flag for phase boundary. All deletable for the stateless class ("a draining pod just doesn't claim the next batch" = k8s preStop + don't-claim). **Keep** `VMInstanceManager`/`WorkspaceInstanceManager` (lifecycle/vm_manager.py, workspace_manager.py) — workspace lifecycle again. Note unified_instance_lifecycle.md is *still a design proposal* ("Deferred twice and still owed") — statelessness would obsolete its agent half before it's built.

### 1f. Session lane — the pinned-pod machinery, component by component
Create/attach flow (who creates the pod): `create_thread` fires `provision_or_assign` (services/provision_or_assign.py:26-231) under `thread_advisory_lock`: grant + endpoint pre-flights → idle-pool probe `_find_idle_persistent_agent` (main.py:4397-4440: `agent_mode IN ('persistent','dual') AND status='ready' AND thread_id IS NULL`, SHA-filtered) → `_send_session_attach` (main.py:4443-4619; re-authorizes datasources under `thread_datasource_lock`, re-resolves expert config, POSTs `/session/attach` to pod IP, then writes both sides of the binding `agents.thread_id` + `threads.agent_id` at 4593-4605) → else dedicated pod `provision_agent(purpose="session")` + `wait_for_binding` (register writes the bind) + `wait_for_ready` poll. All deletable for stateless sessions; replaced by "session = DB row, first turn hits the deployment". The attach-time config re-resolution (main.py:4492-4571) is the "per-request config resolution exists" machinery the doc cites — it moves verbatim to per-turn.
- Message input lane today: `POST /api/persistent/threads/{tid}/input` (main.py:30787-30840) → per-(thread,turn) **in-process** asyncio lock (main.py:30385-30410 — explicitly commented "Single-instance orchestrator, so a module-level dict is enough"; it is NOT replica-safe today) → `_resolve_thread_for_forwarding` (main.py:30413-30458: owner check, suspended-workspace restore, 503 "No agent bound to thread") → `_forward_to_agent` POST to `http://pod_ip:port/api/input` (main.py:30461-30486). Interrupt likewise (30843-30850). Replaced by: turn-request row + claim; the 409 turn_in_flight contract becomes a DB uniqueness/lease check (fixing the replica-unsafety as a side effect).
- SSE out-path: **already pod-agnostic** — `/stream` serves from `thread_events` with epoch+cursor replay, `_no_cursor_replay_start` anchors past last `turn.completed` (main.py:30489-30690); mid-stream epoch-bump re-check `THREAD_EVENTS_EPOCH_RECHECK_S` (30517-30526). Keep as-is; it is the streaming terminus answer for v1.
- Per-session Service/Ingress: `SessionRouterService.ensure_route` (session_router.py:95-170) creates `session-{tid}` Service+Ingress at path `/p/{thread_id}`, patches pod labels (`srw.io/thread-id`, flips `srw/purpose` to session), ownerRef'd to the pod via `agents.pod_uid`; `teardown_route` (172-191). Serves the direct-WS `/ws/chat` handshake authorized by 60s HS256 session JWTs (`SessionTokenService`, session_tokens.py:28-73, claims sub/tid/aud=agent). **Fundamentally pod-pinned** — delete for stateless sessions (journal-SSE + REST input replaces it; P5/P6 convergence), or keep only for attachments that truly need a socket to a specific host (IDE/browser live on the *workspace* pod anyway).
- Idle handling: `attention_sleep_sweeper` (main.py:31883-31980, leader-gated) — threads in `awaiting_user` past per-thread/user/global TTL (`threads.awaiting_user_since`, schema comment 7289) → `suspend_thread_workspace` (snapshots FS to S3, deletes workspace pod/VM, **also deletes the bound agent pod** — workspace_suspension.py:502-504 per the docstring) → CAS `awaiting_user`→`suspended`. For stateless: the agent-pod half disappears; the workspace-suspension half **stays** (it's the real cost being saved) — the sweep becomes purely a workspace policy.
- Teardown: `_release_thread_resources` (main.py:6620-6683) = pre-teardown `_detach_agent_session` grace call (`/session/detach`, 150s budget for final memory capture + git push, main.py:6556-6617) + workspace archive + `delete_agent_pod_by_thread` (agent_provisioner.py:585-620, label-selected) + legacy `persistent_provisioner` pod/PVC. `_suspend_thread_resources` (main.py:6694-6751) with in-process `_threads_suspending` dedup set (again not replica-safe). For stateless: detach-grace becomes "finish current turn + flush queued background work"; pod deletion disappears; workspace archive stays.
- Module-singleton runtime: persistent_app.py is one-session-per-process (`_session`, `_thread_id`, `_events_epoch`, `_next_seq`, `_loop_user_queue`, `_hard_interrupt_event` — e.g. reset block at 1550-1559, 2043-2047). Confirms the doc's v1 "one active turn per pod" framing; the in-memory `_loop_user_queue` is the queued-input the doc wants in the DB.

### 1g. Officer lane — KEEP pinned for now
Officer threads are systematically exempted from every reaper (postgres.py:7496-7500, main.py:31923-31928 "their lifecycle belongs to the officer watchdog (centurion.md §4)"); the watchdog respawns dedicated pods via persistent_provisioner (see _release_thread_resources comment main.py:6654-6659). An always-on officer is the one session shape whose duty cycle argues *least* for statelessness; ledger: keep, migrate last (or note that a wake-driven officer is actually the best stateless fit later).

## 2. Concurrency-primitive inventory (what a turn/batch lease should reuse)

- **Central advisory-lock registry**: orchestrator/database/lock_ids.py — packed-ASCII int64 keys: `SRW_MIG` (migrate.py:168, xact-scoped around the migration run), `SRW_AUDT` (audit_partitions.py:176), `SRW_LEAD` (leader election, the only session-scoped one), bench `hashtext('bench_sweep')` int4. New lease keys must be added here.
- **Leader election** (services/leader_election.py): session-scoped `pg_try_advisory_lock` on a dedicated pooled connection; `is_leader` asyncio.Event; `run_when_leader` wraps ~20 singleton loops in main.py lifespan (main.py:9929-10199: stale_agent_detector, auto_assign_dispatcher, attention_sleep_sweeper, officer_watchdog, agent_pool_reconciler, redispatch sweepers, etc.), with crash-restart and cancel-on-loss. Explicit caveat in both files: **the transient dual-leader window cannot be fenced by election alone — every loop's side effects must be CAS-guarded** (leader_election.py:180-184). It also exposes `get_leader_generation()` (43-51) — a DB fencing generation populated on acquisition, "callers must validate it in the same transaction as every durable mutation". Requires direct asyncpg (transaction-mode poolers silently break it, leader_election.py:18-21).
- **Job-claim CAS**: `claim_job_for_agent` (postgres.py:5933) and `claim_delegation_resume` (5979-6014) — the house style: single-statement `UPDATE ... WHERE <legal-transition> RETURNING id`, "True iff THIS call won".
- **FOR UPDATE SKIP LOCKED, working precedents**:
  - `cron_dispatcher._process_one_due_automation` (cron_dispatcher.py:120-137): claim+fire+advance in ONE transaction "so SKIP LOCKED can do its job — concurrent dispatcher replicas see disjoint sets of due rows".
  - **datasource_reconciliation.py:1-13 — the closest full model of what the worker queue needs**: "claim uses FOR UPDATE SKIP LOCKED and a lease, while the success/retry writes are guarded by a sequence-backed, **never-reused claim token**" (claim_token = `nextval(...)`, postgres.py:8912, 9223, 10131-10215; completion writes require `AND claim_token=$3` at 10174). Leader-gated for efficiency, DB-guarded for correctness. 120s lease, bounded backoff.
  - Infra-metering modules use SKIP LOCKED extensively (infrastructure_metering/*).
- **Row locks for JSONB merges**: `merge_job_context` et al. `SELECT context FROM jobs WHERE id=$1 FOR UPDATE` inside a transaction (postgres.py:2745-2790, 2955-2990); job+thread paired ordering with explicit lock-order comments (postgres.py:3490-3493, 4027-4030).
- **Per-entity advisory locks**: `thread_advisory_lock` (postgres.py:7076-7093, blake2b(thread_id)→int64, xact-scoped) serializing provisioning; `thread_datasource_lock` (7096-7104); rewind lock `hashtext('thread_rewind:{tid}')` (7930-7935); docker workspace leases table `docker_workspace_leases` (migration 0059; postgres.py:3381+) — a *durable occupancy authority* with owner_kind∈{job,thread}, status CHECK, fingerprint checks — precedent for a materialized lease table if the jobs-row lease is deemed too implicit.
- **events_epoch as fencing token — confirmed viable**: `threads.events_epoch` (schema_current.sql:7270, comment 7282: "The agent allocates a new epoch on every DB-backed runtime attach; older client cursors trigger authoritative re-sync"). Allocation is agent-side, atomic, monotonic: `UPDATE threads SET events_epoch=events_epoch+1 ... RETURNING` (`_resolve_event_journal_epoch`, src/api/persistent_app.py:1476-1510), deliberately unconditional per attach. Orchestrator-side rewind also bumps it (postgres.py:7973-7990). SSE consumers already treat epoch mismatch as authoritative re-sync (main.py:30582+). A per-turn lease can reuse exactly this: claim = epoch bump; event writes carry epoch; stale-epoch writers are detectably fenced at the journal.
- **Generation fencing elsewhere**: vm_provisioner `provision_generation` + `_set_context_if_generation` (vm_provisioner.py:349-390, 558) — context writes refused when the generation moved; another in-repo fencing idiom.
- **Materialized one-shot claim**: session-wake outbox `mark_job_wake_pending` (postgres.py:6016-6100) — "the claim exists because the send is NOT idempotent... Claim first, commit, then send," with the explicit note of when a status-CAS is insufficient and a materialized claim is required. Directly applicable to turn-request rows.
- **Cautionary precedent**: docs/done/bench_sweeper_multi_replica_race.md — bench sweeper had NO claim; dev's 2 replicas double-submitted 3/30 pairs (twins 2-5ms apart) once tick phases aligned; fixed with a session-scoped `pg_try_advisory_lock(hashtext('bench_sweep'))` held on ONE pooled connection (claim+release must share the session — `PostgresDB.fetchval` would unlock a different pooled session). Two lessons for the stateless doc: (1) any sweeper started from a *router* lifespan escapes the `run_when_leader` convention audit; (2) "leader election exists" ≠ "claims exist" — the race fired *despite* the codebase having election, because that sweeper wasn't wrapped.
- **Known replica-UNSAFE spots the stateless design inherits-and-fixes**: per-turn input lock dict (main.py:30385-30410), `_threads_suspending` set (main.py:6691), `_pause_pending_job_ids` (main.py:7832-7840) — all module-level in-process state on a 2-replica dev deployment.

## 3. Credential/config injection at dispatch — what a lease-claim must fetch

**JobStartRequest** (main.py:8233-8272) fields: job_id, description, upload ids, document_path/dir, config_name, `config_override` XOR `resolved_config` (mutually exclusive on the wire, main.py:3938-3960), context, instructions, git_remote_url, `datasources` payload, `repositories` payload (incl. `credentials` per repo, main.py:3607-3618), branch_name, project_id, delegation_context. POSTed to `http://pod_ip:8001/job/start` (main.py:3963-3968); then `update_job_status(processing, assigned_agent_id)` re-assert + a simulated `working` heartbeat (3977-3988).

Assembled per dispatch (main.py:3553-4002), all of it re-run on **resume** too (`_resume_job_on_agent` main.py:4005-4090: "Resume is another credential-delivery boundary. Preserve the stored set, reauthorize it as a whole, and fail closed on any revoked row") — i.e. **the per-claim resolution the stateless model needs already runs on every re-dispatch**:
1. Datasource re-authorization `_resolve_authorized_job_datasources` — revocation fails the job, never silently reduced (3636-3650).
2. `_inject_dispatch_credentials` (3201-3550): per-user/project API keys (`resolve_api_keys_for_job`), endpoint-backed model routing (base_url + **api_key values** into `llm.*`), per-section strategic/tactical/auxiliary credential injection, user defaults (autonomy, reasoning, vision/whisper/tts/citation/embedding `env_keys` incl. `EMBEDDING_API_KEY`), system-default chat/embedding fallbacks, per-model `model_max_context_tokens`.
3. Experts path: `resolve_config` → grant PEP `_enforce_dispatch_grants` (fail closed) → `inject_blob_credentials` → **store only `redact_config_override(resolved_config)`** to `jobs.resolved_config` (3865-3875). Resume re-runs the PEP against CURRENT grants (4080-4090).
4. Workspace SSH: VM block sets `workspace.remote{host,port,username='agent-host',key_path='/run/secrets/vm-ssh-key',...}` (3664-3702); sandbox `_inject_container_workspace_config` (5456-5492) — username/key_path "always refresh[ed] … in-flight deployment configuration and deliberately not written back to jobs.config_override". **The SSH private key is never in the payload** — `key_path` points at a deployment-mounted shared secret (`_container_ssh_key_path`, 5441-5453), identical on every agent pod: already stateless-compatible.
5. Lite tier `_inject_lite_workspace_config` — "deployment-sourced credentials, in-flight only (never persisted to the thread row)" (3726-3765; session attach twin at 4480-4490).
6. Backstop: refuse dispatch of a workspace-backed job with no `remote` (3777-3800).

Session attach mirrors it: `_send_session_attach_locked` re-fetches thread, re-authorizes connectors under the datasource lock, re-resolves expert config, delivers `{config_override XOR resolved_config, datasources, project_ids, config_name}` (4463-4581).

**NEVER in a queue/turn row** (the codebase already has this discipline; a queue row must not regress it): resolved LLM api_key values and endpoint keys; `env_keys` values; repository credentials; datasource connection credentials; lite object-store credentials; session WS JWTs (minted per handshake, 60s TTL). The queue row should carry only ids + batch budget + priority + claim token; the claiming pod re-runs the resolution pipeline (which conveniently makes grant/credential revocation take effect at batch granularity — today's dispatch/resume boundaries already advertise exactly this property).

## 4. Session agent pod k8s lifecycle today (exact, with owners)

1. **Create**: cockpit `POST /api/persistent/threads` → thread row → fire-and-forget `provision_or_assign` (provision_or_assign.py:26): advisory-locked pool-attach (`_find_idle_persistent_agent` → `/session/attach`) or `agent_provisioner.provision_agent(purpose="session", thread_id)` (agent_provisioner.py:244): name `srw-agent-s-<8hex>`, per-thread PVC `pvc-agent-s-<tid12>` (fail-closed), pod manifest (1241+), thread.metadata.agent_pod status writes (390-402). Fresh pod boots → `POST /api/agents/register` (24808) binds `threads.agent_id` under the thread advisory lock (409 for a losing twin → pod self-exits); orchestrator `wait_for_binding`/`wait_for_ready`, lifecycle SSE provisioning→booting→ready.
2. **Route**: `ensure_route` (session_router.py:95) — label patch + Service + Ingress `/p/{tid}`, ownerRef to pod (GC-on-delete via `agents.pod_uid`); WS handshake via 60s session JWT. REST input path is orchestrator→pod POST via `threads.agent_id`→`agents.pod_ip` (`_resolve_thread_for_forwarding`).
3. **Serve**: pod heartbeats every 60s as `session` (persistent_app.py:1136-1146); events written to `thread_events` under the attach-allocated epoch; messages persisted mid-turn to `thread_messages`.
4. **Idle**: agent flips thread to `awaiting_user` + stamps `awaiting_user_since`; leader-gated `attention_sleep_sweeper` (31883) past TTL → `suspend_thread_workspace` (S3 snapshot, deletes workspace pod/VM **and the agent pod**, workspace_suspension.py:502-504) → thread `suspended`. Reopen: `_resolve_thread_for_forwarding`/prepare restores workspace + re-provisions/attaches an agent.
5. **Reap/repair** (who deletes pods): (a) `agent_pool_reconciler`→`reap_pods` six categories (801-881); (b) `stale_agent_detector` orphaned-session reap (1529-1544) + thread ended/suspended propagation with `_release_thread_resources(reclaim_volume=False)` — "an agent crash must never take the user's PVC-backed workspace" (1552-1563); (c) lifecycle reconciler drift-drain via `intents.should_drain` → agent self-exit/suspend (dual_app.py:363-430); (d) user DELETE → `end_thread` (29696) → `_release_thread_resources(reclaim_volume=permanent)` incl. 150s detach grace; (e) scale-down of idle pool pods (1081).

## 5. Surprises / doc corrections

1. **The job execution lease already shipped** (0054 + claim/renew/expire live; step 4b in the detector). The doc's "What's missing: turn/batch lease … This *replaces* agent heartbeat/orphan machinery" should say: the *job half already exists and is soaking*; what's missing is pull-claiming, claim-token renewal (not agent-id-keyed), and completion-CAS (stage 4, still open — a fenced-out agent's completion silently wins today).
2. **Heartbeat cadence is 60s, not 5s** — the lease's "18 missed beats of slack" comment (postgres.py:580, feature doc) is stale; real slack is <1 beat in the worst phase. Any batch-lease TTL must be sized against the *actual* renewal transport, and long tool calls need in-loop renewal regardless.
3. **VM is a workspace, not an agent host** (vm_backend.md:26) — the doc's "registered-agent path stays for VM-backed … modes" mis-frames the lane; VM machinery lives in the dispatcher's workspace pre-filter and survives statelessness untouched. The lanes that truly keep registration: compose/bare-metal, officers, and the pinned-pod soak path.
4. **Session agents already carry a per-thread PVC** (`pvc-agent-s-*`, shipped for the wipe bug) — a stateless deployment cannot mount it; the doc needs an explicit migration note (fold agent-side /workspace into the workspace pod or drop it for lite tier).
5. **The dispatch/claim CAS + no-rollback design is already stateless-shaped**: claim-then-notify with pickup-lease self-healing (postgres.py:5975 comment, main.py:7730-7741) is exactly a queue visibility-timeout; the "replaced-by" for much of the ledger is code that exists.
6. **datasource_reconciliation.py is a complete in-repo template** for the worker queue: SKIP LOCKED + lease + never-reused sequence claim-token + leader-gating-for-efficiency-only.
7. **In-process turn lock / suspend dedup are already replica-unsafe** on the 2-replica dev deployment (comment "Single-instance orchestrator" at main.py:30382 is stale) — DB-queued turn requests fix a live latent bug, strengthening the doc's S1 acceptance criteria.
8. **events_epoch allocation is agent-side today** (persistent_app.py:1476) — in the stateless model the epoch bump must move into the claim transaction (orchestrator/DB-side), which the rewind path (postgres.py:7973) already demonstrates.
9. `_forward_to_agent`'s input POST and `/session/attach` have **no fencing at all** beyond `threads.agent_id` — the register-time 409 is the only duplicate-serve guard; per-turn claims subsume it.

## design_implications
- Reframe 'Turn/batch lease' as EXTEND, not ADD: cite jobs.lease_expires_at (migration 0054), claim_job_for_agent's CAS+pickup lease (postgres.py:5933), heartbeat renewal with assigned_agent_id fence (postgres.py:5235), and recover_expired_lease_jobs (postgres.py:5504) as the shipped substrate; the stateless work items are pull-shaped claiming, claim-token-keyed renewal, batch_boundary release (reuse PUT /api/jobs/{id}/agent-release semantics, main.py:11950), and the still-open stage-4 completion CAS.
- Fix the heartbeat-cadence assumption explicitly: registration issues 60s intervals (postgres.py:5073) while lease constants assume 5s beats; specify that batch-lease renewal rides the batch driver's superstep/tool-wait loop with its own cadence and TTL, not the legacy agent heartbeat.
- Correct the lane taxonomy: VM is a remote workspace only (vm_backend.md:26) — the dispatcher's VM/sandbox workspace pre-filter (main.py:7210-7676) survives statelessness unchanged; the registered-agent path is kept only for compose/bare-metal deploys, officer dedicated pods (exempted in every reaper), and the pinned-pod rollback path.
- Add a deletion ledger appendix (or section) enumerating: registration endpoint + agents table + duplicate-bind 409 dance; heartbeat slot-liveness half (keeping job_status backstop, steering pull, graph-progress stall detector re-homed onto the job row); get_available_agents 30s cooldown + SHA filter + phase-1.5 provision-and-wait; stale_agent_detector steps 1-3b/5; ensure_warm_pool/reap_pods/scale_down_idle/reservation eviction; drain intent choreography; per-session Service/Ingress + pod_uid ownerRefs + 60s session JWTs.
- Address the session-agent PVC (pvc-agent-s-<tid12>, agent_provisioner.py:329-362): a stateless Deployment cannot mount per-thread RWO claims — S2 must either finish externalizing agent-side /workspace state onto the workspace pod or scope S1 to lite sessions where no PVC exists.
- Specify the claim transaction to also bump threads.events_epoch (moving allocation from agent-side persistent_app.py:1476 into the DB-side claim), making epoch the turn fencing token end-to-end; cite apply_thread_rewind (postgres.py:7973) as the existing orchestrator-side bump precedent.
- Name datasource_reconciliation.py as the queue-claim template (FOR UPDATE SKIP LOCKED + lease + never-reused sequence claim_token + leader-gating-for-efficiency-only) and register any new advisory-lock keys in orchestrator/database/lock_ids.py.
- State that turn requests as DB rows also fix two live replica-unsafe spots: the in-process per-turn input lock dict (main.py:30385-30410, stale 'single-instance orchestrator' comment) and the _threads_suspending dedup set — add 'no in-process claim state' as an S1 acceptance criterion, citing bench_sweeper_multi_replica_race as the precedent for why.
- Specify the lease-claim credential contract: queue rows carry only ids + budget + priority + claim token; the claiming pod re-runs the existing resolution pipeline (_inject_dispatch_credentials + datasource reauthorization + grant PEP, already executed on every resume at main.py:4049-4090); LLM keys, env_keys, repo/datasource creds, lite object-store creds never sit in the row (jobs.resolved_config is already stored redacted); workspace SSH stays a deployment-mounted shared secret path, which is already pod-fungible.
- Note per-lane keeps in the 'bug-class graveyard' section: graph-progress stall detection (catches renews-fine-but-stuck, complementary to the lease), freeze-blob shedding for AUTO_REDISPATCH_FREEZE_TYPES, officer fleet notifications (re-sourced from lease-expiry events), and workspace suspension/attention-sleep (which loses only its delete-the-agent-pod step).

## surprises
- A job execution lease ALREADY EXISTS and is live (migration 0054; claim/renew/expire in postgres.py:5933/5235/5504; detector step 4b main.py:1607) — the doc's 'What's missing: turn/batch lease' understates shipped substrate; only pull-claiming, claim-token renewal, batch release, and the stage-4 completion CAS are missing.
- Heartbeat interval is 60s (server-issued, postgres.py:5073; client orchestrator_client.py:399), not the 5s that the lease design doc and multiple postgres.py docstrings claim — with JOB_LEASE_RUN_SECONDS=90 and renew-below-60, one heartbeat late by >30s can expire a healthy job's lease; the '18 missed beats of slack' comment is stale.
- VM lane does NOT require agent identity: vm_backend.md:26 records shipped option 1 — 'the agent pod runs on the main cluster, the VM is a remote workspace only'; session VMs arrive via upgrade-to-workspace-VM (main.py:26718). The doc's 'registered-agent path stays for VM-backed modes' mis-attributes the constraint; the true keep-lanes are compose/bare-metal, officers, and the migration soak path.
- Session agent pods now mount a per-thread PVC (pvc-agent-s-<tid12>, agent_provisioner.py:329-362, fail-closed) — a plain Deployment cannot mount per-thread RWO claims, so S2 has a hard prerequisite the doc doesn't mention.
- The per-turn input lock (main.py:30385, comment 'Single-instance orchestrator') and _threads_suspending dedup set are in-process state on a 2-replica dev deployment — already replica-unsafe today; DB-queued turn requests fix a live latent bug, not just enable statelessness.
- datasource_reconciliation.py already implements the exact stateless-queue pattern (FOR UPDATE SKIP LOCKED + lease + never-reused sequence claim token + leader-gating only for efficiency) — the 'new' control plane has a complete in-repo template.
- events_epoch allocation is agent-side today (persistent_app.py:1476, bump-per-attach RETURNING) — usable as the fencing token exactly as the doc hopes, but the bump must move into the claim transaction; the orchestrator-side rewind path (postgres.py:7973) already proves that shape.
- The bench sweeper race fired 3 times in one 30-pair run (twins 2-5ms apart) despite leader election existing in the codebase — because the sweeper started from a router lifespan and escaped the run_when_leader convention; 'election exists' guarantees nothing without per-claim CAS.

## sources
- docs/features/stateless_agents.md
- docs/go_rewrite.md
- docs/features/job_execution_lease.md
- docs/done/bench_sweeper_multi_replica_race.md
- docs/features/unified_instance_lifecycle.md:1-30
- docs/features/vm_backend.md:26
- docs/features/start_session_on_vm.md:1-40
- orchestrator/database/migrations/app/0054_jobs_execution_lease.sql
- orchestrator/database/migrations/app/0059_docker_workspace_leases.sql
- orchestrator/database/postgres.py:574-586 (lease constants)
- orchestrator/database/postgres.py:4982-5097 (register_agent)
- orchestrator/database/postgres.py:5099-5272 (heartbeat + lease renewal 5235-5260)
- orchestrator/database/postgres.py:5369-5394 (mark_stale_agents_offline)
- orchestrator/database/postgres.py:5396-5502 (recover_orphaned_jobs)
- orchestrator/database/postgres.py:5504-5538 (recover_expired_lease_jobs)
- orchestrator/database/postgres.py:5933-5977 (claim_job_for_agent)
- orchestrator/database/postgres.py:5979-6014 (claim_delegation_resume)
- orchestrator/database/postgres.py:6016-6100 (session wake outbox claim)
- orchestrator/database/postgres.py:6960-6994 (get_available_agents cooldown)
- orchestrator/database/postgres.py:7076-7104 (thread advisory locks)
- orchestrator/database/postgres.py:7469-7752 (thread orphan sweeps, zombie sweeps, gc_offline_agents)
- orchestrator/database/postgres.py:7916-7995 (apply_thread_rewind epoch bump)
- orchestrator/database/postgres.py:8912,9223,10131-10215 (claim_token nextval + guarded writes)
- orchestrator/database/schema_current.sql:4120-4160 (agents table + pod_uid comment)
- orchestrator/database/schema_current.sql:7250-7290 (threads table + events_epoch comment)
- orchestrator/database/migrate.py:168 (migration advisory lock)
- orchestrator/database/lock_ids.py
- orchestrator/main.py:1425-1658 (stale_agent_detector incl. step 4b 1607-1638)
- orchestrator/main.py:1661-1715 (agent_pool_reconciler, lifecycle_reconciler_loop)
- orchestrator/main.py:3201-3550 (_inject_dispatch_credentials)
- orchestrator/main.py:3553-4002 (_dispatch_job_to_agent, JobStartRequest POST 3963)
- orchestrator/main.py:4005-4090 (_resume_job_on_agent reauthorization)
- orchestrator/main.py:4397-4440 (_find_idle_persistent_agent)
- orchestrator/main.py:4443-4619 (_send_session_attach[_locked])
- orchestrator/main.py:5441-5492 (_container_ssh_key_path, _inject_container_workspace_config)
- orchestrator/main.py:6556-6751 (_detach_agent_session, _release_thread_resources, _suspend_thread_resources)
- orchestrator/main.py:7158-7851 (_try_dispatch_pending_jobs: VM pre-filter, claim 7736, phase 1.5, preemption)
- orchestrator/main.py:7854-7887 (auto_assign_dispatcher, _trigger_dispatch leader gate)
- orchestrator/main.py:8233-8272 (JobStartRequest model)
- orchestrator/main.py:9929-10199 (run_when_leader-wrapped loops)
- orchestrator/main.py:11950-11975 (agent_release_job)
- orchestrator/main.py:24808-24882 (register endpoint + duplicate-bind 409)
- orchestrator/main.py:26718-26772 (upgrade-to-vm = workspace VM)
- orchestrator/main.py:27093-27212 (heartbeat endpoint: job_status backstop, guidance/replies, intents)
- orchestrator/main.py:30385-30514 (turn locks, _resolve_thread_for_forwarding, _forward_to_agent, _no_cursor_replay_start)
- orchestrator/main.py:30529-30690 (SSE stream epoch/cursor)
- orchestrator/main.py:30787-30850 (thread input + interrupt forwarding)
- orchestrator/main.py:31883-31980 (attention_sleep_sweeper)
- orchestrator/services/agent_provisioner.py:244-421 (provision_agent + PVC 329-362)
- orchestrator/services/agent_provisioner.py:585-620 (delete_agent_pod_by_thread)
- orchestrator/services/agent_provisioner.py:735-799 (ensure_warm_pool)
- orchestrator/services/agent_provisioner.py:801-881 (reap_pods)
- orchestrator/services/agent_provisioner.py:1081-1235 (scale_down_idle, reservation eviction)
- orchestrator/services/agent_provisioner.py:1620-1694 (_create_pvc)
- orchestrator/services/lifecycle/agent_manager.py:128-215 (is_idle, signal_drain_pending, drain, delete)
- orchestrator/services/lifecycle/reconciler.py:32-92 (DisruptionBudget, tick)
- orchestrator/services/leader_election.py (run_as_leader, run_when_leader, get_leader_generation)
- orchestrator/services/bench.py:157-177 (bench sweep advisory lock)
- orchestrator/services/cron_dispatcher.py:104-190 (SKIP LOCKED claim tick)
- orchestrator/services/datasource_reconciliation.py:1-60,104-215 (claim+lease+claim_token pattern)
- orchestrator/services/session_router.py:45-224 (ensure_route/teardown_route, ownerRefs)
- orchestrator/services/session_tokens.py:28-73 (60s HS256 session JWTs)
- orchestrator/services/provision_or_assign.py:26-231 (session create binding flow)
- orchestrator/services/workspace_lifecycle.py:17-48 (WorkspaceOwner job/session)
- orchestrator/services/vm_provisioner.py:349-390,558,579,1714-1773 (generation fencing, create_vm, create_thread_vm)
- src/api/persistent_app.py:1476-1559 (_resolve_event_journal_epoch, module singletons incl. _loop_user_queue)
- src/api/persistent_app.py:1136-1146 (session heartbeat status)
- src/api/dual_app.py:363-430 (_handle_heartbeat_intents drain semantics)
- src/api/orchestrator_client.py:307,399,1148-1215 (heartbeat interval 60s, run_heartbeat_loop)
