# agent-7

# S3 batch-budget seams: worker driver, freeze machinery, resume lanes, dispatch — findings

## 1. How the worker graph is actually driven (lore correction)

**Workers do NOT run a blind whole-job `ainvoke`.** Both worker app layers call `process_job(job_id, metadata, stream=True)` (`src/api/dual_app.py:818`, `src/api/app.py:606`) → `_process_job_streaming` (`src/agent.py:1328`) → `run_graph_with_streaming` (`src/graph.py:5374`) → **`graph.astream(graph_input, config, stream_mode="values")`** (`src/graph.py:5391`). The non-streaming `ainvoke` (`src/agent.py:1216`) is only the `stream=False` branch — no production caller.

**A cooperative break-out of the astream loop already exists and is battle-tested.** `dual_app.py:819-829`: after every yielded superstep the loop checks `_stop_requested` and `break`s. Set by: `/job/cancel` (`dual_app.py:1112`), `/job/pause` (`:1148`), lifespan drain (`:502`), and the heartbeat DB-status preemption backstop `_check_job_preempted` (`:200-240`). After the break: `_complete_stop` (`:701-719`) = evidence push (commit+push, 60s cap, `:672-698`) → `_reset_to_idle` → signal. **The job is then simply left paused by the orchestrator; the agent does not report completion on this path** (`:832-839`).

`recursion_limit` is set to 1,000,000 (`src/agent.py:986`) — hitting it raises `GraphRecursionError`, an error path; not a batch mechanism.

**LangGraph 1.0.5 (installed, `.venv/.../langgraph/pregel/`) checkpoint-at-boundary semantics, verified in source:**
- `after_tick` (`_loop.py:538-563`): apply writes → emit `values` (line 555) → `_put_checkpoint({"source":"loop"})` (563). The put is submitted to an **ordered background task** (`_loop.py:796-803` — each put awaits the previous).
- Default `durability="async"` (`pregel/main.py:2395`).
- On generator close, `__aexit__` (`_loop.py:1312-1328`) unwinds the stack including `AsyncBackgroundExecutor`, which **"waits for all tasks to finish"** (`_executor.py:122-129`) — so the last superstep's checkpoint IS durably flushed when the astream generator closes. **Caveat**: `break`ing an `async for` does not close the generator immediately; today's code relies on event-loop GC finalization. A batch driver keeping any app-layer break should `await gen.aclose()` explicitly before reporting release.
- `interrupt_after` is available **per-astream-call** (`pregel/main.py:2681-2695`) and raises `GraphInterrupt` *after* `_put_checkpoint` (`_loop.py:563-569`) — a native clean-halt alternative, but unused in this repo; the freeze contract is far more integrated (orchestrator status authority, redispatch machinery).
- `_first()` (`_loop.py:618-723`): `input=None` ⇒ `is_resuming=True` ⇒ "proceed past previous checkpoint" — the loop continues at the next pending node; the START edge (and thus `route_entry`) fires **only** when non-None input is applied. This is the root of the two resume lanes in §4.

## 2. Graph compilation and per-job binding (Q2)

**Compilation is per-job, and the graph is not reusable across jobs as built.** `build_phase_alternation_graph` is called inside `process_job` (`src/agent.py:964-977`); `workflow.compile(checkpointer=...)` at `src/graph.py:5303`. Every node is a closure over job-scoped objects: workspace manager, todo_manager, tool_context, snapshot_manager, checkpointer, bound LLMs, ContextManager, ToolRetryManager, memory service (`graph.py:5148-5221`).

Per-claim (per-batch in S3) binding cost chain:
- `_hydrate_job_brief` if resume metadata is bare (`agent.py:848`).
- `_setup_job_workspace(resume=True)` (`agent.py:1883+`): frozen-config load from `jobs.resolved_config` (`:1944-1957`), SSH reattach, existence probes, pod-handoff clone fallback (`:2466`), seed re-assert, `job_frozen.json` removal (`:890-895`).
- `_setup_job_tools` (`agent.py:2851-3300+`): datasource processing, **MCP `connect_all` per job** (`:2896`), repo clones, credential files, KB bindings, RecallStore init (embedding client), tool loading, `bind_tools` (`:3300-3303`).
- `_create_phase_llms` re-run per job after override merge (`agent.py:2204`) — client construction is cheap (no network).
- `_make_checkpointer` opens a **fresh PG connection per job** (`agent.py:1279-1283`), closed at cleanup (`:1303-1311`).
- Graph build+compile: pure Python + prompt/template loads — milliseconds.
- Resume probe: `aget_state` (checkpoint load; doc's worst observed ≈600 kB).

So the doc's cold-cost estimate ("SSH handshake + config resolve + state load") **understates** today's per-claim cost: MCP discovery, KB/memory store init, credential-file materialization, and a dedicated PG connection all run per claim. Measure before fixing N.

## 3. Freeze machinery end-to-end (Q1 substrate)

**Full freeze_type inventory** (site → teardown → orchestrator status):

| type | set at | workspace writes at freeze | status via `determine_job_status` |
|---|---|---|---|
| `phase_boundary` | `freeze_for_review` `src/core/phase.py:489-584` | job_frozen.json + commit+push(allow_empty) + todo archive | pending_review |
| `job_complete` | `finalize_job` `phase.py:872-1161` | file + commit+tag+push+push_ref + content_tree + archive("final") | completed/reviewing/pending_review (`completion.py:1462-1469`) |
| `verdict` | `phase.py:645` (critic) | similar | critic resolution |
| `budget_exceeded` | audited_tools `graph.py:4461-4520` | job_frozen.json + progress-committer flush | pending_review (NOT auto-redispatch, by design `graph.py:4467-4469`) |
| `llm_unavailable` | execute `graph.py:~2332-2400` | — | paused + backoff sweeper, ceilings (`completion.py:1511-1612`) |
| `version_upgrade` | handle_transition drain check `graph.py:3549-3585` | **job_frozen.json only** + `should_stop`+`freeze_data`+`error=None` | paused (`completion.py:1484-1489`) |
| `blocking_message` | messaging tool → `ToolContext._freeze_request`, consumed same superstep in audited_tools (`graph.py:4852`) | — | waiting_for_reply |
| `delegation` | delegate_work.py | — | waiting (`completion.py:1472`) |
| `workspace_upgrade_required` / `vm_upgrade_required` | upgrade.py / shell_tools.py | — | paused (`completion.py:1474-1483`) |
| `memory_unavailable` | `agent.py:932-947` (pre-graph) | — | paused w/ retry cap (`completion.py:1490-1510`) |

Flow: node returns `{should_stop: True, freeze_data}` → `route_after_check_todos`/`route_after_transition` route to `check_goal` (`graph.py:3789, 4151`) → `check_goal` echoes stop (`:3621-3656`) → END. App layer posts `{should_stop, goal_achieved, error, freeze_data}` to `/api/jobs/{id}/complete` (`src/api/orchestrator_client.py:1601-1628`).

**Teardown on every job end** (streaming `finally`, `agent.py:1433-1457`): bounded memory drain (aux timeout / 60s) → `_cleanup_shell_manager` → **`ShellManager.cleanup()` kills the workspace tmux session** (`src/tools/shell/shell_manager.py:591-594`; session name deterministic `agent_{job_id[:12]}`, `:374`) → datasource close → checkpointer close. Then `report_completion` + idle heartbeat (`dual_app.py:841-865`).

**What does NOT happen at freeze**: no workspace snapshot (PhaseSnapshotManager snapshots run at `archive_phase` during normal operation, `graph.py:2869-2887`, and land on the **agent pod's local disk**, `phase_snapshot.py:198-333`). No WebDAV. Orchestrator archives/reaps the workspace **only** for completed/failed (`main.py:18352-18363`); a `paused` job's workspace pod stays warm for `WORKSPACE_PAUSED_REAP_GRACE_S` (default = `WORKSPACE_IDLE_TIMEOUT`·60 = **30 min**, `orchestrator/services/lifecycle/workspace_manager.py:131-150`), then snapshot-and-reap.

## 4. The resume path — two lanes with very different fidelity (Q1/Q4 critical)

Dispatcher resume: `get_dispatchable_jobs` (`postgres.py:6885-6957`: `status IN ('created','paused') AND assigned_agent_id IS NULL AND freeze_data IS NULL`, partial index 0046, cascade guard, `ORDER BY priority DESC, created_at ASC`) → `claim_job_for_agent` CAS (`postgres.py:5933-5977`: sets `processing` + agent + **180s pickup lease** + clears stale errors) → `resume_lane_applies` (`orchestrator/services/dispatch_guards.py:43-58`: paused AND `job_has_checkpoint` PG probe `postgres.py:1783-1816`, fails open) → `_resume_job_on_agent` (`main.py:4005-4324`): re-resolves datasources, re-checks grants, **re-injects credentials** (`_inject_dispatch_credentials`, `:4129`), re-injects container/VM/lite workspace connection (`:4140-4216`), pops queued feedback/delegation (`:4218-4227`), POSTs `/job/resume` with `previous_status` = pre-claim row status.

Agent side (`process_job`): `GRACEFUL_STOP_STATUSES = {cancelled, paused, pending_review, waiting}` (`agent.py:993-998`) → `_resume_from_checkpoint` (`:3932-3993`, `aget_state` with thread_id=job_id, legacy fallback) → `graph_input=None`. Crash statuses (`processing`/`failed`/None) → `_resume_from_snapshot` (`:3995-4113`).

**Lane A — END-lane resume (in-graph freeze):** the prior run reached END with `should_stop=True` persisted. `astream(None)` would run **zero nodes** (`agent.py:1108-1120`). For `_AUTO_CONTINUE_FREEZE_TYPES = {version_upgrade, llm_unavailable, memory_unavailable, kb_unavailable, workspace_upgrade_required}` (`agent.py:77-85`) the agent clears the terminal flags via `aupdate_state(as_node="__start__")` (`:1138-1147`) → next stream enters via `route_entry` (`graph.py:3796-3809`) → **`restore_todo_state`** (`graph.py:3828-3882`): TodoManager rehydrated from checkpointed `todos/staged_todos/todo_next_id`, staged-todo promotion + tactical flip (`:3861-3877`), stop flags cleared. **Clean re-entry.**

**Lane B — mid-loop resume (app-layer break: pause/cancel/preempt/drain-timeout; also crash):** the checkpoint sits mid-loop; `astream(None)` resumes the pending node directly (verified `_loop.py:682-691`) — `route_entry`/`restore_todo_state` **never run**. The successor process's TodoManager is a fresh empty object (`src/managers/todo.py:133-141` — pure in-memory; the CLAUDE.md "todos.yaml" tracking claim is stale, todos are memory + checkpoint-state only). Self-heal is **lossy**: `check_todos` with no todos in a tactical phase **forces `phase_complete`** (ends the phase early, `graph.py:2703-2707`); strategic reloads template todos (`:2711-2729`). `restore_from_feedback`'s own comment confirms the pattern: "This node is the entry point on a feedback resume (restore_todo_state never ran), so the manager is empty" (`graph.py:4018-4025`).

**Crash-lane surprise:** phase snapshots are pod-local (`phase_snapshot.py` writes under the agent pod's disk), so a cross-pod crash resume with `previous_status='processing'` finds no snapshot → `create_initial_state` → **job restarts from scratch** (tripwire `_note_resume_without_checkpoint`, `agent.py:1063-1065, 3483`). D3's verified cross-pod resume works because the sweeps (`recover_expired_lease_jobs` `postgres.py:5504-5538`, `recover_orphaned_jobs` `:5396-5502`) flip the row to `paused` **before** re-dispatch — routing it into the graceful checkpoint lane.

**⇒ The batch driver must produce Lane A**: an in-graph `batch_boundary` freeze that reaches END, added to `_AUTO_CONTINUE_FREEZE_TYPES` — not an app-layer astream break, which inherits Lane B's empty-TodoManager degradation (a bug today's pause lane already has).

## 5. Orchestrator dispatch / lease / heartbeat (Q3 substrate)

**A job execution lease already exists** (migration `0054_jobs_execution_lease.sql`, `docs/features/job_execution_lease.md`, stages 1-3 shipped): claim sets 180s pickup lease (`JOB_LEASE_PICKUP_SECONDS`, `postgres.py:579`); heartbeats renew to 90s when <60s remain (`postgres.py:5235-5260`, `JOB_LEASE_RUN_SECONDS=90`, `RENEW_BELOW=60`; fenced by `assigned_agent_id = $2`); `recover_expired_lease_jobs` (`:5504-5538`) is the primary orphan recovery: expiry ⇒ `paused` + unassigned + lease NULL ⇒ re-enters dispatch. The doc's "What's missing: turn/batch lease" is **~70% built** — what's missing is renewal decoupled from the agent-identity heartbeat, and stages 4-5 (completion CAS + `lost_lease` intent).

Dispatch cadence: `auto_assign_dispatcher` 30s poll (`main.py:7854-7874`) + event-driven `_trigger_dispatch` (leader-gated, `:7877-7887`); **`/complete` fires `_trigger_dispatch()` at step 6 (`main.py:18350`)**, so a batch release re-enters dispatch immediately. Agent matching: `get_available_agents` (`postgres.py:6960-6994`) = `status='ready'`, worker/dual, **30s cooldown since `last_completed_at`**; stale-image SHA skip (`main.py:7699-7717`); pod provisioning for unmatched (7762-7784); priority preemption (7786-7848).

**Heartbeat contract** (60s, registration-issued, `postgres.py:5073`; endpoint `main.py:27093-27212`). Response carries: `intents` (drain, set on the agent row by the lifecycle reconciler), `job_status` of `current_job_id` (out-of-band preemption backstop), `pending_guidance` (list from `jobs.context.pending_guidance` — list = prune signal, None = keep inbox), `queued_replies`. Side effects: renews job lease when `status='working'` (`postgres.py:5242-5260`); tracks workspace `last_activity` (`main.py:27141-27148`).

Agent-side consumption (`dual_app.py`): `_handle_heartbeat_intents` (363-434) — order: preemption backstop `_check_job_preempted` (200-240, deny-list `{failed,cancelled,paused}` → `_request_stop` → astream break), guidance inbox refresh (337-360, process-local dicts 254-265), drain intent (idle → `os._exit(0)`; busy → flag; graph reads it at phase boundaries via `is_drain_requested` (179-185) → `version_upgrade` freeze `graph.py:3558-3585`). Guidance renders **every execute turn** (`graph.py:1148-1163` via `_get_pending_supervisor_guidance:3228` — dual_app inbox pull), acked fire-and-forget (`dual_app.py:278-307`, at-least-once, survives pod death via job context). Queued replies: natural-break drain (todo-complete + wall-clock floor, `graph.py:3257+`) **plus a DB-direct phase-boundary backstop** `_process_queued_replies` (`graph.py:3433-3445` — "outside the dual app there is no heartbeat inbox to read, so the DB is the only source"). **The DB-pull pattern batch mode needs already exists for replies.**

`determine_job_status` (`completion.py:1283-1626`): **unknown freeze types fall through to `pending_review` (`:1626`)** — `batch_boundary` must be an explicit branch. `ERROR_IMMUNE_FREEZE_TYPES = AUTO_REDISPATCH | {llm_unavailable}` (`:361-363`) protects clean-boundary freezes from coincident-error hard-fails. Subjobs short-circuit: non-listed freeze types on a subjob → pending_review (`:1436-1444`, `_SUBJOB_REDISPATCH_FREEZE_TYPES` `:382-389`). Pause plumbing in `/complete`: clear agent on paused (`main.py:18020-18031`), **shed freeze to `context.last_freeze_data` for `AUTO_REDISPATCH_FREEZE_TYPES`** (`:18033-18068` — required because `get_dispatchable_jobs` demands `freeze_data IS NULL`), progress-aware drain-livelock counter (`completion.py:948-981`, `main.py:18070-18104`). A third hardcoded copy of the auto-redispatch list lives in `recover_orphaned_jobs` result4 SQL (`postgres.py:5477-5491` — "keep the two lists in sync").

## 6. Q1 answer — minimal-diff `batch_boundary` design

**Template = the `version_upgrade` drain freeze** (`graph.py:3549-3585`), which is already "release at the next phase boundary for any pod to continue". Diff:

Agent:
1. Phase-boundary batch edge: alongside `_is_drain_requested()` in `handle_transition`, check `state["iteration"] - batch_start_iteration >= N` (batch_start captured at claim; `iteration` is checkpointed state, `state.py:84` — durable for free). Mid-phase superstep cap (if wanted): same check in `check_todos`/`audited_tools`, the `budget_exceeded` shape (`graph.py:4455-4520`) minus the file write — but see carrier caveat in §7 item 5.
2. Freeze payload: `{"freeze_type":"batch_boundary", "phase", "phase_number", "reason", "batch_supersteps"}`, `should_stop=True`, `error=None`.
3. **Can skip vs today's freezes** (all verified skippable):
   - `output/job_frozen.json` write — nothing requires it on the paused lane: `route_after_transition` reads only state (`graph.py:4150-4152`); resume merely deletes it if present (`agent.py:891-895`); the orchestrator persists freeze_data from the report and stashes it to context anyway.
   - git commit/push — `ProgressCommitter` already pushes on its own clock (`src/core/progress_commit.py`); the workspace pod persists, the successor SSHes into the same tree. Optional single flush for observability.
   - todo archive — keep the manager state; `check_todos` exports todos into checkpointed state every loop pass (`graph.py:2741-2756`).
   - evidence push (`dual_app.py:675-698`) — that's the app-break path; the in-graph freeze doesn't route through `_complete_stop`.
   - **tmux kill — must skip** (conditional in `_cleanup_shell_manager` / streaming `finally` when the final state's freeze_type is batch_boundary). Session name is deterministic per job ⇒ cross-batch shell continuity is free.
   - **Must keep**: bounded memory drain (`agent.py:1440-1451`); checkpointer/datasource close.
4. Add `"batch_boundary"` to `_AUTO_CONTINUE_FREEZE_TYPES` (`agent.py:77`) → Lane-A resume: `__start__` clear → `route_entry` → `restore_todo_state` → execute.

Orchestrator (4 touch points, 3 of them the documented in-sync lists):
5. `determine_job_status`: explicit `freeze_type == "batch_boundary"` → `("paused", None)` (default is pending_review!).
6. `AUTO_REDISPATCH_FREEZE_TYPES` (`completion.py:340`) — gets freeze-shed + agent-clear + ERROR_IMMUNE for free.
7. `recover_orphaned_jobs` result4 SQL literal list (`postgres.py:5486-5489`).
8. If subjobs (critic/scholar) batch: `_SUBJOB_REDISPATCH_FREEZE_TYPES` (`completion.py:382`).

Re-dispatch is then automatic: `/complete` → paused+unassigned+freeze-NULL → `_trigger_dispatch()` (`main.py:18350`) → claim → `/job/resume` with `previous_status='paused'` → graceful checkpoint lane.

**Handoff latency floor today**: another ready pod ⇒ ~seconds (event trigger). Same pod ⇒ blocked 30s by the completion cooldown (`postgres.py:6986-6987` + `last_completed_at` set by the completion heartbeat) — a lone pod serving a lone job would duty-cycle batch/30s-idle. Either waive cooldown for batch continuations or keep N·superstep-duration ≫ 30s.

## 7. Q4 answer — worker in-process state NOT covered by the PG checkpoint

**DB-backed** (in `UniversalAgentState`, checkpointed every superstep — `src/core/state.py:74-161`): messages; todos/staged_todos/todo_next_id (synced at `check_todos` `graph.py:2741-2756` + all freeze paths); phase_number/is_strategic_phase/is_final_phase/phase_complete; iteration; turn_count; last_observed_turn/last_assembled_turn; phase_instruction_injections; context_stats; tool_retry_state; completion_decision/verdict_decision (journal-before-observe mirrors; process cache re-seeded from orchestrator on resume `agent.py:1194-1209`); last_archived_phase (exactly-once archive guard `graph.py:2797-2813`); freeze_data/error/consecutive_llm_errors. Outside the checkpoint but durable: `jobs.resolved_config`, `context.pending_guidance/queued_replies/queued_feedback/delegation_results`, workspace files + Gitea, tmux state on the workspace pod (iff not killed).

**Reloadable per claim** (cost, not correctness): LLM clients + bind_tools (`agent.py:2204, 3300-3303`); tools/ToolContext; datasource + MCP connections (`:2896`); RecallStore/KB stores; per-job PG checkpoint connection (`:1279`); prompts.

**Needs-home / needs-decision:**
1. **`_job_tool_call_count`** (`graph.py:4217`) — the job-level tool budget, closure-local, "never reset at a phase boundary" — but reset by every process/graph rebuild. **Already broken across today's resumes**; batching makes the job cap effectively per-batch (N× looser). Home: state key or `jobs.context` counter. Same for the freeze report fields (`:4488-4489`).
2. **TodoManager in-memory todos on Lane-B resumes** (`managers/todo.py:133-141`) — see §4; fix by hydrating the manager from checkpoint values in `process_job` on any resume, or by always using Lane-A freezes.
3. **tmux teardown** (`shell_manager.py:591-594` via `agent.py:1313-1320`, streaming finally `:1455`) — must become freeze-type-conditional or every batch boundary kills long-running workspace commands.
4. **`ToolContext._replan_request`** (`context.py:292-299`): set in the tools superstep, consumed in the **next** superstep's `check_todos` (`graph.py:2685`). An app-layer break between them silently drops a requested replan (today's pause has this hole too). `_freeze_request` is safe (set+consumed inside one audited_tools superstep, `graph.py:4852`). A mid-phase superstep-budget break must fire only when these carriers are empty — or they move into state.
5. **In-flight aux tasks**: memory `capture_nowait` (drained bounded at gen-finally `agent.py:1440-1451` — keep); citation verifications (`await_pending_verifications(15)` only at archive_phase `graph.py:2826`); curation/KB-convergence `asyncio.create_task` at archive_phase (`:2840-3010`) — die with the process if release follows a boundary immediately. Same exposure as today's job-end; acceptable, document it.
6. `_delivered_reply_keys` (`context.py:306-314`) — documented deliberately process-local, at-least-once redelivery on a successor pod. Batch mode redelivers once per claim if acks lag — cosmetic duplicates.
7. **Read-before-write authorization caches**: `_recent_reads` (deque 10), `_recent_read_versions`, `_pinned_reads`, `_instruction_read_stamps` (`context.py:238-252`) — cleared per claim ⇒ the model must re-read files before editing after every batch boundary. A recurring token/latency tax per batch; candidate for checkpointing if the A/B shows it.
8. `_source_registry` / `_cloud_anchors` / `_inaccessible_sources` (`context.py:226-237`) — citation caches; verify re-registration doesn't duplicate sources under frequent re-claims.

**Cache-ok** (hysteresis; resets are the same class as today's per-phase resets `graph.py:4443-4453`): stuck-detection `_tool_call_history` deque(30)/`_calls_since_progress`/`_reflection_injected`/`_warned_signatures`/`_category_failures`/`_process_only_streak`/`_TOOL_TIMEOUT_RETRIES`/`_phase_tool_call_count`/`_last_phase_number` (`graph.py:4210-4243`); execute streaks `_tool_use_failed_streak`/`_llm_error_streak`/`_degeneration_streak`/`_empty_response_streak`/`_no_tool_call_streak` (`graph.py:~656-676`); `ProgressCommitter._last_push/_last_commit` monotonic clocks (`progress_commit.py:104-113`); `ContextManager._state` incl. `last_provider_input_tokens` compaction anchor (`context.py:733-741, 1014` — re-anchors on the batch's first LLM response); `ToolRetryManager._failure_counts` (`context.py:2408-2432`); `ToolContext._graph_progress` (`context.py:282, 423-434`); heartbeat inboxes (`dual_app.py:254-265`); SSH/HTTP connection pools; `memory_health` counters.

**Hard**: nothing worker-side. No client-facing stream, no per-turn attachment. The hard residue is all sessions (canvas/IDE/browser), out of S3 scope.

## 8. Q3 answer — steering a batch worker with no heartbeat

Every lane that rides the heartbeat today has a natural replacement, and two already have DB-pull precedents:
- **Lease renewal** → a background asyncio task held for the duration of the claim (NOT a hook on the astream consumer: `stream_mode="values"` emits nothing while a node sits inside a 10-minute tmux tool call, so consumer-side renewal starves exactly when the doc's OQ1 worries — the tool-wait). The renewal UPDATE is already fenced by `assigned_agent_id` (`postgres.py:5248`).
- **Preemption backstop (`job_status`)** → the renewal statement can `RETURNING status`; agent breaks the batch when the row leaves `processing`. Replaces `_check_job_preempted` 1:1.
- **`pending_guidance` / `queued_replies`** → swap the dual_app inbox reads (`graph.py:1155, 3242-3254`) for the DB-direct pattern that already exists at phase boundaries (`_process_queued_replies`, `graph.py:3096-3138, 3433`); or return both in the renewal round trip. Latency improves from ≤60s+turn to ≤turn.
- **Drain intent** → dissolves: a draining pod doesn't claim the next batch; mid-batch SIGTERM keeps the existing cooperative-stop machinery.
- **Agent metrics/graph_progress** → per-batch annotations on the job (doc's observability point).

## 9. Fairness trap in today's dispatch ordering

`get_dispatchable_jobs` orders `priority DESC, created_at ASC` (`postgres.py:6953`). Under batch rotation every release re-enters with its original `created_at`, so **the oldest jobs win every claim cycle and the newest starve** — the opposite of the doc's stated fairness win ("all 15 make progress"). Fairness needs a rotation key (e.g. `last_released_at ASC` within priority, or the lease column doubling as it). Today this never mattered because a claimed job ran to completion.

## design_implications
- S3 batch driver should be an IN-GRAPH freeze (clone the version_upgrade drain check in handle_transition, graph.py:3549-3585), not an app-layer astream break: only the END-lane resume runs restore_todo_state; the break lane resumes mid-loop with an empty TodoManager and lossy self-heal (check_todos force-ends tactical phases, graph.py:2703-2707). State the lane distinction in the doc.
- batch_boundary requires FOUR orchestrator/agent list touches, three of them documented keep-in-sync lists: explicit ('paused', None) branch in determine_job_status (unknown freeze types default to pending_review, completion.py:1626), AUTO_REDISPATCH_FREEZE_TYPES (completion.py:340), recover_orphaned_jobs result4 SQL literals (postgres.py:5486-5489), and agent-side _AUTO_CONTINUE_FREEZE_TYPES (agent.py:77). Add _SUBJOB_REDISPATCH_FREEZE_TYPES (completion.py:382) if critic/scholar subjobs batch.
- Rewrite the doc's 'What's missing: Turn/batch lease' — a job execution lease shipped (migration 0054, claim=180s pickup, heartbeat-renewed 90s, recover_expired_lease_jobs as primary orphan recovery, docs/features/job_execution_lease.md). What's actually missing is renewal decoupled from the agent-identity heartbeat (background task per claim) plus lease-doc stages 4-5.
- Answer open question 1 (lease TTL vs long tools) as: background asyncio renewal task per claim, NOT a hook on the astream loop or tool-wait — stream_mode='values' yields nothing during a 10-minute tool call, so consumer-side renewal starves exactly then. Renewal UPDATE is already fenced by assigned_agent_id and can RETURN job status + pending_guidance, replacing the heartbeat's preemption backstop and steering pull in one round trip.
- The cheap teardown for batch_boundary is concretely: skip job_frozen.json (route_after_transition reads only state; resume tolerates absence), skip git push (ProgressCommitter already pushes on its own clock), skip todo archive (todos checkpoint every loop pass), and make the tmux kill conditional (ShellManager.cleanup kills the workspace tmux at every job end today, shell_manager.py:591-594; session name agent_{job_id[:12]} is deterministic so skipping the kill gives cross-batch shell continuity for free). Keep the bounded memory drain.
- Fix (or explicitly bypass) the mid-loop TodoManager hole as a prerequisite or companion: hydrate TodoManager from checkpoint values in process_job on any resume. This also fixes today's pause + cross-pod resume, which silently force-ends tactical phases.
- Rehome _job_tool_call_count (graph.py:4217): the job-level tool budget is closure-local, already resets on every resume today, and becomes per-batch (N-times looser) under batching. Move to checkpointed state or jobs.context.
- Fix dispatch fairness before claiming it: get_dispatchable_jobs orders priority DESC, created_at ASC (postgres.py:6953), so batch rotation lets the oldest jobs win every cycle and starves the newest — add a rotation key (last_released_at within priority). Also address the 30s agent cooldown (postgres.py:6986): a releasing pod cannot reclaim for 30s, so lone-pod/lone-job deployments duty-cycle at batch/(batch+30s) — waive cooldown for batch continuations or require batch duration >> 30s.
- Correct the doc's cold-cost estimate for workers: a claim today runs brief hydration + workspace SSH reattach/probes/clone-fallback + full _setup_job_tools (MCP connect_all, KB/memory store init, credential files) + LLM rebuild + a fresh per-job PG checkpoint connection + aget_state — well beyond 'SSH handshake + config resolve + state load'. Measure per-claim setup on the Job Bench harness before fixing N; soft affinity that skips re-setup when re-claiming the same job is worth more than the doc implies.
- Document the mid-phase batch-break caveat: ToolContext._replan_request is set in the tools superstep and consumed in the NEXT superstep's check_todos (graph.py:2685) — a superstep-budget break between them drops a requested replan. Mid-phase batch edges must check transient carriers are empty, or the carriers move into checkpointed state; phase-boundary-only batching avoids the issue entirely.
- Note the workspace warm-grace coupling: paused job workspaces are reaped after WORKSPACE_PAUSED_REAP_GRACE_S (default 30 min, workspace_manager.py:131-150). Batch queue wait must stay well under it, or batch_boundary pauses need a reap carve-out like infra_transient_retry_pending (workspace_manager.py:180-217).
- Steering for batch workers: swap the execute node's dual_app inbox reads (graph.py:1155) for the DB-direct pattern that already exists at phase boundaries (_process_queued_replies, graph.py:3096-3138) — per-turn read of context.pending_guidance. This deletes the heartbeat dependency and improves worst-case steer latency from 60s+turn to one turn.
- If any app-layer break survives in the design, require explicit await gen.aclose() before reporting release: LangGraph 1.0.5 flushes the last superstep's checkpoint on generator close (ordered background put awaited by AsyncBackgroundExecutor.__aexit__), but a bare break leaves the flush to GC timing.
- Mention interrupt_after as the considered-and-rejected native alternative: available per-astream call in langgraph 1.0.5 and checkpoint-consistent, but the freeze contract already carries orchestrator status authority, redispatch, error-immunity and observability — an interrupt would need all of that rebuilt.
- Crash-lane honesty for the doc: pod-local phase snapshots mean previous_status='processing' cross-pod resumes restart the job from scratch; D3 works because the lease/orphan sweeps flip rows to paused first. Stateless workers should retire the snapshot lane for the postgres-checkpointer class (PG checkpoint is strictly better) rather than port it.

## surprises
- Project lore is wrong: workers drive the graph via astream(stream_mode='values') with a per-superstep cooperative break-out already in production (pause/cancel/preempt/drain all use it) — not a monolithic ainvoke. The batch break mechanism half-exists at the app layer.
- A job execution lease already shipped (migration 0054: lease_expires_at, 180s pickup on claim, 90s heartbeat-renewed run lease, recover_expired_lease_jobs as the primary orphan recovery). The doc lists the lease as missing; only heartbeat-decoupled renewal is missing.
- There are TWO resume lanes with different fidelity: an END-lane (in-graph freeze) that runs restore_todo_state and rehydrates todos cleanly, and a mid-loop lane (app-layer break/pause) that skips restore_todo_state — the successor pod runs with an EMPTY TodoManager and self-heals lossily (tactical phase force-ended, graph.py:2703-2707). Today's pause + cross-pod resume silently degrades; batch mode must use the END-lane shape.
- Cross-pod CRASH resume (previous_status='processing') restarts jobs from scratch: phase snapshots are agent-pod-local, so the snapshot lane finds nothing on a successor pod. The D3-verified cross-pod resume works only because lease/orphan sweeps flip rows to 'paused' first, routing into the PG-checkpoint lane.
- determine_job_status routes UNKNOWN freeze types to pending_review (completion.py:1626) — a batch_boundary freeze without its explicit branch would park every batch for human review.
- ShellManager.cleanup kills the workspace tmux session on every job end INCLUDING pause (shell_manager.py:591-594) — workspace shell state does not survive today's pause/resume, contradicting the doc's 'shell state survives in tmux on the workspace pod' as applied to workers. The session name is deterministic, so simply skipping the kill gives cross-batch continuity.
- Dispatch ordering (priority DESC, created_at ASC) actively starves the newest jobs under batch rotation — the oldest job wins every re-claim cycle. The doc's fairness claim ('all 15 make progress') requires a scheduling-key change, not just the batch driver.
- The 30s agent cooldown after completion (get_available_agents) blocks a pod from re-claiming the job it just released for 30s — a lone-pod, lone-job deployment would idle 30s per batch.
- The job-level tool-call budget (_job_tool_call_count, 'never reset at a phase boundary') is closure-local and already resets on every resume today — the 5000-call job cap has never actually spanned a resume.
- job_frozen.json is not needed for the paused/auto-redispatch lane at all: routing reads only graph state, resume tolerates absence, and /complete stashes freeze_data into context — a batch freeze can write nothing to the workspace.
- The DB-pull steering pattern batch mode needs already exists: handle_transition drains queued replies straight from Postgres as the no-heartbeat backstop ('outside the dual app... the DB is the only source', graph.py:3433).
- LangGraph 1.0.5 emits the values event BEFORE submitting the checkpoint put (ordered background task); the flush guarantee comes from generator close — today's break-without-aclose relies on GC finalization timing for checkpoint durability.

## sources
- docs/features/stateless_agents.md
- docs/go_rewrite.md
- docs/features/job_execution_lease.md:1-60
- src/agent.py:60-118 (_AUTO_CONTINUE_FREEZE_TYPES)
- src/agent.py:790-1256 (process_job)
- src/agent.py:1258-1320 (_make_checkpointer, cleanup)
- src/agent.py:1328-1457 (_process_job_streaming, finally teardown)
- src/agent.py:1883-1960 (_setup_job_workspace resume/frozen config)
- src/agent.py:2851-3303 (_setup_job_tools, bind_tools)
- src/agent.py:3932-4123 (_resume_from_checkpoint/_resume_from_snapshot)
- src/graph.py:575-676 (execute closure streaks)
- src/graph.py:1148-1163 (guidance injection per turn)
- src/graph.py:2660-2757 (check_todos, todo export, lossy self-heal)
- src/graph.py:2762-3093 (archive_phase: snapshot, compaction, aux tasks)
- src/graph.py:3096-3138, 3211-3254, 3433-3445 (queued replies DB backstop, drain flag, inbox pulls)
- src/graph.py:3412-3586 (handle_transition, version_upgrade freeze template)
- src/graph.py:3596-3760 (check_goal stop)
- src/graph.py:3784-3882 (routes, restore_todo_state)
- src/graph.py:3887-4123 (restore_from_feedback, 'manager is empty' comment)
- src/graph.py:4126-4165 (route_after_transition reads state only)
- src/graph.py:4173-4520 (audited tool node closures: fingerprints, budgets, budget_exceeded freeze)
- src/graph.py:4852 (freeze_request consumed same superstep)
- src/graph.py:4946-5312 (build_phase_alternation_graph, compile per job)
- src/graph.py:5374-5392 (run_graph_with_streaming astream values)
- src/core/state.py:21-238 (UniversalAgentState schema)
- src/core/phase.py:459-584 (freeze_for_review), 775-837 (_push_job_ending_state), 872-1161 (finalize_job), 1164-1268 (phase git, push_evidence_snapshot)
- src/core/phase_snapshot.py:198-333 (pod-local snapshots)
- src/core/progress_commit.py:67-113 (ProgressCommitter clocks)
- src/core/context.py:727-741, 1003-1030 (ContextManagementState, compaction anchor), 2408-2432 (ToolRetryManager)
- src/tools/context.py:163-434 (ToolContext state inventory, _delivered_reply_keys, _replan_request, graph_progress)
- src/managers/todo.py:82-150 (TodoManager pure in-memory)
- src/tools/shell/shell_manager.py:291-378, 591-598 (deterministic session name, cleanup kills tmux)
- src/api/dual_app.py:60-92 (stop machinery), 168-240 (drain flag, preemption backstop), 254-360 (guidance inboxes), 363-434 (_handle_heartbeat_intents), 442-546 (lifespan drain), 591-719 (_reset_to_idle, evidence push, _complete_stop), 810-884 (job runner astream break)
- src/api/orchestrator_client.py:307, 399, 1148-1210 (heartbeat interval 60s), 1601-1628 (report_completion payload)
- orchestrator/main.py:3553, 4005-4324 (_resume_job_on_agent payload, credential/workspace reinjection)
- orchestrator/main.py:7660-7887 (dispatcher: claim, resume lane, provisioning, preemption, 30s poll, _trigger_dispatch)
- orchestrator/main.py:17339, 17600-17832 (complete handler: recovery, memory/llm pause arms)
- orchestrator/main.py:18020-18104 (pause clears agent, freeze shed, drain livelock counter)
- orchestrator/main.py:18330-18369 (trigger dispatch step 6, cleanup only for completed/failed)
- orchestrator/main.py:27093-27212 (heartbeat endpoint response contract)
- orchestrator/services/completion.py:340-389 (AUTO_REDISPATCH/ERROR_IMMUNE/subjob sets), 461-477, 948-981, 1283-1626 (determine_job_status, pending_review default)
- orchestrator/services/dispatch_guards.py:43-58 (resume_lane_applies)
- orchestrator/services/lifecycle/workspace_manager.py:49-217 (paused grace, infra_transient carve-out)
- orchestrator/database/postgres.py:579-586 (lease constants), 1584-1661 (pause_job, pause_job_shed_freeze), 1783-1857 (job_has_checkpoint, delete_checkpoint_thread), 5195-5272 (heartbeat SQL + lease renewal), 5396-5538 (recover_orphaned_jobs, recover_expired_lease_jobs), 5933-5977 (claim_job_for_agent CAS + pickup lease), 6885-6958 (get_dispatchable_jobs + ordering), 6960-6994 (get_available_agents cooldown), 5073 (heartbeat_interval_seconds=60)
- orchestrator/database/migrations/app/0054_jobs_execution_lease.sql
- .venv/lib/python3.13/site-packages/langgraph/pregel/_loop.py:538-569 (after_tick emit-then-put, interrupt_after), 618-723 (_first resume semantics), 780-830 (ordered background put, durability exit), 1262-1328 (__aenter__/__aexit__)
- .venv/lib/python3.13/site-packages/langgraph/pregel/_executor.py:122-129 (AsyncBackgroundExecutor waits on exit)
- .venv/lib/python3.13/site-packages/langgraph/pregel/main.py:2394-2395, 2681-2695 (durability default async, astream interrupt params)
