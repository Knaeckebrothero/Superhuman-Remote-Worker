# agent-4

# In-process state inventory: persistent-session agent → stateless turns

Scope examined: `src/api/persistent_app.py` (8313 lines), `src/api/persistent_session.py` (2438), `src/persistent_graph.py` (2576), `src/api/dual_app.py`, `src/services/cloud_sync/`, `src/services/cloud_mount/`, `src/services/memory/`, `src/citation_engine/engine.py`, `src/tools/context.py`, `src/tools/shell/shell_manager.py`, `src/database/postgres_db.py`, plus the two issue docs (both now in `docs/done/`, not `docs/issues/` — the doc's link to `docs/issues/session_turn_end_cloud_push_blocks_queued_input.md` is stale; fix shipped `99b87008`, k3d-verified 08-07).

Classification key: (a) already DB-backed · (b) trivially reloadable per turn · (c) needs a new DB home · (d) needs a queued-work home · (e) per-pod cache acceptable · (f) genuinely hard.

## 1. Module globals in persistent_app.py (all lines from current tree)

**Process-singleton layer** (dies with pod-per-agent model):
- `_agent: UniversalAgent` (:64) — boot config, `postgres_conn`/`vector_conn` pools (src/agent.py:764/779), boot LLMs `_llm`/`_tactical_llm`/`_auxiliary_llm` (agent.py:277/292). **(b/e)** — pools and LLM client objects are per-pod infrastructure; attach already rebuilds the *session* LLM from the resolved blob (persistent_app.py:1815).
- `_orchestrator_client` (:66), `_heartbeat_task` (:67, spawned :1139), `_started_at` (:68) — registration + 60s heartbeat (`register()` orchestrator_client.py:343, interval learned at :399). **Deleted** under stateless per the doc's own claim; heartbeat currently also carries `graph_progress`, RSS/CPU, aux-health and memory-health counters (`_get_agent_metrics` :864-900) — the aux-degraded admin badge and contained-memory-failure surfacing lose their transport and need a new one (per-batch report at lease release, or derive from `llm_requests`). **(c-small)**
- `_sessions_served`/`_max_sessions_per_process` (:75-78) — pool-mode "restart after N sessions to guard against state leakage" valve (:2451-2459). Evidence the global-singleton design is known-fragile. Deleted.
- `_pending_exit_task` (:81), `_drain_intent_handled`/`_drain_deferred_logged` (:88-89), `_awaiting_input` (:94), `_terminating` (:112), `_watchdog_tasks`/`_ws_connected_event` (:100-101), `_loop_task` (:123) — pod-exit scheduling, drain-suspend state machine (`_handle_heartbeat_intents` :593, `_session_parked` :650, `_drain_suspend_session` :663), boot-WS watchdog (:739) and thread-status watchdog (:776), teardown re-entrancy guard. All exist to manage a long-lived pod's lifecycle; the entire cluster **dissolves** (doc's bug-graveyard claim checks out — the drain-suspend arc in `docs/done/session_agent_drift_drain_kills_idle_sessions.md` is ~200 lines of choreography incl. a re-entrancy race and a 409 resume race that a lease model never has). One caveat: the boot-WS watchdog's *function* (abandoned-session cleanup) and the status watchdog's (out-of-band `ended` detection) move to the orchestrator, which already has overlapping reconciler/sweeper logic.
- `_subscribers: Dict[str, asyncio.Queue]` (:159, maxsize 1000, drop-oldest) — the WS fan-out hub. Transport moves to journal-SSE/orchestrator (P5/P6). **But `_subscribers` is also the tethered/untethered presence oracle**: `_wait_for_permission_resolution` CAS-expires a gate only `if not _subscribers` (:4795-4805); the awaiting_user flip fires on `not _subscribers` (:4205, :5019). Presence must be re-homed orchestrator-side (it partly is — SSE consumers) or the permission-expiry and attention-sleep semantics silently change. **(c)**
- Canvas presence: `_CanvasAwarenessLease` (:169), `_canvas_awareness` (:176), `_canvas_control_validation_at` (:177), `_canvas_source_updates` (:178), `_canvas_presentation_updates` (:179); TTL 15-60s expiry tasks (:3807, :3857); cleared per attach/detach (:3837). Pure live co-editing presence among WS clients — canvas *content* is orchestrator REST rows (a). Presence is (e)-ephemeral but its host must move with the control transport. The tool-originated invalidation path (`canvas_event_callback` → `_broadcast`, :3731) rides the journal and is stateless-compatible.
- Loop input primitives: `_loop_user_queue` (:189), `_loop_interrupt_flag` (:197), `_hard_interrupt_event` (:204), `_loop_last_user_content` (:205). See §5 for the queue. Interrupt: tri-state flag set by POST /api/interrupt or WS, consumed at 3 poll sites in the graph (`_loop_check_interrupt` :4295); "hard" additionally sets an `asyncio.Event` to tear down a blocked LLM stream/summarizer await. Under stateless, graceful interrupts can be a DB flag polled at the same sites; **hard interrupt needs a live signal into the lease-holding pod** — route the POST to the pod recorded on the lease, or LISTEN/NOTIFY into the turn executor. **(c)**
- `_rewind_lock` (:209) — in-process serialization of rewind vs. turn. Becomes the thread lease (a rewind claims the lease like a turn). `_handle_rewind` (:6267) is otherwise DB-shaped already (sweep + ledger + epoch bump). **(a via lease)**
- `_draft_title_value` (:217) — explicitly documented "process memory only… if the pod restarts the draft simply sticks, which is a fine title". **(e)** by design.
- `_tool_inflight` (:222, set/cleared :4359/:4379) and `_turn_event_open` (:229, :5216/:5343) — drive interrupt-mode selection, the welcome frame's `running_tool` (:2992), drain gating, reattach turn-continuity. Both **(b)** inside a turn executor; both are also derivable from the journal (tool.started/completed, turn.started/completed edges) for any outside observer.
- `_cloud_sync_retry_pending` (:238) — one-bit "sync never started, retry at turn boundary" (`_retry_cloud_sync_start` :5148). Dissolves: per-turn construction *is* the retry. **(b)**
- `_pending_cloud_push_task` (:249) — see §6.
- Event journal: `_events_epoch` (:255), `_next_seq` (:256), `_event_writer` (:257) — see §7.
- `_nats_client` (:286, lazy :326) — notification mirror to `session.events.{oid}.{tid}` for the SSE feed (`emit_session_event` :361; methods :291). **(e)** per-pod; under P6 could move orchestrator-side entirely.
- `_announced_permission_rows` (:4516), `_active_permission_request_id` (:4521) — per-turn bookkeeping over DB rows; retire-on-turn-end already runs in `_loop_on_turn_complete` (:5348). Rows themselves are **(a)**; the retire-on-abnormal-exit duty must attach to lease expiry (there is **no `expires_at` sweeper anywhere** — only an active waiter CAS-expires, :4527-4535). **(c-small)**

## 2. PersistentSession fields (persistent_session.py:198-301)

- `messages: List[BaseMessage]` (:214) — the live context. Substrate is **(a)**: message-granular writes (`_loop_persist_message` :5452 per append + accept-time persist :2774 + turn-end reconcile `_save_turn_ai_messages` :6216, converging on one row via `ON CONFLICT (id)`, :6132-6149) + compaction checkpoint (`_record_compaction` :5558 writes `role='summary'` with `boundary_seq`). **But the round-trip is lossy** — see Surprises: restore projection is role/content/tool_calls/tool_call_id only (postgres_db.py:397-398 "HF-7 read diet"), list-shaped content is flattened at write (`_serialize_message_row` :6181-6184 joins `.get("text","")` — image blocks vanish), `thinking` goes to a column but is never restored (`_db_rows_to_lc_messages` :5745 rebuilds plain AIMessage). Today this bites only on rare resumes; per-turn reload makes it the *only* context path. **(a) for text, (c) for full-fidelity content.**
- `turn_count` (:215) — restored from max(turn_number) (:5937, :6010). **(a)**
- `config` (:206) + `permission_mode`/`narration_mode` (:209-211) — config reloads via the resolved blob **(b)**; but `mode.set`/`narration.set` mutate **memory only** (:3099, :3131 — no DB write). Already reverts on resume today; regresses every turn under stateless. **(c)**
- `workspace_manager` + SSH backend — **(e)** with soft affinity; reconnect cost is the doc's 0.3-0.5s. `swap_backend` (:2322) hot-swap machinery becomes unnecessary (next turn just resolves the new backend).
- `tools`/`llm_with_tools`/`tool_context`/`system_prompt`/`context_manager` — rebuilt from config+backend (`_setup_tools` :1246, `_bind_tools` :1972, `_setup_context_manager` :2059). **(b)**. ContextManager's in-memory extras (provider-usage token anchor, `compaction_runs`, `_last_compaction_boundary_id`) reset per pod; the resume path already tolerates this. **(e)**
- `auxiliary_llm` + `AuxiliaryLLM.health` counters — rebuild (b); health counters (e, lost — see heartbeat note above).
- `shell_manager` (:2094) — **stubs only**: "the backend owns the authoritative tab state" (shell_manager.py:372-374), deterministic tmux session name `agent_{thread_id[:12]}` (:374), `ensure_tab` idempotent against the backend. tmux on the workspace pod is the durable shell state (matches doc §"How much already exists" pt 1). **(b/e)**
- `session_task_manager` — **SessionTaskManager is a pure in-memory list** (src/managers/session_tasks.py:45-57, `self._tasks: List`, `_next_id`), no persistence of any kind. Session todos are already lost on any pod recycle today; under stateless they reset every turn. **(c)** — DB table or workspace file.
- `file_checkpoints: Dict[int, List[...]]` (:264) — undo: **full original file contents in agent RAM**, keyed by turn (`snapshot_file` :1990, `undo_turn` :2011). Breaks entirely under stateless. Replacement already exists structurally: per-turn auto-commit + `record_turn_commit` turn→sha ledger (persistent_graph.py:1019-1037, postgres_db.py:635) — the rewind code-restore path. **(c: re-home undo onto the git ledger, delete the RAM snapshots)**
- `workspace_sync` coordinator → per-mount `WorkspaceSyncBase` state: `_local_state` (mtimes), `_remote_state` (etags), `_remote_dirs`, `_pushed_sizes`, `_remote_seeded` (base.py:124-135). The 08-06 fix explicitly redesigned this for pod boundaries: first push/pull seeds dedup from a recursive remote listing, unchanged files cost a `stat`. **(e)** with affinity; cold cost = one remote tree walk per fresh pod (seconds on dev Nextcloud, ~40s on slow k3d). Residual known gap: push doesn't record response etags, so a pull re-downloads just-pushed files once. Legacy `_poll_task` (:137) is dormant (turn-boundary sync replaced polling).
- `cloud_mount_manager` / `overlay_mount_manager` / `_cloud_overlay_monitor_task` / `_protected_mount_id` (:272-283) — see §6, the hardest item.
- `datasources`/`_datasource_clients`/`datasource_configs` (:286-296) — rebuilt per attach from orchestrator payload; MCP `connect_all()` discovery round-trips (:1737) + **process-global `register_mcp_tools(mcp_manager)` TOOL_REGISTRY mutation** (:1780) — fine with one-turn-per-pod, a blocker for later in-process multiplexing. **(b)** with (e) affinity for connection reuse.
- `recall_store`/`knowledge_store`/`memory_service`/`_knowledge_graph` — handles over DBs. **(b)**. `final_memory_extracted` (:248) is teardown-scoped (b).
- `tool_decisions` (:301) — per-turn, persisted with rows at turn save (:5366), cleared (:5372). **(a/b)**
- `_managed_canvas_skill_files`/`_canvas_skill_manifest_owned` (:267-268) — ownership manifest persisted **to the workspace** (`_store_canvas_skill_manifest` :954, marker-checked load :917). **(a-equivalent, workspace-backed)**.
- `user_id`, `project_ids`, `knowledge_bindings` — from thread row / orchestrator payload. **(a/b)**

## 3. ToolContext cross-turn state (src/tools/context.py:163-350)

- `_recent_reads` (deque 10), `_recent_read_versions`, `_pinned_reads`, `_instruction_read_stamps` (:238-252) — **read-before-write authorization + instruction-freshness gates that span turns**. A fresh pod per turn forcibly re-arms them: the agent must re-read a file before editing it even if it read it last turn. Behavioral regression + token cost, or gate-softening, or persist to thread metadata. **(c or an explicit accept-the-regression decision)**
- `_cloud_anchors` (:229) — cloud-read drift fingerprints consumed by `cite_*` possibly turns later; loss degrades citation cloud metadata silently. **(c-small or accept)**
- `_source_registry` (:226), `_inaccessible_sources` (:235) — caches over vector-DB truth. **(e)**
- `_delivered_reply_keys` (:306) — **already designed for pod death**: "Deliberately process-local: a successor pod has no record… so it redelivers — which is the correct at-least-once behaviour". **(e)** — a model comment worth quoting in the doc.
- `_pending_memories` (:283) — sync-tool memory queue. **(d)**
- `_freeze_request`/`_officer_sleep_request`/`_replan_request`/`_reply_drain_requested` (:286-305) — intra-turn signals. **(b)**
- `citation_engine` + `_verify_tasks` — see §4.
- `session_runtime_facts` (:344) — atomically-rebuilt observation (b).

## 4. Complete asyncio.create_task inventory

persistent_app.py: **:536** run_persistent_loop (the loop — replaced by per-turn driver) · **:567** `_loop_completion_handler` (teardown router — per-turn) · **:731** delayed `os._exit` (deleted) · **:836/:840** boot-WS + thread-status watchdogs (deleted) · **:1139** heartbeat loop (deleted) · **:2811** `_early_title_from_prompt` (LLM-free draft title, DB write — (b), can run inline in the input-accept path) · **:2971** per-WS subscriber pump (transport → orchestrator with P6) · **:3057/:3068** `_resolve_pending_permission` approve/deny (DB CAS + NOTIFY — (a); P6 §6a already plans the orchestrator control route) · **:3106** retire-announced rows on mode downgrade ((a)) · **:3147** `_handle_config_update` (persists via orchestrator `update_thread_config` :6971 — under stateless the whole in-place mutation ladder [`_model_swap_fit_ladder` :6744, `resetup_datasources`, `refresh_context_limits`] collapses into "write override, next turn resolves fresh") · **:3160** `_handle_compact` (manual compaction → becomes a lease-holding work item writing a checkpoint row) · **:3168** `_handle_archive` (/done teardown → work item) · **:3172** `_handle_vm_upgrade` + **:3177** `_handle_workspace_upgrade` (**provisioning workflows driven from the agent**, polling VM-ready up to 900s [:144] then `swap_backend` — must move orchestrator-side; arguably always belonged there) **(c)** · **:3216** `_handle_rewind` (lease-holding work item, otherwise DB-shaped) · **:3278** subscriber-queue overflow handling (transport) · **:3412** event-writer worker ((b), per-turn lifecycle) · **:3680** NATS notification mirror ((b/e)) · **:3857** canvas-awareness TTL expiry (presence, moves with transport) · **:4208/:5024** awaiting_user status flips (DB write, (a)) · **:4242** `_file_officer_wake` (durable orchestrator timer, (a)) · **:5397** turn-end cloud push (§6) · **:5406** `_notify_cloud_stage` protected-cloud staging ping (HTTP to orchestrator, (b) — could be orchestrator-triggered off the journal's turn.completed) · **:5449** `archive-llm-call` llm_requests audit write via to_thread ((d-lite): bounded, await before release) · **:7114** `_close_datasources_after_turn` (deferred close of replaced connections — dissolves under per-turn lifecycle).

persistent_graph.py: **:971** `memory_service.capture(turn_end)` and **:990** legacy `extract_and_store_memories` — fire-and-forget aux-LLM memory extraction **(d)**; `MemoryManager` already tracks these in `_bg_tasks` and exposes `drain_background(timeout)` (manager.py:63, :324-329, :329-356) — a ready-made lease-release hook. **:1654** on_thinking stream callback ((b)).

persistent_session.py: **:714** `_cloud_overlay_monitor_task` (§6).
cloud_mount/__init__.py: **:368** `_token_refresh_loop` (§6).
citation_engine/engine.py: **:824** `_schedule_verification` — background citation verification whose docstring *states the assumption stateless breaks*: "the agent's loop persists for its lifetime, so the verdict lands after cite_* returns" (:816-818). Verdict rows stay `pending` in the vector DB if the pod dies; `await_pending_verifications(timeout)` (:849) exists as a release-hook. **(d)**
memory/plugins/legacy_writers.py: interval state — see Surprises #4. **(c)**

## 5. The queued-input path, exactly

`POST /api/input` (:2815) → `_ensure_persistent_loop_started` (:488) → `_accept_user_input` (:2730): builds HumanMessage with `msg_{uuid}` id, **persists to thread_messages BEFORE the 200** (`_persist_one_message` with `turn_number = turn_count + 1`, 5s bounded, non-fatal :2774-2787), then `await _loop_user_queue.put({"content", "id", "role?"})` (:2791). The loop consumes (`_loop_get_user_input` :4164 — queue.get under idle-timeout/officer-backstop wait), reuses the id so its turn-start persist is an upsert (persistent_graph.py:854-932).

So: **content is durable at accept; the pending-vs-consumed semantic is memory-only.** On pod death, an accepted-but-unconsumed message becomes a transcript row with no turn — the next restore loads it as history (it *is* in context) but no turn ever runs *for* it; the user's message sits answered-by-nothing until they send another. The doc's line "queued input lives in agent memory today" should be sharpened to this. A turn-request row (the doc's proposal) is the fix; note the accept-time persist means the migration is small: add a `consumed_by_turn`/queue table keyed to the already-written message id. The cockpit's `pendingTurnCount/isAwaitingTurn` (fix C in the done doc) already gives queued input a visible state.

Also note `queue_depth` rides the /input response (:2851) — an API consumer signal to preserve.

## 6. Background daemons that keep EXTERNAL state alive (the hardest items)

1. **Turn-end cloud push** `_pending_cloud_push_task` (:249, spawned :5397, body `_run_turn_end_cloud_push` :5114). Contract: at most one pending; next turn's start hook awaits it **before its pull** (strict push(N)→pull(N+1) per mount, :5231); both teardown paths await before final `push_all` + `aclose` (:2383). Under stateless with the pod freed at turn end, this ordering has no enforcer: the next turn may claim on a different pod and pull mid-push. Options: (i) lease covers the push (pod busy until push lands — surrenders the just-shipped latency win only when the user replies faster than the push, same as today's semantics), (ii) a per-mount sync fence in DB (push generation counter the next claim must observe), (iii) push becomes a queued work item that the next turn's claim awaits. **(d/f)** — the one place where "run on the pod after lease release" (doc §Sessions) needs a cross-pod fence to stay correct.
2. **rclone bearer-token refresh loop** (`RcloneMountManager._token_refresh_loop`, cloud_mount/__init__.py:386-416): re-mints Keycloak bearers on a TTL schedule and pushes token files **to the workspace pod over SSH** (:423-437). The mount lives on the workspace; the keeper lives in the agent process. With no resident process between turns, tokens expire and the workspace-side mount 401s until someone refreshes. Needs a new home: workspace-side refresher daemon, orchestrator cron, or refresh-on-lease-claim + accept mid-idle rot. **(f)**
3. **Protected-cloud overlay ENOTCONN monitor** (`_cloud_overlay_monitor_loop`, persistent_session.py:742-779): periodic health probe + heal (restart the backing rclone mount) for the capture overlay on the workspace pod. Same shape as #2. **(f)** — same home options.
4. Memory extraction / citation verification / llm_requests audit — (d), with ready-made await hooks (`drain_background`, `await_pending_verifications`) so v1 can be "pod completes background work after releasing the session lease" exactly as the doc sketches; only the cloud push (item 1) additionally needs the ordering fence.

## 7. Event journal epoch/seq mechanics (answering the mission question directly)

**Epoch is DB-allocated, seq is in-memory.** Per runtime attach, `_resolve_event_journal_epoch` (:1476) runs `UPDATE threads SET events_epoch = events_epoch + 1 … RETURNING` — unconditional, atomic. `_next_seq` starts at 0 and is incremented **synchronously in `_broadcast`** (:3665-3667) so the (epoch, seq) cursor is stamped on the live WS frame and the queued journal write identically. One `_OrderedPersistentEventWriter` (:3350) per attach: bounded queue (10k), FIFO batches (100) via a single jsonb-array INSERT (:3364), 1 attempt for streaming frames, bounded retries for `canvas.*` state invalidations, terminal failures fan out `canvas.reconcile_required` without re-journaling (:3621). `thread_events` has a **UNIQUE index on (thread_id, epoch, seq)** (migrations/app/0004_thread_events.sql:60), so epoch allocation is a workable fencing token: a new claim's epoch bump makes a wedged old pod's writes land under a stale epoch that current-epoch SSE cursors never replay, and same-epoch collisions are constraint-rejected. Per-turn stateless v1: allocate an epoch at every lease claim (one UPDATE, same code path), seq resets to 0 in the executor — the cockpit already handles mid-stream epoch changes ("mid-stream epoch-change reconciliation path", :2038-2041). Cost: one extra threads UPDATE per turn + client re-anchor per turn (worth measuring; alternative is a seq range reservation per claim keeping one epoch per thread-generation).

## 8. Attach/resume rebuild map — the functions a per-turn load would reuse

Entry points: dedicated `lifespan` (:1056) → `_attach_session(thread_id)` (:1564); pool `POST /session/attach` (:2565) → same with `resolved_config`/`config_override`/`project_ids`/`datasources`/`config_name`. Steps, in order:
1. `get_thread_workspace` peek → tier detection `_session_backend_is_lite/vm` (:1611-1625). NB `get_thread_workspace` can be called **up to 3×** in one attach (:1614, :1691, :2143) — a per-turn load wants this collapsed into the turn-request payload or one cached fetch.
2. `_poll_workspace_ready` (:7470, timeout 120s; skipped lite) → workspace_override incl. SSH creds, git_remote_url, cloud cfg.
3. Datasource processing: `process_datasources`, MCP `connect_all`, `datasource_tool_categories`, `_apply_datasource_enrichment_to_resolved` (:1713-1774); `register_mcp_tools` (:1780).
4. **Config hydration — the doc's "per-request config resolution"**: `load_config_from_resolved(resolved_config)` (:1792) | `_load_expert_config(config_name)` (:1806) | deep-merge override + `_apply_settings_matrix` (:1820-1857); `create_llm` (:1815); aux-LLM rebuild with fallback (:1899-1949); embedding env override + singleton reset (:1951-1993); `build_knowledge_bindings` (:1995).
5. `PersistentSession.setup()` (:2018 → persistent_session.py:308): workspace connect, cloud mounts, shell manager, knowledge, user_id resolve, tools, bind, context manager, prompt, memory.
6. Epoch alloc + writer start (:2043-2070).
7. Repo clones onto workspace + datasources.md injection (:2099-2112); cloud-sync coordinator build + initial `pull_all` (:2160-2199).
8. **`_restore_session_messages` (:5824)** — the per-turn context load: `get_latest_compaction_checkpoint` (postgres_db.py:448, newest `role='summary'` row → `{summary, boundary_turn, boundary_seq}`) → Path A: `get_thread_messages_history(seq_gt=boundary_seq, limit=RESUME_MESSAGE_LIMIT(:135, default 1000), newest_first=True)` (postgres_db.py:352) → `[SystemMessage(summary)] + _db_rows_to_lc_messages(tail)` (:5745) → `repair_tool_pairing` (src/core/context, aliased :5722) → `_sanitize_restored_history` (:5725) → `ensure_within_limits(trigger="resume")` (may invoke aux-LLM summarization when over budget) → `strip_removal_markers`. Path B (no checkpoint): newest-N full load + fresh checkpoint write via `_record_compaction(trigger="resume")` (:5958-6038). Turn count restore :5930-5937/:6009-6010.
9. `_update_thread_status("active")` (:2227), loop primitives (:2232-2237), watchdogs (:2241), officer boot self-wake (:2250-2259).

Per-turn split: steps 4-5 + 8 are the load (cacheable by affinity: SSH, MCP connections, resolved config keyed by a config-version stamp, message tail keyed by last-seen seq); steps 1-3, 6-7 are claim-scoped; step 9 mostly dissolves.

## 9. Already stateless-compatible (verified, worth asserting in the doc)

- **Permission gates**: DB rows + trigger + LISTEN/NOTIFY on `thread_permission_updates` (:4411-4425); approvals converge from any path (WS, REST, future magic-link) on one UPDATE; wake-replay dedup via `_has_terminal_permission_decision` (:4472) even handles the restored-tool_call_id case. The code comment says it outright: "The agent never blocks on an in-memory queue anymore" for permissions (:4418). Residuals: the *waiter* parks the leased turn in 300s slices (a gated turn holds a pod — capacity note for the doc), and untethered-expiry reads `_subscribers` (§1).
- **Officer sessions**: park = queue.get with a durable orchestrator timer as primary wake (`_file_officer_wake` :3292) + local backstop; boot self-wake is an injected input (:2250). Under stateless an officer sleep is *no lease + a timer that enqueues a turn request* — strictly better fit than today's 24/7 parked pod.
- **IDE**: code-server runs **in the workspace pod**, proxied by the orchestrator (`orchestrator/services/ide_proxy.py:1-6`, profile store, nats_bridge seeding) — the agent pod holds no IDE state. The doc's attachment worry list can strike IDE from the *agent* inventory.
- **Browser**: executes on the workspace via `ToolContext.browser_exec` (context.py:1202, browser_workspace_executor); backend capability flag `supports_canvas_shared_browser` read from workspace metadata (persistent_session.py:518).
- **Shell**: tmux on the workspace pod, deterministic session name, agent-side stubs only (§2).
- **Canvas skill files**: workspace-persisted ownership manifest (§2).
- **Config updates**: persisted through the orchestrator (`update_thread_config` :6971) — a config change under stateless is just "next turn resolves fresh", deleting the fit-ladder/hot-swap/in-place-refresh machinery (:6744-7279).
- **Rewind/compact**: DB-shaped already; need only the lease.

## 10. dual_app note

The deployed shape is dual mode (`src/api/dual_app.py`), which wraps these handlers with pod-state pre-checks and adds worker-side in-memory guidance/reply inboxes fed by heartbeat responses (`_replace_inbox`/`get_pending_guidance`/`get_queued_replies`, dual_app.py:268-337) — those inboxes are the worker-side analog of queued input (heartbeat-pull, at-least-once, ack'd) and belong in the S3 worker inventory; `docs/issues/dual_app_persistent_app_redundancy.md` is the standing complaint that the two apps duplicate. Statelessness is an opportunity to collapse them into one turn-executor app.

## design_implications
- Fix the stale cross-reference: both issue docs the design cites now live in docs/done/ (session_turn_end_cloud_push_blocks_queued_input.md shipped 99b87008 and k3d-verified 08-07; the drain doc resolved 06-12). Cite them as shipped mitigations whose machinery statelessness then deletes, not as open bugs.
- Sharpen the queued-input claim: message CONTENT is already durable at accept time (_accept_user_input persists to thread_messages with turn_number=turn_count+1 before the 200 returns); only the pending-vs-consumed semantic is in-memory. The DB queue migration is therefore small — a consumption marker/turn-request row keyed to the already-persisted message id — and the cockpit's isAwaitingTurn already renders the queued state.
- Add a 'message fidelity' work item to S1: _serialize_message_row flattens list content (image blocks silently dropped), and the restore projection excludes thinking/additional_kwargs — acceptable for rare resumes, unacceptable when reload is the only context path. Store structured content JSON (or explicitly accept images-visible-one-turn) before per-turn reload ships.
- Add to S2's background-task inventory the two agent-side daemons that keep WORKSPACE-side mounts alive: the rclone bearer-token refresh loop (cloud_mount/__init__.py:386, pushes token files over SSH on a TTL schedule) and the protected-overlay ENOTCONN heal loop (persistent_session.py:742). These cannot be 'queued work' — they need a resident home (workspace-side refresher, orchestrator cron, or refresh-on-claim with accepted mid-idle rot).
- Specify the cloud-push ordering fence: the just-shipped background push contract (push N awaited before pull N+1 per mount, and before teardown) is enforced today by one process awaiting one task handle. Under any-pod-any-turn, either the lease must cover the push, or a per-mount sync-generation fence in the DB must gate the next turn's pull. Without this, S2 reintroduces the concurrent-walk corruption the 08-06 fix was designed around.
- Re-home the presence oracle: _subscribers (in-process WS registry) currently decides both permission-gate expiry (untethered => CAS-expire) and the awaiting_user/attention-sleep flip. Statelessness needs an orchestrator-side attached-clients signal (SSE consumer count) feeding both decisions, or their semantics silently change.
- Persist three small pieces of session state that are memory-only today and would reset EVERY turn: (1) runtime permission_mode/narration_mode changes (mode.set writes no DB row — already a latent revert-on-resume bug), (2) SessionTaskManager todos (pure in-memory list, src/managers/session_tasks.py), (3) memory-extraction interval cursor (PersistentIntervalExtractor._last_extraction_turn resets to 0 per process; per-turn pods would fire extraction EVERY turn once turn_count >= interval — a cost blowup, not the benign 'wider window' the writer docstring assumes).
- Replace file-undo with the git ledger: PersistentSession.file_checkpoints holds full original file contents in agent RAM across turns and cannot survive statelessness; per-turn auto-commit + record_turn_commit (turn->sha in DB) already provides the substrate the rewind code-restore path uses. Delete the RAM snapshots as part of S2.
- Decide the read-before-write gate policy: ToolContext._recent_reads/_recent_read_versions/_instruction_read_stamps span turns and authorize edits. Per-turn pods force re-reads (token cost + behavior change). Either scope the gate per-turn deliberately, or persist the stamps in thread metadata.
- Strike IDE from the session-attachment long tail: code-server runs in the WORKSPACE pod behind orchestrator ide_proxy.py; the agent pod holds no IDE state. Browser likewise executes workspace-side via browser_exec. Open question 3 reduces to canvas presence (which moves with the control transport under P6) — the 'likely the long tail' framing can be softened.
- Name the fencing token concretely: events_epoch is already an atomic DB counter bumped per attach (UPDATE threads ... RETURNING) and thread_events has a UNIQUE (thread_id, epoch, seq) index — allocate an epoch per lease claim and seq-from-0 in the executor; the cockpit already handles mid-stream epoch changes. Note the cost to measure: one threads UPDATE + one client re-anchor per turn.
- Use the ready-made drain hooks for (d)-class background work: MemoryManager.drain_background(timeout) and CitationEngine.await_pending_verifications(timeout) already exist — v1's 'pod finishes background work after releasing the session lease' can be implemented as awaiting these before the pod rejoins the pool, with only the cloud push needing the extra fence.
- Note the deleted-machinery dividend explicitly for sessions: per-turn config resolution makes the entire live-settings mutation ladder (model-swap fit ladder, resetup_datasources, refresh_context_limits, swap_backend, deferred datasource close) unnecessary — config changes become 'write override, next turn resolves fresh'. Likewise workspace/VM upgrade workflows (currently agent-driven WS handlers polling up to 900s) move orchestrator-side.
- Hard-interrupt routing needs the lease row to record the executing pod: graceful interrupts can be a polled DB flag (the graph already polls at 3 sites), but 'hard' must cancel a live LLM stream — route the interrupt POST to the lease-holder or NOTIFY into the executor.
- Add a heartbeat-replacement note: today's heartbeat carries aux-health/memory-health/RSS metrics that feed admin badges and degraded-flag persistence; the lease heartbeat (or a per-batch report at release) should carry the same payload or those surfaces go dark.
- Account for gate-parked turns in capacity math: a supervised permission gate parks the leased turn in 300s waiter slices — with one-turn-per-pod, N pending approvals pin N pods. Either release the lease at the gate (turn resumes as a new claim on resolution — the existing wake-replay dedup already supports re-announcing restored tool_call_ids) or size for it.
- Sweep permission rows on lease expiry: there is no expires_at sweeper — only an active waiter or turn-end retire CAS-expires announced rows. A pod death mid-gate strands pending rows that re-render as live approval cards forever; the lease-expiry path must run the _retire_announced_permission_rows equivalent.
- Collapse the attach fetch chatter before making it per-turn: _attach_session calls get_thread_workspace up to 3 times plus a separate lifecycle poll; a turn-request payload (or one cached claim-time fetch keyed by a config version) should deliver resolved_config, datasources, cloud cfg, and protected_cloud in one read.

## surprises
- Both cited issue docs moved to docs/done/ — the cloud-push fix SHIPPED (99b87008, k3d session-smoke passed 08-07) and drain-drain was resolved 06-12; the design doc cites them as open issues.
- Queued input content is ALREADY durable: _accept_user_input persists the message row (turn_number = turn_count+1) BEFORE returning 200; only the pending/consumed semantic lives in the in-memory asyncio.Queue. The doc's 'queued input lives in agent memory today' overstates — the gap is a consumption marker, not message durability.
- thread_messages round-trip is lossy by design: _serialize_message_row flattens list content (' '.join of text blocks — image payloads silently dropped), thinking is stored in its own column but never restored, and the HF-7 restore projection reads only role/content/tool_calls/tool_call_id. Per-turn reload would make uploaded images invisible to the model after their own turn — today this only bites on rare resumes.
- Runtime mode changes are memory-only: the WS mode.set / narration.set handlers assign _session.permission_mode/narration_mode and never write the DB — a latent revert-on-resume bug today that becomes a revert-EVERY-TURN bug under statelessness.
- Session todos (SessionTaskManager) are a pure in-memory list with no persistence anywhere — already lost on every pod recycle, contradicting an implicit assumption that session state is DB-backed.
- The memory-extraction interval cursor resets to 0 per process, and for the persistent (elapsed-gate) writer that means a fresh pod per turn fires extraction EVERY turn once turn_count >= interval — a cost blowup, not the benign 'window widens once' the writer docstring claims for occasional resumes.
- Two agent-side daemons keep WORKSPACE-side mounts alive: the rclone bearer-token refresh loop pushes fresh Keycloak tokens to the workspace over SSH on a TTL schedule, and the overlay ENOTCONN monitor heals the protected-cloud mount. The workspace's cloud access silently rots without a resident agent process — statelessness must re-home both (they cannot be queued work).
- IDE is already NOT agent-pod state: code-server runs in the workspace pod behind orchestrator ide_proxy.py; browser tools execute workspace-side via browser_exec. The doc's 'attachment homes... likely the long tail' worry list is really just canvas presence + the WS control transport, both of which P6 already moves.
- CitationEngine._schedule_verification's docstring states the exact assumption statelessness breaks: 'the agent's loop persists for its lifetime, so the verdict lands after cite_* returns'.
- The in-process _subscribers registry is a load-bearing presence oracle beyond transport: permission gates CAS-expire only when it is empty, and the awaiting_user/attention-sleep flip keys off it — statelessness changes approval and sleep semantics unless presence is re-homed.
- ToolContext._delivered_reply_keys is already deliberately process-local with documented at-least-once redelivery on a successor pod — an existing in-repo precedent for the crash-replay semantics the doc proposes.
- seq is allocated in-memory (module global incremented synchronously in _broadcast), NOT DB-side; only the epoch is a DB counter — but thread_events' UNIQUE (thread_id, epoch, seq) index plus per-attach epoch bump makes the epoch a real fencing token, confirming the doc's fencing claim with a concrete mechanism.
- Permission gates park the leased turn in 300-second waiter slices while a client is attached — under one-turn-per-pod, every pending human approval pins an entire pod.

## sources
- src/api/persistent_app.py:62-310 (module globals inventory)
- src/api/persistent_app.py:488-590 (_ensure_persistent_loop_started, callback wiring)
- src/api/persistent_app.py:593-731 (_handle_heartbeat_intents, _session_parked, _drain_suspend_session, _schedule_exit)
- src/api/persistent_app.py:739-855 (boot-WS + thread-status watchdogs)
- src/api/persistent_app.py:864-930 (_get_agent_metrics, aux/memory heartbeat health)
- src/api/persistent_app.py:1056-1233 (lifespan: registration, heartbeat, dedicated attach)
- src/api/persistent_app.py:1476-1510 (_resolve_event_journal_epoch — atomic DB epoch alloc)
- src/api/persistent_app.py:1564-2261 (_attach_session full rebuild path)
- src/api/persistent_app.py:2264-2465 (_terminate_session/_terminate_session_inner teardown ordering)
- src/api/persistent_app.py:2478-2715 (create_persistent_app; pool /session/attach + /session/detach)
- src/api/persistent_app.py:2730-2855 (_accept_user_input accept-time persist + handle_api_input)
- src/api/persistent_app.py:2928-3243 (handle_persistent_websocket: subscriber registration, welcome frame, receive loop, mode.set memory-only mutation at 3099/3131)
- src/api/persistent_app.py:3330-3598 (_QueuedPersistentEvent, _OrderedPersistentEventWriter)
- src/api/persistent_app.py:3653-3702 (_broadcast — in-memory seq allocation)
- src/api/persistent_app.py:3791-3877 (canvas awareness leases)
- src/api/persistent_app.py:4164-4292 (_loop_get_user_input: park, idle timeout, officer backstop, awaiting_user flip)
- src/api/persistent_app.py:4355-4408 (_loop_on_tool_start/result, _tool_inflight)
- src/api/persistent_app.py:4411-4530 (DB permission gates, LISTEN/NOTIFY, announced-rows bookkeeping, no-sweeper note)
- src/api/persistent_app.py:4718-4830 (_wait_for_permission_resolution — untethered expiry via _subscribers)
- src/api/persistent_app.py:5064-5250 (_resilient_cloud_sync, _run_turn_end_cloud_push, _await_pending_cloud_push, _retry_cloud_sync_start, _loop_on_turn_start push→pull ordering)
- src/api/persistent_app.py:5338-5407 (_loop_on_turn_complete: retire rows, save, title, background push spawn, cloud-stage ping)
- src/api/persistent_app.py:5452-5482 (_loop_persist_message incremental durability)
- src/api/persistent_app.py:5676-5706 (_loop_completion_handler)
- src/api/persistent_app.py:5745-5821 (_db_rows_to_lc_messages — lossy restore projection)
- src/api/persistent_app.py:5824-6041 (_restore_session_messages Path A/B)
- src/api/persistent_app.py:6132-6196 (_serialize_message_row — list-content flattening, thinking column)
- src/api/persistent_app.py:6267-6330 (_handle_rewind)
- src/api/persistent_app.py:6715-6741 (_close_datasources_after_turn)
- src/api/persistent_app.py:6879-7279 (_handle_config_update — orchestrator persistence at 6971)
- src/api/persistent_app.py:7383-7467 (_handle_idle_archive)
- src/api/persistent_app.py:8259-8313 (_early_title_from_prompt, _auto_title_after_first_turn, _draft_title_value)
- src/persistent_graph.py:762-1061 (run_persistent_loop: turn_count, _last_extraction_turn, input dict consumption, memory capture create_task at 971/990, per-turn git commit+push)
- src/api/persistent_session.py:198-301 (PersistentSession field inventory)
- src/api/persistent_session.py:308-415 (setup order)
- src/api/persistent_session.py:657-796 (_setup_cloud_mount, _cloud_overlay_monitor_loop, reset_cloud_overlay)
- src/api/persistent_session.py:917-990 (canvas skill manifest — workspace-persisted)
- src/api/persistent_session.py:1990-2030 (snapshot_file/undo_turn — in-RAM file checkpoints)
- src/api/persistent_session.py:2094-2129 (_setup_shell_manager)
- src/api/persistent_session.py:2322-2438 (swap_backend, cleanup)
- src/services/cloud_sync/base.py:96-139 (in-memory dedup state + _remote_seeded pod-boundary seeding)
- src/services/cloud_sync/coordinator.py:67-140 (WorkspaceSyncCoordinator)
- src/services/cloud_mount/__init__.py:356-437 (_token_refresh_loop + _push_token_sync over SSH)
- src/services/memory/manager.py:41-356 (MemoryManager, _bg_tasks, capture_nowait, drain_background)
- src/services/memory/plugins/legacy_writers.py:28-144 (in-memory interval state; PersistentIntervalExtractor elapsed gate)
- src/citation_engine/engine.py:795-855 (_schedule_verification loop-lifetime assumption, await_pending_verifications)
- src/tools/context.py:163-350 (ToolContext cross-turn state: _recent_reads, _cloud_anchors, _delivered_reply_keys at-least-once note, _pending_memories)
- src/tools/shell/shell_manager.py:320-395 (backend-authoritative tabs, deterministic tmux session name)
- src/database/postgres_db.py:352-529 (get_thread_messages_history seq_gt/newest_first cursors, get_latest_compaction_checkpoint, get_seq_for_message_id, get_live_message)
- src/database/postgres_db.py:635 (record_turn_commit)
- src/managers/session_tasks.py:42-57 (SessionTaskManager pure in-memory)
- src/agent.py:272-292,570-608,764-788 (UniversalAgent singleton state)
- src/api/dual_app.py:53-360 (dual-mode pod state, guidance/reply inboxes)
- src/api/orchestrator_client.py:307,343-402,1148-1209 (registration, heartbeat_interval, heartbeat loop)
- orchestrator/services/ide_proxy.py:1-6 (IDE lives in workspace pod)
- orchestrator/database/migrations/app/0004_thread_events.sql:48-60 (UNIQUE (thread_id, epoch, seq))
- docs/done/session_turn_end_cloud_push_blocks_queued_input.md (shipped fix, ordering contract, k3d verification)
- docs/done/session_agent_drift_drain_kills_idle_sessions.md (drain-suspend machinery statelessness deletes)
- docs/features/stateless_agents.md
- docs/go_rewrite.md
- config/session_base.yaml:167-230 (session memory pipeline: persistent_interval_extractor, no assembler)
