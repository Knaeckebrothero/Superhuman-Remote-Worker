# agent-5

# Research: per-turn cold-load cost + journal/epoch stress test for stateless agents

All line refs verified against the working tree 2026-08-07 (uncommitted changes included; `src/api/persistent_app.py` is modified in-tree).

---

## A. BOUNDING THE PER-TURN COLD-LOAD COST

### A1. What pool-mode attach actually does today (the machinery doc §"How much already exists" item 6 names)

The attach is two halves. **Orchestrator side** (`orchestrator/main.py:4470-4600`, `_send_session_attach`-shaped helper):

1. `get_thread` (1 DB read, :4499), `_thread_project_ids` + `_revalidate_thread_project_ids` + `_resolve_authorized_thread_datasources` + `_build_datasources_payload` (~3-6 DB reads, :4524-4535)
2. `_resolve_session_config` (:4553 → :2441-2560). Docstring: **"Sessions re-resolve on every (re)attach — there is no freeze."** Per call: `get_expert_by_id`, `_resolve_session_account_defaults`, `get_project_expert_link`, `_gather_in_scope_skills`, `_seed_registry_model_overrides` (the DB model-registry read — registry lookups are orchestrator-only; `src/core/model_registry.py:203-212` says the hooks stay `None` in the agent process), `_enforce_dispatch_grants`, `_thread_has_knowledge_scope`, `inject_blob_credentials` → **~8-12 DB roundtrips** + the pure-CPU `resolve_config` (`orchestrator/services/config_resolver.py:1-110`, "Pure + synchronous: no DB, no network"). YAML layer loads (`load_and_merge_config`, `loader.py:241-291`) `open()`+`yaml.safe_load` per layer, **no content cache** — only the model-config matrix (`loader.py:305-367`) and guardrails files (`:416-445`) are process-cached. Cost class: **tens of ms** (DB rt ~1-5ms in-cluster ×10, YAML parse a few ms).
3. POST `/session/attach` to the pod (30s timeout, :4583).

Delivery blob size: **`resolved_config` ≈ 127 kB/row** measured (`orchestrator/main.py:20127-20136` — the OOM postmortem number; also `docs/done/audit_metadata_config_duplication_ooms_orchestrator.md`).

**Agent side** (`src/api/persistent_app.py:1564-2261`, `_attach_session`), in order, with cost class:

| Step | Where | Cost class | Needed per stateless turn? |
|---|---|---|---|
| epoch/writer teardown guard | :1588-1602 | µs | n/a |
| `get_thread_workspace` peek (skipped when blob delivered) | :1612-1619 | 1 HTTP+DB | no (blob in request) |
| `_poll_workspace_ready` (non-lite) | :1631-1638 → :7470 (2s poll, 120s budget; VM budget larger) | 1 HTTP happy-path; **seconds-to-minutes if workspace suspended** | no — replace with turn-request-carried endpoint or cached |
| second `get_thread_workspace` (missing fields) | :1691-1707 | 1 HTTP+DB | no |
| `process_datasources` + **`mcp_manager.connect_all()`** | :1731-1743 | network, seconds-class for MCP servers | NO — must cache or defer |
| config hydration `load_config_from_resolved` | :1792 (`loader.py:5306`) | pure CPU, ms (parse 127 kB JSON → dataclasses) | yes |
| `create_llm` (+aux LLM rebuild, embedding env swap + singleton reset) | :1813-1993 | CPU ms; **mutates `os.environ` + module singleton** (`:1984-1988`) | yes (but process-global mutation blocks in-process multiplexing) |
| `PersistentSession(...)` + `.setup()` | :2003-2026 → `persistent_session.py:308-415` | see A5/A6 | partially |
| **epoch alloc (UNCONDITIONAL bump)** + writer start | :2036-2056 → :1476-1510 | 1 UPDATE…RETURNING | see B |
| repo datasource clones | :2102-2105 | git-over-SSH, seconds | no — idempotent-skip needed (clone fails warn-only if dir exists; `src/managers/git_manager.py:1081+` has no exists-probe) |
| cloud sync build + **blocking `pull_all()`** | :2160-2178 | WebDAV recursive tree walk, seconds | no — turn-boundary sync must move off the attach path |
| `_restore_session_messages` | :2224 → :5824-5949 | 2 DB reads + CPU (A3) | yes |
| `_update_thread_status("active")` | :2227 | 1 write | fold into lease claim |
| watchdog start, loop primitives | :2229-2241 | µs | replaced by lease |
| **officer boot wake injection** | :2250-2259 | 1 LLM-turn triggered! | NO — per-turn attach would inject a wake message *every turn* for officers |

Duplicate-attach guard: `/session/attach` 409s if `_session is not None` (:2584-2591) — the module-singleton design the doc's "de-globalization" bullet refers to.

**Detach is symmetric and also heavyweight** (`_terminate_session_inner`, :2314-2400): final memory capture (**an aux-LLM call**, :2355-2369), status write, `push_all`+`pull_all` cloud sync, git commit+push, cleanup. A naive per-turn attach/detach pairs seconds of work on both edges. The doc's "background tasks re-homed" bullet is the right answer; this is the inventory evidence.

### A2. Config resolution specifics — caches that exist and don't

- `deep_merge` = `copy.deepcopy` + recursion (`loader.py:184-221`) — CPU, sub-ms per layer at these sizes.
- Process caches: model-config matrix per path (`loader.py:305`), guardrails per path (`:416`). NOT cached: expert YAML leafs, prompt/instruction file contents (`read_text` per call, `:916-929`, `:1165-1185`) — warm page cache makes these ms-class anyway.
- DB config-override maps `_CONFIG_OVERRIDES`/`_VALUE_OVERRIDES` (`loader.py:60-116`): module-global, populated **once per job** by the worker (`src/agent.py:2176-2200` — 1 DB query `list_overrides_for_family`, first run only, skipped on resume). Comment: *"One job per agent process at a time, so module-level maps are safe"* — a stated invariant that stateless v1 (one turn per pod) preserves and in-process multiplexing breaks.
- Worker resume never re-resolves: it hydrates the frozen `jobs.resolved_config` (`src/agent.py:1936-1969`, freeze at :2209-2228). Job dispatch already ships the blob in `JobStartRequest` (`orchestrator/main.py:3938-3953`) — the exact delivery pattern a per-turn/batch request should reuse.
- Agent-side model-registry: **no DB read ever** (`src/core/model_registry.py:203-212`); family lookup is a sync prefix heuristic (`:252-330`). The catalog read is folded into the orchestrator resolve (`_seed_registry_model_overrides`, main.py:2498).

**Conclusion (config):** per-turn config cost = 0 agent-side DB reads if the blob rides the turn request (~127 kB JSON parse + dataclass hydrate + `create_llm`, single-digit ms) or ~8-12 orchestrator DB reads + tens of ms if re-resolved per turn. A config cache keyed `(thread_id, config_version/updated_at)` makes re-resolve an edge event. This is the answer to doc Q6: the generic deployment + config cache is cheap; per-expert deployments buy nothing on resolution cost.

### A3. thread_messages tail load

Path: `_restore_session_messages` (`persistent_app.py:5824-5949`). Path A (checkpoint): 1 `fetchrow` for newest `role='summary'` row (`postgres_db.py:448-489`) + 1 `fetch` of the tail `seq > boundary_seq` capped at `RESUME_MESSAGE_LIMIT` (**default 1000**, `persistent_app.py:135`), `newest_first` floor semantics (`postgres_db.py:352-446`). Index-backed: `idx_thread_messages_thread_seq_live (thread_id, seq) WHERE rewound_at IS NULL` (`schema_current.sql:9758-9761`, migration `0111`). Projection is the **HF-7 thin diet** — only `role, content, tool_calls, tool_call_id, turn_number` (`postgres_db.py:389-403`); the fat JSONB columns (reasoning, provider_raw, …) are excluded.

Row sizes, measured on k3d (`srw-postgres-0`, live query): thread_messages 1069 rows, **avg 1004 B/row full-row (max 32 kB)**; the thin projection is less. The OOM incident that motivated Path A was a **793-message / 395 k-token** thread (`persistent_app.py:5836-5839`) ≈ 2 kB/row of content. Typical post-compaction tail is tens-to-low-hundreds of rows → **~50 kB-1 MB wire, 2 DB roundtrips, 5-50 ms**. Then `ensure_within_limits` re-bounds (token counting; CPU, tens-to-hundreds of ms on large tails) + `_repair_tool_pairing`/sanitize (CPU).

Writes are already stateless-safe: `thread_messages.seq` is **DB-side BIGSERIAL** (migration `0023_thread_messages_seq.sql:36`; default `nextval('thread_messages_seq_seq')`, `schema_current.sql:7731`), upsert-by-id with `RETURNING id, seq` (`postgres_db.py:682-785`), mid-turn incremental persists + one batched turn-end reconcile (`:787-806`). Input is persisted **before** the 200 (`_accept_user_input`, `persistent_app.py:2730+`); only the *turn-triggering* queue (`_loop_user_queue`, asyncio.Queue, :2234) is process memory — confirming the doc's "queued input into the DB" item is a trigger-row problem, not a data-durability problem.

### A4. Worker checkpoint load

- Construction per job: `psycopg AsyncConnection.connect` + `AsyncPostgresSaver` (`src/agent.py:1258-1301`); `setup()` gated once per process (`:1294-1301`). Connect ≈ 10-30 ms in-cluster.
- Resume: `graph.aget_state` (`:3963/:3969/:3981` in `_resume_from_checkpoint`, up to 3 thread-id formats probed = up to 3 queries on miss, 1 on hit).
- **Latest-per-channel is exact**: the library's `SELECT_SQL` (`langgraph/checkpoint/postgres/base.py:87-111`, verified in an installed copy) joins `checkpoint_blobs` on the checkpoint row's `channel_versions` (channel, version) pairs — one SELECT + one pending-sends SELECT.
- Volume anecdote (the 447 MB story — it is in the **audit-OOM doc**, not the D3 doc): thread `19707fa1` left **4,915 `checkpoint_blobs` rows / 447 MB**, `messages` channel = 2,451 version-snapshots, max single blob 551 kB, but **restore loads only latest-per-channel ≈ 600 kB** (`docs/done/audit_metadata_config_duplication_ooms_orchestrator.md:61`). D3 doc probe: PG checkpoint **writes ~11 ms p50 / 28 ms p95, flat with state growth**, ~3 writes/superstep ≈ 30 ms/node (`docs/done/cross_pod_resume_cold_starts_checkpoint_not_replicated.md:207-227`).
- Retention: delete-on-terminal + **keep-last-3 per thread sweeper every 600 s** (`orchestrator/services/checkpoint_retention.py`, `CHECKPOINT_RETENTION_KEEP=3`) — added after a 16 Gi checkpoint PVC refilled to ~15 GB in 4 days from long-running jobs. Batch-boundary freezes add no new writes (checkpoints already happen per superstep); the storage story is already managed.
- Per-batch cold extras: graph **rebuilt per job** (`build_phase_alternation_graph`, `src/agent.py:964-977` — CPU ~10-50 ms), `_setup_job_tools` per job (:900), workspace SSH connect, config-overrides fetch first-run-only (:2176).

### A5. SSH / workspace connect

`RemoteBackend.connect` (`src/core/backends/remote.py:371-513`): key validation + paramiko `SSHClient.connect` (TCP + KEX + pubkey auth) + `open_sftp()` + keepalive tuning. In-cluster ≈ **100-500 ms cold**; the doc's "~0.3-0.5 s" is consistent. Reuse: every op runs `_ensure_connected()` (`:554-583`) — no-op when the transport is alive, transparent reconnect + **re-seed hook** otherwise. Retry budgets: `connect()` owns them (max_retries=5, classified failure buckets); session `_setup_workspace` wraps in a 300 s budget (`persistent_session.py:481-507`).

**Hazard the doc must address:** `_init_shell` (`remote.py:1129-1158`) begins with `tmux kill-session` then `new-session` — and `disconnect()` also kills the tmux session (`:516-523`). The `_shell_initialized` flag is per-backend-object. So a **fresh backend per turn/batch wipes tmux state (scrollback, running processes, tab CWDs) on its first shell op** — directly contradicting the doc's premise 1 ("Shell state survives in tmux on the workspace pod") as applied to stateless pods. Needs a reattach-if-exists path (`tmux has-session` → adopt) plus tab-state re-hydration (or accept shell state as batch-scoped). Note tab bookkeeping (`self._tabs` OrderedDict, `:279`) is also in-process — `get_shell_state`-style pane reads exist, but tab metadata would need reconstruction from `tmux list-windows`.

### A6. Tool binding + prompt assembly

`PersistentSession.setup` steps 3-8 (`persistent_session.py:365-405`): shell manager, knowledge bindings, `_setup_tools` (registry filter + tool construction), `_bind_tools` (`:1972-1987` — guardrails wrap + `llm.bind_tools`, optional `parallel_tool_calls`), context manager, `get_phase_system_prompt` (`loader.py:4258`, file reads + render). All pure CPU/filesystem: **~20-100 ms** for ~35-45 tools. `register_mcp_tools` swaps process-global dynamic registry entries per attach (`persistent_app.py:1778-1780`) — another one-session-per-process invariant. Memory/KB init (`_setup_memory`, `_setup_knowledge`) reuses existing pools; embedding-service is a process singleton rebuilt via env mutation (A1).

### A7. Cold vs warm bound, and what soft affinity buys

**Minimal stateless SESSION turn (S2, decomposed — not attach-as-built):** lease claim 1 rt + config blob in-request (0 rt, ms hydrate) + LLM/tools/prompt CPU (~20-100 ms) + message tail 2 rt (~5-50 ms + token-count CPU) + SSH connect (~0.1-0.5 s) + journal writer init (1 rt) ≈ **~0.3-1 s cold**, ~5 DB roundtrips, ~0.2-1.5 MB read. Against a 5-30 s LLM turn: 3-15 % of a *short* turn — noticeable but acceptable; near-zero on a warm hit. **S1 (lite)** drops SSH and workspace polling entirely → cold ≈ 50-200 ms; the "<2 s create-to-first-token" acceptance is realistic *provided the epoch cascade (B3) is fixed*, which otherwise adds ~2-4 s of client-side reload per turn — the single biggest threat to the S1 latency goal.

**Attach-as-built is NOT a viable per-turn unit**: MCP `connect_all`, repo clone probes, blocking recursive cloud `pull_all`, workspace-ready polling, officer wakes, memory-capture/push on detach make it seconds-class on both edges. The design work is decomposition (per-turn core vs cached/edge-triggered vs re-homed background), not speeding up attach.

**Worker batch cold:** checkpointer connect (~10-30 ms) + `aget` (~600 kB worst, 10-50 ms) + graph+tools rebuild (~0.1-0.3 s) + SSH (~0.1-0.5 s) + blob hydrate (ms) ≈ **~0.5-1.5 s/batch**. At ≥25 supersteps × (LLM call + ~30 ms checkpoint writes) the overhead is **<1-2 % wall** — the doc's "<5 %" claim is safe with margin, *if* tmux reattach lands (else add a phase-0-style shell warmup per batch).

**Soft affinity buys, in order of value:** (1) live SSH transport + tmux continuity — avoids both the 0.1-0.5 s handshake and the kill-session wipe hazard; (2) resident message context — skips tail load + `ensure_within_limits` recount; (3) built LLM/tools/graph objects (~0.1-0.3 s CPU); (4) config already hydrated. Provider prompt caching does NOT need affinity (content-keyed; vLLM prefix cache is per-endpoint — doc Q5 already right). Realistic value ≈ **0.3-1 s/turn + hazard avoidance**; correctness must never depend on it (all items reconstructible).

---

## B. JOURNAL-ONLY STREAMING & EPOCHS, STRESS-TESTED

### B1. Schema, writer, and where seq comes from

- Table: `thread_events(id identity, thread_id, epoch int, seq bigint, kind text, payload jsonb, created_at)` with **UNIQUE (thread_id, epoch, seq)** (`schema_current.sql:6914-6922, 9747`) + (thread_id, created_at) for the pruner. Measured on k3d: avg **168 B/row** (max 1.27 kB), 3,062 rows.
- Writer: `_OrderedPersistentEventWriter` (`persistent_app.py:3350-3597`) — one asyncio task per attached runtime, bounded queue (10,000, batch 100; `:259-269`), single batched INSERT via `jsonb_array_elements ... ORDER BY ordinal` (`:3364-3375`), monotonic-cursor rejection in `enqueue` (`:3424-3439`), best-effort for stream frames / bounded retries for canvas-state kinds, overflow drops with `canvas.reconcile_required` fan-out.
- **Seq allocation is IN-PROCESS, NOT DB-side**: `_broadcast` does `global _next_seq; _next_seq += 1` synchronously (`persistent_app.py:3653-3672`; global declared :256, reset to 0 per attach :2044 and in teardown). Cross-pod safety today comes **only** from each attach getting a fresh epoch namespace; two writers inside one epoch would collide (the unique index would fail their batches → frames dropped via `_notify_terminal_failure("write_failed")`, not corrupted). Contrast: `thread_messages.seq` is a global **BIGSERIAL** — the sibling journal already demonstrates the cross-pod-safe allocation pattern in the same codebase.

### B2. The attach-time epoch rule — the mission's premise is outdated

`_resolve_event_journal_epoch` (`persistent_app.py:1476-1510`) is **unconditional**: `UPDATE threads SET events_epoch = events_epoch + 1 ... RETURNING`, on *every* runtime attach — the docstring says allocation is unconditional even when the current epoch has no rows (a pruned-empty epoch could strand cached cursors ahead of new seqs). Schema comment agrees: *"The agent allocates a new epoch on every DB-backed runtime attach"* (`schema_current.sql:7282`). It has been unconditional since the feature landed (commit `37a16928`, 2026-05-12). The rewind verb also re-epochs (`persistent_app.py:6482`, commit `20c01951`). **So: under fresh-attach-per-turn, `events_epoch` bumps on EVERY turn.** Today's live ratio for comparison (k3d): a 20-turn thread had 3 epochs; per-turn attach makes it ≥21.

### B3. What per-turn epoch bumps do to the SSE loop and the cockpit — the full cascade

Server (`orchestrator/main.py:30529-30778`): epoch read once at open (:30543); poll `WHERE epoch=$2 AND seq>$3` at 200 ms ×5 → 1 s backoff (:30744-30746); the shipped P1 zombie guard re-reads `events_epoch` only after **≥2.0 s accumulated idle** (`THREAD_EVENTS_EPOCH_RECHECK_S`, :30524) and then emits `gone_beyond_horizon {reason: epoch_bumped_mid_stream}` anchored at `_no_cursor_replay_start(new_epoch)` (:30673-30721) **and terminates the generator**.

Sequence per turn under naive stateless: user sends → pod attaches, bumps N→N+1 → all turn frames journal under N+1 → client's open SSE polls dead epoch N → empty polls accumulate 5×200 ms + 1×1 s = **~2.0-2.2 s before detection** → horizon frame → client `_handleGoneBeyondHorizon` (`persistent-chat.service.ts:1628-1680`): close SSE → **`clearThreadMessages` (wipes the whole IndexedDB thread cache) + delete cursor** (:1653-1654) → `loadHistory` which, with the cache just cleared, is a **FULL transcript refetch** (:1259-1309 — the `?after=` delta optimization is defeated) → re-dispatch outbox bubbles → set cursor to (N+1, anchor) → `loadThreadMeta` (another GET) → `_openSse` (reopen). The comment at :1648 — "Epoch bumps are rare … a full refetch here is cheap" — becomes false by design.

**Net per-turn tax: ~2-2.2 s added latency before any token renders + full-history REST download + IndexedDB rebuild + 2 extra GETs + SSE generator churn (one terminated generator per turn per client), every single turn.** Frames aren't lost (`_no_cursor_replay_start` returns 0 while the new epoch has no `turn.completed`/`turn.error`, :30501-30514, so the in-flight turn replays fully), but tokens burst-render after the reload. With P5's idle-close the 2.2 s detection disappears (stream reopens on `thread.activity`) but the reopen hits the **at-open `epoch_mismatch` horizon** (:30582-30601) instead → the same cache-clear + full-reload dance, minus the delay. **Epoch stability is required regardless of P5.**

Also affected: `resumedFromEpoch` stale-lifecycle suppression (`service.ts:1694-1728`) and the epoch-keyed duplicate-render fixes assume epochs mark *runtime generations*; per-turn epochs semantically overload them.

### B4. Fencing reality check

The doc calls the epoch "a fencing token that can serve the lease". Half true: the epoch **routes readers** (clients follow the max epoch via re-read) but **never fences writers** — `_write_batch` (`:3568-3583`) inserts rows stamped with the writer's cached epoch and never re-checks `threads.events_epoch`; a stale writer keeps succeeding forever into a dead epoch (the P1 doc's "live old-epoch writer" residual). The unique index only stops same-epoch collisions. A real lease needs the batch INSERT guarded by the claimed generation (e.g. CTE `WHERE threads.events_epoch = $claimed` → 0 rows inserted = lost lease, surface loudly).

### B5. Retention

`thread_events_prune_sweeper` (`orchestrator/main.py:30937-30986`): every 300 s, delete ended-thread rows >24 h and non-ended rows >7 days. Dead per-turn epochs would accumulate rows only within those windows — storage is a non-issue (168 B/row avg); the cost of per-turn epochs is entirely the client cascade, not the table.

### B6. Concrete proposal: stable epochs across stateless pods (within existing code shapes)

1. **Epoch = writer-generation, bumped only on writer-identity change** (lease steal, crash takeover, rewind, genuine session resume) — not per attach. Implementation: fold epoch allocation into the lease claim: `UPDATE threads SET lease_owner=$pod, lease_expires=..., events_epoch = events_epoch + CASE WHEN lease_owner IS DISTINCT FROM $pod THEN 1 ELSE 0 END ... RETURNING events_epoch` — with soft affinity the common case (same pod re-claims) bumps nothing; a steal bumps exactly once and the *existing* server guard + client handler (built and shipped for exactly this) handle it as today's rare event. Caveat: `_resolve_event_journal_epoch`'s stated reason for unconditional bumps (pruned-empty epoch strands a high cached cursor, :1479-1483) dies automatically once seq is DB-side and never resets (next point).
2. **Move seq allocation DB-side, mirroring `thread_messages`**: simplest — a global sequence default on `thread_events.seq` (exactly like `thread_messages_seq_seq`), writer inserts without stamping seq and lets the DB assign; ordering per thread is preserved because the lease serializes writers and the single writer task serializes batches. Alternative with fewer semantics changes: writer-side **block allocation** — one `UPDATE threads SET events_seq = events_seq + $batch_len RETURNING events_seq` per batch (1 extra rt per ≤100 frames). Two consumers to adjust: (a) `_broadcast`'s synchronous `_seq` stamp on live frames (:3672) — only WS subscribers consume it; under journal-only streaming (P6 retires the WS) it can carry a provisional writer ordinal or be dropped; the SSE `id:` line is built from the DB row (`main.py:30740`), unaffected. (b) The retention check `cursor_seq < min_seq - 1` (:30614) assumes near-contiguity — gapped global seqs still behave (a stale cursor below the pruned floor still trips it), but state the tolerance explicitly.
3. **Fence the writer with its claimed generation** (B4): batch INSERT via CTE checking `threads.events_epoch = $claimed AND lease_owner = $pod`; on 0-rows-inserted, the writer terminal-fails the batch and the pod abandons the turn — turning the epoch into an actual fencing token, which is the doc's lease §"Sessions already have an epoch mechanism" made true.
4. **Do NOT take the client-only shortcut** (teaching the cockpit that turn-attach bumps don't need a reload): it leaves per-turn generator churn, overloads `resumedFromEpoch` semantics, and abandons the append-only cache invariant that `_handleGoneBeyondHorizon`'s cache-clear protects.

### B7. Worker jobs today: there is NO journal

`thread_events` is sessions-only (FK → threads). Verified table inventory (k3d): no job_events/progress table. What exists instead: jobs-row status polling via REST (`get_job_progress` derives ETA from the jobs row alone, `orchestrator/database/postgres.py:4737+`); replica-local, no-replay notification feed frames (`session_reliability...md` transport table; `notification.service.ts` handles `new_message`/loop frames); live pod-log endpoints + **S3 job-log archive at pod deletion** (`orchestrator/main.py:32475-32588`, keyed by job — the doc's "logs keyed by thread/job id" observation is already the archive's shape); `agent_audit`/`llm_requests` in the separate audit store; `message_log` for send_message_to_job; ProgressCommitter = git commits, not events (`src/core/progress_commit.py`). **Implication: "journal-only streaming" for workers means *building* the journal (or accepting REST-poll UX), not converging on an existing one.** Cheapest convergence: give job runs a `thread_events`-shaped journal keyed by job_id with the same epoch/seq/cursor contract, so the P5 head-endpoint + trigger machinery serves both; the batch driver's writer is the same class.

### B8. P5/P6 convergence (the endpoint this must converge with)

`docs/features/session_reliability_and_transport_simplification.md`: P1-P4 shipped; **P5/P6 not started** (status table :3-15). P5 adds `thread.activity` via **DB trigger on `thread_events` INSERT (kind='turn.started') → pg_notify** — trigger-based *because* the agent writes the journal directly and replicas are 2; this is inherently stateless-pod-compatible (fires whichever pod inserts). P5's `GET /events/head` `{epoch, seq}` is the revalidate probe a stateless client wants. P6 retires the per-session control WS → control verbs become REST + journaled 202 frames — removing the last transport that assumes a resident session pod (welcome-frame `session.state`/`running_tool` substitute is 6c). Stateless timing point: **land the epoch-stability change before or with P5** — P5's idle-close converts the 2.2 s mid-stream detection into an at-open `epoch_mismatch`, which *feels* better but still forces the full reload per turn; sequencing the epoch fix first makes P5's reopen a clean cursor resume.

---

## Key numbers table (for the doc)

| Item | Value | Basis |
|---|---|---|
| resolved_config blob | ~127 kB | main.py:20130 (measured, prod incident) |
| Orchestrator session re-resolve | ~8-12 DB rt + tens ms CPU | main.py:2441-2560 trace |
| Message tail | 2 rt; ≤1000 rows; ~1 kB/row full (k3d measured), thin projection less | postgres_db.py:352-446; k3d psql |
| Checkpoint restore | 1 SELECT latest-per-channel; ~600 kB worst observed | langgraph SELECT_SQL; audit doc :61 |
| Checkpoint write | 11 ms p50 / 28 ms p95, ~3/superstep | D3 probe |
| SSH cold connect | ~0.1-0.5 s | remote.py:371-513 (estimate; doc's 0.3-0.5 s consistent) |
| Epoch-bump client cascade | ~2.0-2.2 s detect + full-history refetch + cache wipe + 2 GETs + SSE reopen | main.py:30524/30744-30746; service.ts:1628-1680 |
| thread_events row | ~168 B avg | k3d measured |
| Minimal cold session turn (S2) | ~0.3-1 s, ~5 DB rt | composed |
| Minimal cold worker batch | ~0.5-1.5 s → <2 % at ≥25 supersteps | composed |
| Agent pod memory | request 512Mi / limit 2Gi | helm/values.yaml:400-405 |

## design_implications
- Correct the doc's epoch premise: events_epoch bumps UNCONDITIONALLY on every runtime attach (persistent_app.py:1476-1510, schema comment schema_current.sql:7282), so naive per-turn attach = per-turn epoch bump = ~2-2.2s added latency + full-transcript refetch + IndexedDB wipe + SSE reopen on EVERY turn (service.ts:1628-1680). Add an explicit 'epoch stability' work item to §What's missing/Sessions.
- Specify the epoch/seq redesign concretely: (a) bump events_epoch only on writer-identity change, folded into the lease claim UPDATE (same-pod re-claim bumps nothing — soft affinity makes bumps rare again); (b) move thread_events.seq to DB-side allocation mirroring thread_messages' BIGSERIAL (or per-batch block allocation on the threads row); (c) fence writer batches with WHERE threads.events_epoch=$claimed so a stale writer fails loudly — today a stale writer is never fenced (writer never re-checks the DB epoch).
- State that _broadcast's synchronous in-process _next_seq (persistent_app.py:256, 3665-3667) is the reason seq can't survive pod hops today, and that the synchronous `_seq` stamp on live frames only matters for the WS path P6 retires — journal-only streaming removes the constraint that forced in-process allocation.
- Decompose 'attach' explicitly in the doc: the per-turn core (lease claim, config-blob hydrate, message tail, SSH ensure, journal writer) is ~0.3-1s cold; everything else in _attach_session (MCP connect_all, repo clone, blocking cloud pull_all, workspace-ready polling, officer boot wake at :2250-2259, watchdogs, status writes) and in _terminate_session (final memory capture via aux LLM, cloud push+pull, git push) must be cached, edge-triggered (first-attach-of-lease / lease-release), or queued work — list them as the background-task inventory the doc already calls for.
- Add the tmux hazard to §S2/workspace sessions: RemoteBackend._init_shell does tmux kill-session+new-session (remote.py:1129-1158) and disconnect() kills the session (:516-523) — a fresh backend per turn wipes shell state (running processes, scrollback, CWDs), contradicting premise 1. Required: reattach-if-exists (tmux has-session) + tab-state rehydration from tmux list-windows, or declare shell state batch-scoped.
- For workers, correct §5's implied generality: thread_events is sessions-only — jobs have NO event journal (visibility = REST polling + replica-local notification feed + pod logs/S3 archive). Decide in the doc: either extend the thread_events epoch/seq/cursor contract to job runs (same writer class, same P5 head/trigger machinery) or explicitly accept poll-based job UX for S3.
- Tighten the doc's cold-cost claim with the measured decomposition: config blob ~127kB (0 agent DB reads if delivered in the turn/batch request — job dispatch already ships it, main.py:3938-3953); message tail 2 roundtrips ≤1000 rows (~1kB/row measured); checkpoint restore ~600kB worst via latest-per-channel SELECT; SSH 0.1-0.5s; worker batch overhead <2% at ≥25 supersteps (safer than the doc's <5%).
- Answer Q6 with evidence: config re-resolution is already per-attach with no freeze (main.py:2452-2453), costs ~8-12 DB roundtrips + tens of ms, and the agent never reads the model registry (model_registry.py:203-212) — so one generic deployment + a (thread_id, config_version)-keyed resolve cache is cheap and per-expert deployments buy no resolution cost.
- Note the process-global residue that pins v1 to one-turn-per-pod: loader override maps ('one job per process' comment, loader.py:60-67), embedding-service os.environ mutation + singleton reset (persistent_app.py:1951-1993), register_mcp_tools registry swap (:1778-1780), module singletons _agent/_session — list them as the de-globalization inventory for the deferred multiplexing step.
- Sequence against P5/P6: land epoch stability BEFORE P5 — P5's idle-close only converts the 2.2s mid-stream horizon into an at-open epoch_mismatch that still forces cache-clear + full reload per turn; P5's thread.activity DB trigger and /events/head are already stateless-pod-compatible and should be cited as the stateless client transport.
- Add officer sessions as a special case: the attach path injects a boot wake message per attach (persistent_app.py:2250-2259) — per-turn attach would self-wake officers every turn; the wake must key on lease-generation change, not attach.
- Queued-input migration is smaller than the doc implies: input rows are already persisted before the 200 (_accept_user_input, persistent_app.py:2730+); only the turn-trigger (asyncio.Queue) is memory — the DB work item is a turn-request/trigger row + drain, not message durability.

## surprises
- The mission's stated epoch rule ('re-attach bumps iff the current epoch already has events') does not exist in the code and never did — the bump has been UNCONDITIONAL per attach since the feature landed 2026-05-12 (persistent_app.py:1476-1510, docstring says so explicitly; schema comment agrees). This makes the per-turn-attach epoch problem strictly worse than the doc review assumed.
- The two journals disagree on seq allocation: thread_messages.seq is DB-side BIGSERIAL (cross-pod-safe by construction) while thread_events.seq is an in-process counter reset per attach — the codebase already contains the correct pattern for the fix, one table over.
- A stale journal writer is never fenced: the writer stamps its cached epoch and never re-checks threads.events_epoch, so a pod that lost the session keeps successfully inserting into a dead epoch forever. The doc's 'epoch as fencing token' claim is only true for readers today.
- The epoch-bump client cascade wipes the entire IndexedDB thread cache (clearThreadMessages) before reloading, converting loadHistory's cheap ?after= delta fetch into a FULL transcript download — the code comment 'epoch bumps are rare... a full refetch here is cheap' is an explicit assumption per-turn attach would break.
- RemoteBackend._init_shell begins with tmux kill-session (and disconnect() also kills it) — 'shell state survives in tmux on the workspace pod' is only true because today's resident pod never re-inits; a fresh stateless backend per turn would wipe running processes/scrollback/CWDs on its first shell op.
- Worker jobs have NO event journal at all — thread_events is sessions-only (FK to threads); job visibility is REST polling + a replica-local best-effort notification feed + pod logs/S3 archive. 'Journal-only streaming' for workers is a build, not a convergence.
- Officer sessions get a synthetic boot-wake message injected on every attach (persistent_app.py:2250-2259) — per-turn attach would make officers wake themselves every turn.
- Session config is already re-resolved on EVERY attach with no freeze (main.py:2452-2453) — per-turn resolution is not a new cost class, just a higher frequency of an existing ~8-12-roundtrip path; and the agent process never reads the model registry from DB at all.
- P5's idle-close does NOT neutralize per-turn epoch bumps — it converts the ~2.2s mid-stream detection into an immediate at-open epoch_mismatch that still forces the full cache-wipe reload every turn; epoch stability must land regardless of P5.
- The 447MB checkpoint anecdote lives in docs/done/audit_metadata_config_duplication_ooms_orchestrator.md (not the D3 doc): 4,915 blobs/447MB stored but restore reads only latest-per-channel ≈600kB — and a keep-last-3 sweeper (added after a 15GB-in-4-days PVC incident) now bounds live threads.
- Queued input is already persisted to thread_messages BEFORE the 200 returns — only the turn-trigger queue is process memory, so the doc's 'queued input into the DB' item is smaller than it reads (a trigger-row problem, not a durability problem).

## sources
- docs/features/stateless_agents.md
- docs/go_rewrite.md
- docs/done/cross_pod_resume_cold_starts_checkpoint_not_replicated.md:207-345
- docs/done/audit_metadata_config_duplication_ooms_orchestrator.md:61,117-121 (447MB / 600kB restore)
- docs/features/session_reliability_and_transport_simplification.md:1-100,572-804 (P5/P6)
- src/api/persistent_app.py:135 (RESUME_MESSAGE_LIMIT=1000)
- src/api/persistent_app.py:255-269 (in-process epoch/seq globals + writer constants)
- src/api/persistent_app.py:1476-1510 (_resolve_event_journal_epoch — unconditional bump)
- src/api/persistent_app.py:1564-2261 (_attach_session full trace)
- src/api/persistent_app.py:2036-2070 (epoch alloc + writer start per attach)
- src/api/persistent_app.py:2250-2259 (officer boot wake per attach)
- src/api/persistent_app.py:2264-2400 (_terminate_session: memory capture, cloud push, git push)
- src/api/persistent_app.py:2565-2625 (/session/attach, 409 single-session guard)
- src/api/persistent_app.py:2730+ (_accept_user_input persist-before-200)
- src/api/persistent_app.py:3350-3597 (_OrderedPersistentEventWriter, batch INSERT :3364-3375, no epoch re-check on write)
- src/api/persistent_app.py:3653-3702 (_broadcast, in-process _next_seq)
- src/api/persistent_app.py:5824-5949 (_restore_session_messages Path A/B)
- src/api/persistent_app.py:7470+ (_poll_workspace_ready 2s/120s)
- src/database/postgres_db.py:352-446 (get_thread_messages_history thin projection)
- src/database/postgres_db.py:448-489 (get_latest_compaction_checkpoint)
- src/database/postgres_db.py:682-806 (save_thread_message upsert, BIGSERIAL seq RETURNING)
- orchestrator/database/migrations/app/0023_thread_messages_seq.sql:36 (BIGSERIAL)
- orchestrator/database/schema_current.sql:6914-6922,7270-7282,7731,9747-9761 (thread_events schema, epoch comment, seq default, indexes)
- orchestrator/main.py:2441-2560 (_resolve_session_config — re-resolve every attach, DB roundtrips)
- orchestrator/main.py:3938-3953 (JobStartRequest ships resolved_config)
- orchestrator/main.py:4470-4600 (session attach dispatch trace)
- orchestrator/main.py:20127-20136 (resolved_config ~127kB/row)
- orchestrator/main.py:30489-30526 (_no_cursor_replay_start, THREAD_EVENTS_EPOCH_RECHECK_S=2.0)
- orchestrator/main.py:30529-30778 (thread_event_stream: at-open epoch_mismatch :30582-30601, retention check :30614, mid-stream bump :30673-30721, 200ms-1s poll :30744-30746)
- orchestrator/main.py:30937-30986 (thread_events prune: 24h ended / 7d active)
- orchestrator/main.py:32475-32588 (job log endpoints + S3 archive)
- orchestrator/services/config_resolver.py:1-110 (pure resolve, layer order)
- orchestrator/services/checkpoint_retention.py:1-60 (keep-last-3, 15GB-in-4-days incident)
- orchestrator/database/postgres.py:4737+ (get_job_progress = jobs-row derivation, no journal)
- src/core/loader.py:60-133 (process-global override maps, 'one job per process')
- src/core/loader.py:184-221 (deep_merge), :241-291 (load_and_merge_config, no file cache), :305,416 (matrix/guardrails caches), :5205,5306 (serialize/load resolved config)
- src/core/model_registry.py:203-212 (DB lookups orchestrator-only)
- src/agent.py:900-1029 (per-job tools/graph/checkpointer/resume trace)
- src/agent.py:1258-1301 (_make_checkpointer, per-process setup())
- src/agent.py:2160-2228 (config overrides fetch first-run-only, freeze resolved_config)
- src/agent.py:3932-3993 (_resume_from_checkpoint, up to 3 aget probes)
- src/core/backends/remote.py:203-300,371-513 (RemoteBackend connect/keepalive), :516-523 (disconnect kills tmux), :554-583 (_ensure_connected reuse+reseed), :1129-1158 (_init_shell kill-session+new-session)
- src/api/persistent_session.py:308-517 (setup steps, workspace retry budget), :1972-1987 (_bind_tools)
- cockpit/src/app/core/services/persistent-chat.service.ts:1259-1309 (loadHistory cache-first delta), :1628-1680 (_handleGoneBeyondHorizon: cache clear + full reload + meta + reopen), :1694-1728 (resumedFromEpoch)
- langgraph/checkpoint/postgres/base.py:87-111 (SELECT_SQL latest-per-channel; installed lib copy)
- helm/values.yaml:400-405 (agent 512Mi request / 2Gi limit)
- k3d live measurements (srw-postgres-0 psql): thread_messages avg 1004B/row max 32kB; thread_events avg 168B/row; 20-turn thread = 3 epochs today
- git 37a16928 (2026-05-12, unconditional epoch bump since inception), 20c01951 (rewind re-epoch)
