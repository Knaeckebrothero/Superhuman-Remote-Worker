# Postgres Audit Store — Verified Code Contracts (discovery output)

Companion to `postgres_audit_store_implementation.md`. These are the four
verbatim contract reports extracted from the tree on 2026-06-10/11 by the
discovery agents — the ground truth the schema/adapter were synthesized
against. Every claim carries file:line anchors (the tree had local
modifications on `fix/worker-pod-state-zombie-on-cancel`; re-grep before
relying on exact numbers).

---

# 1. Write-side contract

# Audit-store WRITE-side contract (MongoDB → Postgres swap)

All paths relative to `/home/ghost/Repositories/Superhuman-Remote-Worker/`.

## 0. Topology

- **Sole production writer**: `LLMArchiver` in `src/core/archiver.py` (1167 lines), constructed lazily via module singleton `get_archiver()` (src/core/archiver.py:1117-1126; `_default_archiver` global at :1114, created once, **never closed** — no caller of `LLMArchiver.close()` exists anywhere).
- `LLMArchiver` does not speak pymongo directly for connection; it wraps `src/database/mongo_db.py:MongoDB` (instantiated at src/core/archiver.py:194) and pulls raw collection handles off `MongoDB.db` (archiver.py:250-252). All inserts/updates then use pymongo collection methods directly.
- Gate: `LLMArchiver.from_env()` (archiver.py:202-224) returns `None` when `MONGODB_URL` is unset → `get_archiver()` returns None → every call site guards with `if auditor:`. Design doc's claim "archiver.py:209 gates on MONGODB_URL" verified (:209-212).
- **Writers are batch-worker only — verified**: `src/persistent_graph.py` has zero archiver references; `src/api/persistent_app.py:29` imports only `inflight_tool_call` (a pure message-inspection function, archiver.py:127-160, no DB). Root `agent.py` entry point: zero archiver references. Matches design doc § Scope.
- `orchestrator/` contains **zero** `insert_one`/`update_one`/`insert_many` (grep verified) — `orchestrator/database/mongodb.py` is the async motor READ side (plus `ensure_indexes()` DDL). The write side never runs in the orchestrator process.

## 1. Write methods — full contract

### 1.1 `LLMArchiver.__init__` / connection
```python
def __init__(self, mongodb_url: str, database_name: str = "srw_logs",
             collection_name: str = "llm_requests",
             audit_collection_name: str = "agent_audit")          # archiver.py:172-200
```
State: `_collection` (llm_requests), `_audit_collection` (agent_audit), `_chat_history_collection` (hardcoded `"chat_history"`, archiver.py:252), `_connected`, `_connection_attempted`, `_step_counters: Dict[str,int]` (archiver.py:195-200).

`_ensure_connected()` (archiver.py:226-269): lazy; **one attempt per process** — `if self._connection_attempted: return False` (:235-236) means a failed first connect silently disables archiving for the process lifetime (no retry). On success it also runs a **write-side DDL**: `self._collection.create_index([("job_id",1),("call_type",1),("timestamp",1)], background=True)` (:256-262) — this index is NOT in `MONGODB_INDEX_DECLARATIONS` (orchestrator/database/mongodb.py:80-119), i.e. it's a 6th llm_requests index the design doc's § Indexing inventory misses.

### 1.2 `archive()` → **llm_requests, insert_one** (archiver.py:309-441)
```python
def archive(self, job_id: str, agent_type: str, messages: Sequence[BaseMessage],
            response: AIMessage, model: str, latency_ms: Optional[int] = None,
            iteration: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
            phase: Optional[str] = None, phase_number: Optional[int] = None,
            tool_schemas: Optional[List[Dict[str, Any]]] = None,
            model_kwargs: Optional[Dict[str, Any]] = None,
            call_type: str = "main",
            auxiliary_metadata: Optional[Dict[str, Any]] = None) -> Optional[str]
```
Document fields (archiver.py:354-403):
- `job_id: str`, `agent_type: str`, `timestamp: datetime.now(timezone.utc)`, `model: str`, `call_type: str` — always.
- `request: dict` — `{messages: [_message_to_dict(m)], message_count: int}` + conditionally `tools` (tool_schemas list) + `tool_count` + `model_kwargs` (:354-362). **Full message bodies, no truncation.**
- `response: dict` — `_message_to_dict(response)` (:371).
- Conditional: `latency_ms: int` (if not None, :375), `iteration: int` (if not None, :378), `metadata: dict` (`_serialize_for_mongo`, if truthy, :381-382), `auxiliary_metadata: dict` (serialized, if truthy, :384-385).
- `metrics: dict` — `{input_chars: int, output_chars: int, tool_calls: int, token_usage: dict}` (:393-403); `token_usage` from `response.response_metadata["token_usage"]`.
- **`phase`/`phase_number` params are NOT written to llm_requests** — only forwarded to `_archive_chat_entry` (:424-435). The design-doc llm_requests DDL (no phase columns) is consistent with this, but the adapter signature must still accept them.

`_message_to_dict` shape (archiver.py:87-124): `{type: <class name>, content: <str via _normalize_content>, role: system|human|assistant|tool}` + AIMessage: `tool_calls: [{id, name, args}]`; ToolMessage: `tool_call_id`, `name`; any: `additional_kwargs`, `response_metadata` when non-empty. `_serialize_for_mongo` (:57-69) recursively stringifies `uuid.UUID`.

Returns `str(result.inserted_id)` (24-hex ObjectId) or None. Side effect: when `call_type == "main"`, calls `_archive_chat_entry(..., request_id=doc_id)` (:421-435). Caller use of return: only graph.py:1450 keeps it (`request_id`) to thread into `update_llm_response`; all other callers discard it.

### 1.3 `_archive_chat_entry()` → **chat_history, insert_one** (archiver.py:546-668; private, sole caller is `archive()`)
```python
def _archive_chat_entry(self, job_id, agent_type, messages, response, model,
                        latency_ms, iteration, request_id, phase, phase_number) -> None
```
Document fields (:645-664):
- Always: `job_id: str`, `agent_type: str`, `timestamp: utcnow`, `iteration: int|None` (written even if None), `model: str`, `latency_ms: int|None` (written even if None), `inputs: list`, `response: dict`, `request_id: str` (the llm_requests ObjectId string — the cross-collection link).
- Conditional: `phase: str` (if truthy, :657), `phase_number: int` (if not None, :659), `reasoning: {content, content_preview≤500}` (only if `response.additional_kwargs.reasoning_content`, :632-642, 661).
- `inputs` = delta: messages after the last AIMessage, SystemMessages excluded (:580-607). Entry shape: `{type: "human"|"tool", content: <full str>, content_preview: ≤500}` + for ToolMessage `tool_call_id`, `tool_name`.
- `response` = `{content: <full>, content_preview: ≤500, has_tool_calls: bool}` + `tool_calls: [{id, name, args_preview: ≤200-char str}]` if present (:609-630).
No return value; guards `if self._chat_history_collection is None: return` (:576).

### 1.4 `audit_step()` → **agent_audit, insert_one** (archiver.py:703-775) — the generic event writer
```python
def audit_step(self, job_id: str, agent_type: str, step_type: str, node_name: str,
               iteration: int, data: Optional[Dict[str, Any]] = None,
               latency_ms: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None,
               phase: Optional[str] = None, phase_number: Optional[int] = None) -> Optional[str]
```
Document fields (:738-762): always `job_id, agent_type, iteration, step_number (from _get_next_step_number), step_type, node_name, timestamp (utcnow)`; conditional `phase` (if not None), `phase_number` (if not None), `latency_ms` (if not None), `metadata` (serialized, if truthy); then **`doc.update(_serialize_for_mongo(data))` — data keys merge at the TOP LEVEL of the document** (:761-762). So agent_audit documents carry variable top-level keys (`tool`, `llm`, `state`, `check`, `transition`, `phase` (dict! collides semantically with the string `phase` field when both present — at graph.py:2102 the `data={"phase": {...}}` dict **overwrites** the `phase: "strategic"|"tactical"` string set at :749-750, since `doc.update(data)` runs after), `workspace`, `error`, `warning`, `count`, `total_tokens`, `started_at`, `completed_at`, `feedback_length`, `existing_id`, `id`, etc.). The Postgres `payload JSONB` design nests these under one column — readers that consumed top-level keys must be mapped; the `phase` dict/string collision at step_type=phase_complete is a live data quirk worth knowing about.
Returns `str(inserted_id)` or None; all callers discard the return except via the wrappers below.

### 1.5 `audit_tool_call()` — pre-phase wrapper (archiver.py:777-840), delegates to `audit_step`
```python
def audit_tool_call(self, job_id: str, agent_type: str, iteration: int, tool_name: str,
                    call_id: str, arguments: Dict[str, Any],
                    metadata: Optional[Dict[str, Any]] = None,
                    phase: Optional[str] = None, phase_number: Optional[int] = None) -> Optional[str]
```
Fixed `step_type="tool"`, `node_name="tools"`. Per-key argument preview truncation to 200 chars (str directly; dict/list via `str()`; scalars pass through) (:807-815). `data = {"tool": {name, call_id, arguments: args_preview, result_preview: None, result_size_bytes: None, success: None, error: None}, "started_at": utcnow, "completed_at": None}` (:823-836). Return: doc id; caller (graph.py:3257-3258) stores it in `audit_ids[call_id]`.

### 1.6 `update_tool_result()` → **agent_audit, update_one** (archiver.py:842-893) — post phase
```python
def update_tool_result(self, audit_doc_id: str, result: str, success: bool,
                       latency_ms: int, error: Optional[str] = None) -> bool
```
Filter: `{"_id": ObjectId(audit_doc_id)}` (bson imported lazily inside, :866). `$set` (:868-879): `tool.result_preview` (≤500 + "... [truncated]"), `tool.result_size_bytes` (len(result) or 0), `tool.success`, `completed_at` (utcnow), `latency_ms`; plus `tool.error` (≤500) only if `error` truthy. Returns `modified_count > 0`; on 0 modifications logs `"[AUDIT] No document updated..."` warning and returns False (:886-889). **Caller ignores the bool** (graph.py:3429, no assignment).

### 1.7 `audit_llm_call()` — pre-phase wrapper (archiver.py:895-949), delegates to `audit_step`
```python
def audit_llm_call(self, job_id: str, agent_type: str, iteration: int, model: str,
                   input_message_count: int, state_message_count: int,
                   metadata: Optional[Dict[str, Any]] = None,
                   phase: Optional[str] = None, phase_number: Optional[int] = None) -> Optional[str]
```
Fixed `step_type="llm"`, `node_name="execute"`. `data = {"llm": {model, input_message_count, request_id: None, response_content_preview: None, tool_calls: None, metrics: None}, "state": {message_count}, "started_at": utcnow, "completed_at": None}` (:924-948).

### 1.8 `update_llm_response()` → **agent_audit, update_one** (archiver.py:951-1006) — post phase
```python
def update_llm_response(self, audit_doc_id: str, request_id: Optional[str],
                        response_preview: str, tool_calls: List[Dict[str, Any]],
                        output_chars: int, latency_ms: int) -> bool
```
Filter `{"_id": ObjectId(audit_doc_id)}`. `$set` (:979-989): `llm.request_id` (the llm_requests ObjectId string — links audit row to request row), `llm.response_content_preview`, `llm.tool_calls` (list of `{name, call_id}` built by caller, graph.py:1466-1474), `llm.metrics: {output_chars, tool_call_count}`, `completed_at` (utcnow), `latency_ms`. Returns `modified_count > 0`; caller ignores it (graph.py:1492).

### 1.9 Module-level convenience `archive_llm_request(...)` (archiver.py:1129-1167)
Same params as `archive()`; wraps `get_archiver().archive(...)`. **Zero production callers** (grep across repo) — only mentioned in docstrings.

### 1.10 Legacy dead writers in `src/database/mongo_db.py` (NOT used by archiver, NO callers anywhere)
- `MongoDB.archive_llm_request(job_id, agent_type, messages: List[Dict], response: Dict, model, **metadata) -> Optional[str]` → llm_requests insert with a DIFFERENT shape (top-level `messages`, `**metadata` splat, naive `datetime.utcnow()`) (mongo_db.py:126-166).
- `MongoDB.audit_tool_call(job_id, agent_type, tool_name, inputs, output=None, error=None, **metadata)` → agent_audit insert with `event_type: "tool_call"` (NOT `step_type`) (mongo_db.py:168-212).
- `MongoDB.audit_phase_transition(job_id, agent_type, from_phase, to_phase, **metadata)` → agent_audit insert `event_type: "phase_transition"` (mongo_db.py:214-247).
These three are dead code (grep: only docstring references at src/database/__init__.py:25, mongo_db.py:38/47). The adapter shim does not need to port them. Reads on this class (`get_job_audit_trail` :249, `get_llm_conversation` :275, `get_statistics` :301) also have no callers. The archiver uses only `MongoDB.db` property (:321-330) + `close()`.

## 2. Two-phase pattern (exact)

**Chain A — LLM call** (all inside `async def execute`, src/graph.py:731):
1. PRE: `llm_audit_id = auditor.audit_llm_call(...)` graph.py:1119-1129 — inserts agent_audit doc with `llm.*` response fields None.
2. `response = await asyncio.wait_for(llm_with_tools.ainvoke(...))` :1145-1148 (real await between pre and post).
3. `request_id = auditor.archive(...)` :1450-1463 — llm_requests insert.
4. POST: `auditor.update_llm_response(audit_doc_id=llm_audit_id, request_id=request_id, response_preview=content_str[:500], tool_calls=tool_calls_preview, output_chars=len(content_str), latency_ms=latency_ms)` :1492-1499, guarded `if llm_audit_id:` :1477 (skips the update when the pre insert failed/returned None).
The id passed around is the stringified Mongo ObjectId of the pre document; `request_id` is the llm_requests ObjectId string.

**Chain B — tool calls** (all inside `async def audited_tools`, src/graph.py:3072):
1. PRE: loop over `tool_calls_info`; `doc_id = auditor.audit_tool_call(...)` :3246-3256; `audit_ids: Dict[str,str]` maps `call_id → audit_doc_id` :3243, 3257-3258.
2. `result = await tool_node.ainvoke(state)` :3262 (await between pre and post; tools execute, possibly in executor threads).
3. POST: for each ToolMessage in `result["messages"]`: `auditor.update_tool_result(audit_doc_id, result=content, success=not _is_tool_error(content), latency_ms=execution_time_ms // max(len(tool_calls_info),1), error=content[:500] if is_error else None)` :3419-3436. Note: **latency is the whole batch wall-clock divided evenly across calls**, not per-tool. `success` is a string heuristic (`_is_tool_error`, graph.py:372-384: lowercase content contains "error:", "failed:", "exception:", "traceback").

Sync/async: every archiver method is **sync** (pymongo) and is called **without await** from async graph nodes — verified `grep "await.*auditor\.|await.*archiver\."` returns nothing in src/. Writes block the event loop for their duration. Because the pre→post gap contains awaits, a crash mid-call leaves a pre doc with null result fields — exactly the contract the design's append-only `event_phase='post'` second INSERT preserves.

## 3. Error handling — exact behavior

- Not configured: `from_env()` → None (archiver.py:209-212); call sites skip via `if auditor:`/`if not archiver: return`.
- Connect failure: `MongoDB._connect` catches `(ConnectionFailure, ServerSelectionTimeoutError)` and generic `Exception` → `logger.warning` → False (mongo_db.py:105-112); `serverSelectionTimeoutMS=5000` (:92). Archiver side: `_ensure_connected` catches Exception → `logger.warning(f"Failed to connect to MongoDB: {e}")` → False (archiver.py:267-269), and the one-shot gate:
  ```python
  if self._connection_attempted:
      return False              # archiver.py:235-236
  ```
  → after one failed attempt, all subsequent writes for the process return None/False instantly and silently (no further log lines).
- Every write swallows everything:
  - `archive`: `except Exception as e: logger.warning(f"Failed to archive LLM request: {e}"); return None` (archiver.py:439-441)
  - `_archive_chat_entry`: warning `"Failed to archive chat entry"` → return None (:667-668)
  - `audit_step`: warning `"Failed to audit step"` → return None (:773-775)
  - `update_tool_result`: warning `"Failed to update tool result"` → return False (:891-893)
  - `update_llm_response`: warning `"Failed to update LLM response"` → return False (:1004-1006)
- If Mongo dies AFTER a successful connect, each write raises inside pymongo and is swallowed per-call (so per-call latency cost while down, one warning per write) — only the never-connected case short-circuits.
- Caller-side handling: graph nodes do not wrap archiver calls in try/except (they rely on the methods never raising); recall_store/auxiliary/vision/audio additionally wrap their whole archive block in try/except logging "Failed to archive ... call" (auxiliary.py:707-710, vision_helper.py:323-324, audio_helper.py:637-638).
- `is_available` (design-doc adapter naming) does not exist on the write side: actual surface is `from_env()→None` + private `_ensure_connected()` + `MongoDB.is_connected` property (mongo_db.py:333-335, unused by archiver). The orchestrator read class has `is_available`; the doc's unified surface merges the two namings.

## 4. step_number mechanism (`_get_next_step_number`, archiver.py:271-301)

- In-process counter `self._step_counters[job_id]`, lazily seeded on first use per job by querying Mongo: `find_one({"job_id": job_id}, sort=[("step_number", -1)], projection={"step_number": 1})`; falls back to 0 on exception or no doc (:284-299). Then `+= 1` and return. **Resume semantics: a new agent process continues numbering from the persisted max** — the Postgres design (global BIGSERIAL + read-time ROW_NUMBER) must preserve monotonic per-job ordering across resume, which it does.
- Used ONLY by `audit_step` (and thus `audit_tool_call`/`audit_llm_call`). `archive`/`_archive_chat_entry` have no step numbers.
- Two-phase updates do NOT consume a new step_number — pre and post live in one document, so today "post" events have no ordering identity of their own; the append-only redesign gives the post row a new id, which is strictly more ordered.
- Ordering consumers (read side, orchestrator/database/mongodb.py): paginated audit sorts by `step_number` (:352-355), audit timerange first/last by step_number asc/desc (:448-458), bulk audit by step_number (:638-639), graph deltas by step_number and exposes `stepNumber` in the wire shape (:757-758, :774), "last step" lookup (:826). The archiver's own `get_job_audit_trail` sorts by step_number (archiver.py:1032-1034) — dead code. Mongo indexes `(job_id, step_number)` and `(job_id, iteration, step_number)` exist for these (orchestrator/database/mongodb.py:101-105).
- Concurrency of the counter: all `audit_step` callers run on the agent's single asyncio event-loop thread (graph nodes + `asyncio.create_task` background aux tasks); the method is fully synchronous (no await inside), so increments can't interleave mid-call — uniqueness holds in-process. Vision/audio threads call only `archive()` (no counter). The design doc's "race-prone under asyncio.gather" is overstated for the current code (single-threaded loop), but correct in spirit: nothing enforces it.

## 5. The four aggregation pipelines — exact stages and CALLERS

All four live on `LLMArchiver`; **grep across src/, agent.py, orchestrator/, scripts/, tests/ finds ZERO production callers** for `get_job_stats`/`get_audit_stats` (the orchestrator's identically-named `get_job_stats` MCP/builder tool at orchestrator/mcp/server.py:887 etc. calls an orchestrator REST endpoint, not the archiver). These pipelines are dead code; the design doc's "the four pipelines we actually use (archiver.py:510, :530, :1066, :1093)" overstates — they translate to SQL trivially but nothing currently invokes them.

1. `get_job_stats` overall (archiver.py:493-510, executed :510 on `llm_requests`):
   `[{$match: {job_id}}, {$group: {_id: "$job_id", total_requests: {$sum: 1}, total_input_chars: {$sum: "$metrics.input_chars"}, total_output_chars: {$sum: "$metrics.output_chars"}, total_tool_calls: {$sum: "$metrics.tool_calls"}, avg_latency_ms: {$avg: "$latency_ms"}, first_request: {$min: "$timestamp"}, last_request: {$max: "$timestamp"}, models_used: {$addToSet: "$model"}}}]` → single dict, `_id` popped (:514-515).
2. `get_job_stats` by call_type (:518-530, executed :530 on `llm_requests`):
   `[{$match: {job_id}}, {$group: {_id: "$call_type", count: {$sum:1}, input_chars: {$sum: "$metrics.input_chars"}, output_chars: {$sum: "$metrics.output_chars"}}}]` → reshaped into `stats["by_call_type"][call_type] = {count, input_chars, output_chars}` (:531-538).
3. `get_audit_stats` by step_type (:1054-1066, executed :1066 on `agent_audit`):
   `[{$match: {job_id}}, {$group: {_id: "$step_type", count: {$sum:1}, avg_latency_ms: {$avg: "$latency_ms"}, total_latency_ms: {$sum: "$latency_ms"}}}]` → `{total_steps: int, by_step_type: {<type>: {count, avg_latency_ms, total_latency_ms}}}` (:1068-1079).
4. `get_audit_stats` timerange (:1082-1093, executed :1093 on `agent_audit`):
   `[{$match: {job_id}}, {$group: {_id: None, first_step: {$min: "$timestamp"}, last_step: {$max: "$timestamp"}, max_iteration: {$max: "$iteration"}}}]` → merged into stats as `first_step`/`last_step`/`max_iteration` (:1094-1097).
Both methods return `{}` when unconnected or on exception (:488-489, :542-544, :1050-1051, :1101-1103).

## 6. Threading / concurrency

- The agent process is a single asyncio event loop. Archiver methods are sync pymongo, never awaited — each write **blocks the loop**.
- Fire-and-forget asyncio tasks (`asyncio.create_task`) that write through the archiver:
  - `extract_and_store_memories` — graph.py:1553 (execute node) and :2046 (archive_phase node) → inside, `AuxiliaryLLM.chain()` archives (call_type `memory_extraction`) and `recall_store.store()` audit_steps (`memory_dedup`/`memory_store`).
  - `assemble_memories` — graph.py:1587 → call_type `memory_assembly`.
  - `curate_and_store_knowledge` — graph.py:2150 → call_type `knowledge_curation`.
  These interleave with main-loop writes at await boundaries (step_number stays consistent, see §4).
- **Real OS threads**: sync workspace tools (e.g. image read at src/tools/workspace/files.py:245, audio at :303) call `describe_image_sync`/`transcribe_sync`, which use `run_async` (vision_helper.py:26-46): when a loop is running it spawns a `concurrent.futures.ThreadPoolExecutor` and runs `asyncio.run(coro)` in that worker thread — the subsequent `archiver.archive()` (vision_helper.py:313, audio_helper.py:627) therefore executes **off the main loop thread**. pymongo's MongoClient is thread-safe so this works today; the Postgres write adapter must be callable from arbitrary threads (a sync, thread-safe seam — it cannot assume one event loop). Sync tools themselves also run in LangGraph ToolNode's executor threads.
- No locks anywhere in archiver or mongo_db (no `threading.Lock`). `get_archiver()` singleton creation is unguarded (benign double-create race).

## 7. Complete write call-site inventory (29 sites + 2 wiring)

`src/graph.py` (worker graph; `auditor = get_archiver()` fetched fresh per node — :528, :596, :920, :1113, :2100, :2381, :2503, :2546, :2579, :2607, :2893, :3242):
- :530 `audit_step` step_type=`initialize`, node `init_workspace`, iter 0, data `{"workspace": {"created": False}}`, phase="strategic", phase_number=0
- :598 `audit_step` `initialize`, node `init_strategic_todos`, data `{phase_alternation, strategic_todos, task_brief_length, instructions_length}`
- :922 `audit_step` `memory_inject`, node `execute`, data `{count, total_tokens}`
- :1119 `audit_llm_call` (PRE) — model=phase model, input_message_count=len(prepared_messages), state_message_count=len(messages)
- :1189 `audit_step` `warning`/`execute`, data `{"error": {type: "empty_response", message, streak, model}}`
- :1255 `audit_step` `warning`/`execute`, data `{"error": {type: "parser_failure", message, streak, model, content_sample}}`
- :1354 `audit_step` `warning`/`execute`, data `{"error": {type: "response_degeneration", message, streak, patterns, content_length, content_preview}}`
- :1416 `audit_step` `warning`/`execute`, data `{"warning": {type: "response_validation_warning", patterns, details}}`
- :1450 `archive` (call_type default "main") — messages=prepared_messages, tool_schemas=phase-specific schemas, model_kwargs=phase model kwargs
- :1492 `update_llm_response` (POST)
- :1687 `audit_step` `error`/`execute`, data `{"error": {type: "context_overflow", message, token_count, limit, recoverable: False}}`
- :1753 `audit_step` `warning`/`execute`, data `{"error": {type: "tool_use_failed", message, streak, failed_generation_preview, failed_generation_length}}`
- :1812 `audit_step` `error`/`execute`, data `{"error": {type: "llm_error", message≤500, recoverable: False, classification: "permanent", attempts}}`
- :1892 `audit_step` `error`/`execute`, data `{"error": {type: "llm_error", message≤500+auth_hint, recoverable: True, attempts}}`
- :2102 `audit_step` `phase_complete`/`archive_phase`, data `{"phase": {completed, archive_path}}` (note: this data key overwrites the string `phase` field — §1.4)
- :2383 `audit_step` `phase_transition`/`handle_transition`, data `{"transition": {from_phase, to_phase, success, error, new_phase_number}}`
- :2505, :2548, :2581, :2609 `audit_step` `check`/`check_goal`, data `{"check": {decision: goal_achieved|frozen|continue, goal_achieved, should_stop?, reason?|next_phase?}}`
- :2895 `audit_step` `feedback_resume`/`restore_from_feedback`, data `{feedback_length, resume_todos, messages_before, messages_after}`
- :3246 `audit_tool_call` (PRE), :3429 `update_tool_result` (POST)
All graph sites pass `metadata=state.get("metadata")` (job metadata dict) and `agent_type=config.agent_id` (the expert id, e.g. "universal" — naming crossover: the *agent_type* field carries *agent_id* values).

`src/services/recall_store.py` (archiver injected via ctor param :233/:253; wired at src/agent.py:1787 with `archiver=get_archiver()`):
- :370 `audit_step` `memory_dedup`, node `recall_store`, iteration=0, data `{existing_id, source, similarity}` — no phase/metadata
- :448 `audit_step` `memory_store`, iteration=0, data `{id, type, source, importance, tokens}`
- :725 `audit_step` `memory_retrieve`, iteration=0, data `{count, total_tokens}`
(job_id passed as `str(self.job_id)`; agent_type=`self.agent_id or ""`.)

`src/services/auxiliary.py` (`AuxiliaryLLM._archive_call`, :677-710; archiver wired via `set_job_context` at src/agent.py:477-481):
- :697 `archive` — call_type from `_TASK_CALL_TYPES` (:351-356): SummarizeTask→`summarization`, ExtractMemoriesTask→`memory_extraction`, AssembleMemoriesTask→`memory_assembly`, CurateKnowledgeTask→`knowledge_curation`, fallback `"auxiliary"`; auxiliary_metadata=`{task_class, [iterations, tool_calls_made]}`; no phase/iteration/metadata args.

`src/services/vision_helper.py`:
- :313 `archive` — agent_type=`"vision"`, call_type=`"vision"`, auxiliary_metadata `{trigger: "vision", page_num?}`; synthetic `HumanMessage`/`AIMessage` (image replaced by placeholder text); model from VISION_MODEL env. Call sites :166, :250 inside `async def describe_image`/pdf path; reachable from executor threads via `describe_image_sync` :181-192.

`src/services/audio_helper.py`:
- :627 `archive` — agent_type=`"transcription"`, call_type=`"transcription"`, auxiliary_metadata `{trigger, file_name, file_size, transcript_length, language, chunk_index?, total_chunks?}`. Call site :558.

Wiring: src/agent.py:475-481 (aux), :1777-1788 (recall store). `src/core/__init__.py:35-36` lazily re-exports `get_archiver`/`LLMArchiver`.

Observed `call_type` value set: `main, summarization, memory_extraction, memory_assembly, knowledge_curation, auxiliary (fallback), vision, transcription`. The archiver docstring (:341-342) omits `transcription`/`auxiliary` — minor doc rot. Observed `step_type` set (13): `initialize, memory_inject, llm, warning, error, tool, phase_complete, phase_transition, check, feedback_resume, memory_dedup, memory_store, memory_retrieve` — exactly matches the design doc's agent_audit comment (postgres_audit_store.md:263-266) **except** the doc omits `warning` from the inline enum comment (it lists `warning`? — it lists `llm|tool|check|initialize|warning|error|...` — yes, included; full match).

## 8. Tests affected by a backend swap

Write-side / archiver-coupled:
- `tests/test_audio_helper.py` — `TestArchiving` (:385+): patches `src.core.archiver.get_archiver`, asserts `archive()` kwargs (job_id, agent_type="transcription", model, call_type, auxiliary_metadata keys, response.content) — breaks on any signature/shape change.
- `tests/test_vision_helper.py` — :54-56 stubs `helper._archive_vision_call` with MagicMock ("so we don't touch any orchestrator/database modules").
- `tests/test_responses_api.py` — imports `_normalize_content` from `src.core.archiver` (:14; pure function — must survive the refactor or move).
- `tests/test_persistent_app.py` — imports `inflight_tool_call` from `src.core.archiver` (:99-140; pure function).
- `tests/test_database_phase1.py` — `TestMongoDB` (:97+) tests `src.database.MongoDB` env/url handling; dies when mongo_db.py is deleted.
- `tests/test_graph_image_postprocessing.py:52`, `tests/test_stuck_detection.py:45` — fake config carries `mongodb: list` field (tool/datasource config); these tests exercise graph nodes which call `get_archiver()` — passes today because MONGODB_URL is unset under pytest, so audit is skipped. The Postgres adapter must keep an equivalent "unconfigured → None/no-op" path or these graph tests start needing a DB.
Read-side (orchestrator):
- `tests/test_audit_pagination.py` — `orchestrator.database.mongodb.MongoDB.get_job_audit` signature + unavailable branch (:11-18).
- `tests/test_job_access.py` — patches `main.mongodb.is_available = False` to skip audit-count enrichment (:48-52).
Out of scope (customer-datasource Mongo only): `tests/test_tool_registry.py` (:101, :622-625), `tests/test_run_command.py` (:303+), `tests/test_datasource_redesign.py` (:89-133).
Confirmed: `tests/test_archiver.py` does not exist and `mongomock` appears nowhere — matches the design doc's correction.

## 9. Suspicious sweep (src/, outside archiver.py / mongo_db.py / tools/mongodb/)

- `src/core/datasource_setup.py:586-588` — `from pymongo import MongoClient` + `MongoClient(url, serverSelectionTimeoutMS=5000)`: customer-attached MongoDB datasources. OUT of scope (boundary: pymongo stays a runtime dep for this).
- `src/agent.py:177, :2358`, `src/api/persistent_session.py:143`, `src/core/datasource_setup.py:57, :560` — comments/cleanup registry for datasource MongoClients. Out of scope.
- `src/api/persistent_app.py:1016-1024` — datasource tool catalog naming (`mongo_query` etc.). Out of scope.
- `src/database/__init__.py:41` re-exports `MongoDB` (delete-after-cutover surface, matches doc).
- No `motor`, no `insert_one`/`update_one`/`insert_many`, no other `MongoClient` in src/. No mongo writes anywhere in orchestrator/.

## 10. Doc-vs-code mismatches to flag (code wins)

1. **The four aggregation pipelines are dead code** — design doc § Tradeoffs calls them "the four pipelines we actually use"; nothing calls `get_job_stats`/`get_audit_stats` (or any archiver read method). The adapter's write side doesn't need them; keep only if the unified surface wants parity.
2. **Adapter write-surface naming**: doc lists `connect()/disconnect()/is_available`; actual write side is `from_env()→Optional`, private `_ensure_connected()`, `close()` (never called), `MongoDB.is_connected` (unused). The "unavailable → no-op" behavior is implemented as Optional-singleton + per-call `if auditor:` guards at every one of the 29 sites, plus the one-shot `_connection_attempted` gate.
3. **`agent_id UUID` column in the doc's llm_requests DDL has no source field today** — the writer emits only `agent_type` (which actually carries `config.agent_id` values from graph call sites). Net-new column or derive at write time.
4. **Doc's § Indexing inventory misses the agent-side index**: archiver creates `(job_id, call_type, timestamp)` on llm_requests at every connect (archiver.py:256-262), distinct from the 5 declared in `MONGODB_INDEX_DECLARATIONS`. Its only query consumer (`get_conversation`) is dead code, so the Postgres set's `(call_type, timestamp)` choice is fine — but the migration deletes this create_index path with the file.
5. **`audit_step` merges `data` at document top level**, and at step_type=`phase_complete` the `data={"phase": {...}}` dict overwrites the `phase` string field (graph.py:2102 vs archiver.py:749/762). The doc's separate `phase TEXT` + `payload JSONB` columns silently fix this collision — flag it so the read adapter doesn't expect `phase='strategic'` on phase_complete rows migrated conceptually from old data.
6. **`archive()` accepts but does not persist `phase`/`phase_number` in llm_requests** (chat_history only). Doc DDL agrees (no phase columns on llm_requests; chat_history has them) — but anyone "widening to capture every field the writer emits" should know these two params are pass-through.
7. **Thread-safety requirement is real, not theoretical**: vision/audio archive writes run on ThreadPoolExecutor threads (run_async, vision_helper.py:37-44). The doc's adapter section doesn't state a threading contract for the sync shim under `src/database/` — it must be thread-safe or marshal to the loop.
8. `update_tool_result` latency is batch-time / n (graph.py:3433-3434) — per-row `latency_ms` on tool rows is approximate; don't promise per-tool latency in the new schema docs.
9. Doc § Scope writes-per-job table credits all three collections to `LLMArchiver` — correct, with the nuance that `chat_history` is written only via the private `_archive_chat_entry` cascade inside `archive()` when `call_type=="main"`, never independently.

---

# 2. Read-side contract

# Postgres Audit Store — Definitive READ-side Contract (orchestrator → cockpit)

Verified 2026-06-10 against the working tree. Code wins over docs; mismatches in § M.

## 0. Wiring

- Singleton: `mongodb = MongoDB()` at `orchestrator/main.py:237`; imported via `from database import (PostgresDB, MongoDB, ALLOWED_TABLES, FilterCategory, MIGRATIONS_VECTOR_DIR)` at `main.py:94-100`; `from graph_routes import router as graph_router, set_mongodb` at `main.py:227`.
- Lifespan connect: `await mongodb.connect()` `main.py:3325`; `await mongodb.ensure_indexes()` `main.py:3332` (no Postgres analogue — migration runner owns DDL); `set_mongodb(mongodb)` shares the instance with graph_routes at `main.py:3391`.
- Shutdown: `await mongodb.disconnect()` `main.py:3672`.
- `MongoDB.connect()` (`mongodb.py:189-217`): `AsyncIOMotorClient(url, serverSelectionTimeoutMS=5000)` + `admin.command("ping")`; DB name parsed from URL path else `"srw_logs"` (`:166,173-182`); any failure → WARN log, `_available=False`, never raises. `is_available` is a plain latched bool (`:184-187`) — **never re-checked after startup; flipped only by connect/disconnect** (see M8).
- Exports: `orchestrator/database/__init__.py` exports `MongoDB, FILTER_MAPPINGS, FilterCategory`.

## 1. FilterCategory semantics (`mongodb.py:54-62`)

```python
FILTER_MAPPINGS = {"all": [], "messages": ["llm"], "tools": ["tool"], "errors": ["error"]}
FilterCategory = Literal["all", "messages", "tools", "errors"]
```
- `all` → no `step_type` clause; `messages` → `step_type ∈ {llm}`; `tools` → `{tool}`; `errors` → `{error}`. Applied as `query["step_type"] = {"$in": step_types}` (`:330-332`). Unknown category falls to `.get(..., [])` → no filter. Used only by `get_job_audit` and (dead) `get_page_for_timestamp`.
- Writer step_type universe is broader (free-form via `audit_step` `src/core/archiver.py:703-775`; `tool` set at `:820`, `llm` at `:927`; plus initialize/check/warning/error/phase_transition/etc. per design doc) — filters intentionally cover only llm/tool/error.

## 2. Timestamp / _id serialization

Two converters exist and they disagree at the margins:
- `mongodb._to_iso_utc` (`mongodb.py:21-36`): naive datetime → `isoformat()+"Z"` (microsecond precision); tz-aware → `astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]+"Z"` (millisecond precision).
- `CustomJSONEncoder` (`main.py:3262-3281`, applied app-wide via `CustomJSONResponse` `main.py:3284-3295`): identical naive/aware logic.
- Pre-converted in the adapter: bulk endpoints (top-level `timestamp` only), `/timerange`, `/llm-requests`, `get_job_version.lastUpdate`. NOT converted (raw datetime → encoder at render): `/audit` entries, `/chat` entries, `/requests/{doc_id}`, and any nested datetimes (`started_at`, `completed_at`) everywhere. Since PyMongo decodes BSON dates as naive UTC, today both paths emit microsecond `...Z`; asyncpg returns tz-aware → post-migration both converters truncate to milliseconds (cosmetic wire change).
- Exception: `graph_routes._get_all_tool_calls` uses bare `.isoformat()` (`graph_routes.py:156-162`) → `/api/graph/changes` timestamps have **no Z suffix** today.
- `_id`: always `str(doc["_id"])` (24-hex ObjectId string) before returning — `mongodb.py:363, 431, 569, 643, 699, 773, 907`; `graph_routes.py:156`.

## 3. Endpoint contracts

### 3.1 GET /api/jobs/{job_id}/audit (`main.py:8390-8441` → `get_job_audit` `mongodb.py:285-377`)
- Params: `page: int = Query(1, ge=-1)`; `page_size: int = Query(50, ge=1, le=200, alias="pageSize")`; `offset: Optional[int] = Query(None, ge=0)`; `limit: Optional[int] = Query(None, ge=1, le=200)`; `order: Literal["asc","desc"]="asc"`; `filter: FilterCategory="all"`. Auth: `require_job_access` first (`:8416`).
- Method signature: `get_job_audit(job_id, page=1, page_size=50, filter_category="all", offset=None, limit=None, order="asc")`.
- Query: filter `{"job_id": job_id}` + optional `{"step_type": {"$in": [...]}}`; `total = count_documents(query)` (`:335`); skip resolution: `offset` wins if set, else `page==-1` → `max(1, ceil(total/size))` then `skip=(page-1)*size` (`:337-347`); `effective_size = limit if limit is not None else page_size` (`:313`); cursor `find(query).sort("step_number", 1|-1).skip(skip).limit(size)` (`:350-358`). No projection.
- Response: `{entries, total, page, pageSize, offset, limit, hasMore}`; `hasMore=(skip+size)<total` (`:349`, computed independent of order); `page` echoed as `skip//size + 1` (`:367`). Entries = full writer docs (`_id` stringified, datetimes raw): audit doc keys per `archiver.py:738-762` — `_id, job_id, agent_type, iteration, step_number, step_type, node_name, timestamp, [phase], [phase_number], [latency_ms], [metadata]` + merged `data`: tool steps → `tool:{name, call_id, arguments(previews ≤200ch), result_preview(≤500), result_size_bytes, success, error}, started_at, completed_at` (`archiver.py:806-840`, post-filled by `$set` UPDATE `:868-880`); llm steps → `llm:{model, input_message_count, request_id, response_content_preview, tool_calls, metrics:{output_chars, tool_call_count}}, state:{message_count}, started_at, completed_at` (`:930-945`, post-fill `:979-993`).
- Degraded: 200 `{entries:[], total:0, page:<raw echo, may be -1>, pageSize, offset, limit, hasMore:false, error:"MongoDB not available"}` (`main.py:8418-8428`). Exceptions → 500.
- SQL: `SELECT count(*) FROM agent_audit WHERE job_id=$1 [AND step_type=ANY($2)]` + `SELECT *, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY id) AS step_number FROM agent_audit WHERE ... ORDER BY id ASC|DESC OFFSET $3 LIMIT $4`. **Awkward**: append-only pre/post doubling — either JOIN-merge (`pre LEFT JOIN post ON post.request_id=pre.request_id AND post.event_phase='post'`, payload `pre.payload || post.payload`) or serve doubled rows and let cockpit collapse; see M5. `step_number` must be synthesized over logical (pre) rows so values match `total`.

### 3.2 GET /api/requests/{doc_id} (`main.py:8444-8476` → `get_request` `mongodb.py:407-432`)
- `doc_id: str` path param. Degraded: **503** `"MongoDB not available"` (`:8452-8456`).
- Parse/validate: `oid = ObjectId(doc_id)` inside `try/except InvalidId → return None` (`mongodb.py:419-422`; `from bson.errors import InvalidId` `:41`). Any non-24-hex input → None → endpoint runs `require_approved_user` then **404** `"Request '<id>' not found"` (`main.py:8460-8466`) — invalid format and not-found are indistinguishable (no 400).
- Query: `find_one({"_id": oid})` on `llm_requests`; no projection; `_id` stringified; datetimes raw → encoder.
- Auth is fetch-then-gate: found doc with `job_id` → `require_job_access(job_id)`; missing `job_id` (legacy) → `_require_admin` (`:8467-8471`).
- Doc shape (writer `archiver.py:354-403`): `_id, job_id, agent_type, timestamp, model, call_type, request:{messages[], message_count, [tools], [tool_count], [model_kwargs]}, response (message dict), [latency_ms], [iteration], [metadata], [auxiliary_metadata], metrics:{input_chars, output_chars, tool_calls, token_usage}`.
- SQL: `SELECT * FROM llm_requests WHERE id=$1`. Param becomes `int` per design — note FastAPI then 422s non-numeric input where today it 404s (M6).

### 3.3 GET /api/jobs/{job_id}/audit/timerange (`main.py:8479-8493` → `get_audit_time_range` `mongodb.py:434-469`)
- No params. Auth `require_job_access` (`:8486`). Degraded: **200 body `null`** (`:8487-8488`); also `null` when job has no entries.
- Query: two `find_one({"job_id": job_id}, sort=[("step_number", ±1)], projection={"timestamp": 1})` (`:449-460`). Response `{"start": iso, "end": iso}` via `_to_iso_utc`.
- SQL: `SELECT MIN(timestamp) AS start, MAX(timestamp) AS end FROM agent_audit WHERE job_id=$1` (today first/last is by step_number, not min/max timestamp — equivalent unless counter order diverges from clock; for exact parity order by `id`).

### 3.4 GET /api/jobs/{job_id}/chat (`main.py:8496-8531` → `get_chat_history` `mongodb.py:523-579`)
- Params: `page: int = Query(1, ge=-1)`; `page_size = Query(50, ge=1, le=200, alias="pageSize")`. No offset/limit style. Auth first (`:8513`).
- Query: `{"job_id": job_id}`; `total=count_documents`; `page==-1` → last page; `skip=(page-1)*page_size`; `find().sort("timestamp", 1).skip(skip).limit(page_size)` (`:565`). No projection; `_id` stringified; timestamps raw.
- Response: `{entries, total, page, pageSize, hasMore}` — **no offset/limit keys** (differs from audit shape). Chat doc keys (writer `archiver.py:644-662`): `_id, job_id, agent_type, timestamp, iteration, model, latency_ms, inputs:[{type:"human"|"tool", content, content_preview(≤500), [tool_call_id], [tool_name]}], response:{content, content_preview, has_tool_calls, [tool_calls:[{id, name, args_preview(≤200)}]]}, request_id (stringified llm_requests ObjectId), [phase], [phase_number], [reasoning:{content, content_preview}]`.
- Degraded: 200 `{entries:[], total:0, page:<raw>, pageSize, hasMore:false, error:"MongoDB not available"}` (`main.py:8514-8522`).
- SQL: count + `SELECT * FROM chat_history WHERE job_id=$1 ORDER BY timestamp, id OFFSET LIMIT` (tie-break by id; trivial).

### 3.5 GET /api/jobs/{job_id}/audit/bulk (`main.py:9311-9345` → `get_job_audit_bulk` `mongodb.py:599-655`)
- Params: `offset = Query(0, ge=0)`, `limit = Query(5000, ge=1, le=5000)`. Auth first.
- Query: `{"job_id": job_id}` (no filter, no projection); `total=count`; adapter re-clamps `limit=min(limit,5000)` (`:633`); `hasMore=(offset+limit)<total`; `find().sort("step_number", 1).skip(offset).limit(limit)` (`:639`). Per doc: `_id` → str AND top-level `timestamp` → `_to_iso_utc` (`:643-646`); nested datetimes left to encoder.
- Response: `{entries, total, offset, limit(clamped echo), hasMore}`.
- Degraded: 200 same shape empty + `error:"MongoDB not available"` (`main.py:9328-9336`).
- SQL: as 3.1 without filter, ASC only; same pre/post stitching question (M5).

### 3.6 GET /api/jobs/{job_id}/chat/bulk (`main.py:9348-9382` → `get_chat_history_bulk` `mongodb.py:657-710`)
- Identical param/response/degraded contract to 3.5 but collection `chat_history`, sort `("timestamp", 1)` (`:695`), key `entries`.
- SQL: `SELECT * FROM chat_history WHERE job_id=$1 ORDER BY timestamp, id OFFSET $2 LIMIT $3` + count.

### 3.7 GET /api/jobs/{job_id}/graph/bulk (`main.py:9385-9419` → `get_graph_deltas_bulk` `mongodb.py:712-785`)
- Params as 3.5. Auth first. Degraded: 200 `{deltas:[], total:0, offset, limit, hasMore:false, error:"MongoDB not available"}` (`:9402-9410`).
- Query (`mongodb.py:740-746`): `{"job_id": job_id, "step_type": "tool", "tool.name": {"$in": ["cypher_query", "cypher_execute", "execute_cypher_query"]}}`; count; clamp 5000; `find().sort("step_number", 1).skip(offset).limit(limit)` (`:758`).
- Delta extraction (`:760-777`): per doc → `{toolCallIndex: offset+i (running index), timestamp: _to_iso_utc|None, cypherQuery: doc.tool.arguments.query or "", toolCallId: str(_id), stepNumber: doc.step_number}`. Response `{deltas, total, offset, limit, hasMore}`.
- SQL: `SELECT id, timestamp, payload#>>'{tool,arguments,query}' AS q, ROW_NUMBER() ... FROM agent_audit WHERE job_id=$1 AND step_type='tool' AND payload->'tool'->>'name' = ANY('{cypher_query,cypher_execute,execute_cypher_query}') AND event_phase='pre' ORDER BY id OFFSET LIMIT` + matching count — must pin `event_phase='pre'` (arguments live on the pre row; post rows would double-count) and uses the planned expression index `agent_audit_tool_name_idx`.

### 3.8 GET /api/jobs/{job_id}/version (`main.py:9422-9440` → `get_job_version` `mongodb.py:787-843`)
- No params. Auth first. Degraded: **200 `null`** (`:9434-9435`); also `null` when `audit_count == 0`.
- Computation, four sequential queries:
  1. `auditEntryCount = agent_audit.count_documents({"job_id": job_id})` (`:805`); if 0 → return None (`:807-808`).
  2. `chatEntryCount = chat_history.count_documents({"job_id": job_id})` (`:810`).
  3. `graphDeltaCount = agent_audit.count_documents({job_id, step_type:"tool", "tool.name": {"$in": [cypher_query, cypher_execute, execute_cypher_query]}})` (`:813-821`).
  4. `lastUpdate`: `find_one({"job_id"}, sort=[("step_number", -1)], projection={"timestamp":1})` → `_to_iso_utc` else None (`:824-832`).
  - `version = hash((audit_count, chat_count, graph_count))` (`:835`) — tuple-of-ints, deterministic across processes (PYTHONHASHSEED only randomizes str/bytes); `lastUpdate` NOT in the hash.
- Response: `{version, auditEntryCount, chatEntryCount, graphDeltaCount, lastUpdate}`.
- SQL: one CTE over agent_audit — `SELECT COUNT(*) FILTER (WHERE event_phase='pre') AS audit, COUNT(*) FILTER (WHERE event_phase='pre' AND step_type='tool' AND payload->'tool'->>'name' = ANY(...)) AS graph, MAX(timestamp) AS last FROM agent_audit WHERE job_id=$1` + scalar subquery `(SELECT COUNT(*) FROM chat_history WHERE job_id=$1)`. Roadmap (`postgres_audit_store_roadmap.md:270-272`) requires single-query for race-freedom. **Awkward**: counts must be logical-step counts (pre rows / DISTINCT request_id) or every post-INSERT bumps `auditEntryCount`, changing cache-invalidation cadence (arguably fine — more invalidations — but counts then ≠ `/audit` `total` unless that also counts raw rows; keep the two consistent).

### 3.9 GET /api/jobs/{job_id}/llm-requests (`main.py:14623-14649` → `list_llm_requests` `mongodb.py:849-932`)
- Params: `limit = Query(20, ge=1, le=100)`, `offset = Query(0, ge=0)`. Auth first (`:14636`). Degraded: **503** (`:14637-14638`). Extra: `UUID(job_id)` validation → **400** `"Invalid job_id format"` (`:14640-14643`) — unique among these endpoints.
- Query: `{"job_id": job_id}`; adapter clamps `limit=min(limit,100)` (`:880`); `total=count`; `hasMore=(offset+limit)<total`; projection `{_id:1, job_id:1, timestamp:1, model:1, token_usage:1, iteration:1, response:1}` (`:888-896`); `find(query, projection).sort("timestamp", 1).skip(offset).limit(limit)`.
- Post-processing (`:905-924`): `_id`→str; `timestamp`→`_to_iso_utc`; `response` popped and reduced to `tool_calls: [{"name": tc.name|"?"}]`. **`token_usage` is projected but never exists top-level** — writer nests it at `metrics.token_usage` (`archiver.py:393-403`) → entries lack it despite the endpoint docstring promising token usage (M4).
- Response: `{entries:[{_id, job_id, timestamp, model, iteration, tool_calls:[{name}]}], total, offset, limit, hasMore}`.
- SQL: `SELECT id, job_id, timestamp, model, iteration, response->'tool_calls' AS tc, metrics->'token_usage' AS token_usage FROM llm_requests WHERE job_id=$1 ORDER BY timestamp, id LIMIT/OFFSET` + count — chance to actually fix token_usage (or omit for byte parity; decide explicitly).

### 3.10 GET /api/graph/changes/{job_id} (`orchestrator/graph_routes.py:39-130`)
- **No auth dependency** — router mounted bare (`main.py:3796`), no `require_job_access`/`Depends` anywhere in graph_routes.py (M7).
- Degraded: **503** `"MongoDB not available"` if instance None or `not is_available` (`:52-57`). Other exceptions → 500 (`:129-130`).
- Raw collection access (`_get_all_tool_calls` `:133-165`): `collection = mongodb._db["agent_audit"]` (`:142` — private attr, not the `.db` property); query `{"job_id": job_id, "step_type": "tool"}`; `find(query).sort("step_number", 1)` — **unbounded, no limit/projection**; `_id`→str, `timestamp`→bare `.isoformat()` (no Z).
- Delta extraction (`:62-114`): Python-filters to `tool.name ∈ {cypher_query, cypher_execute, execute_cypher_query}`; per call: `query = entry.tool.arguments.query or ""`, `parse_cypher_query(query)` (regex CREATE/MERGE/DELETE/SET/REMOVE extraction `:168-356`) → delta `{timestamp, toolCallIndex: i (0-based over graph calls), cypherQuery, toolCallId, stepNumber, changes:{nodesCreated, nodesDeleted, nodesModified, relationshipsCreated, relationshipsDeleted, matchedVariables}}`. Snapshots every `clamp(sqrt(n), 50, 100)` ops + first op + chain cap 50 + >50-node create/delete (`:106-111`, `_build_snapshots` `:400-535`); summary via `_compute_summary` (`:612-642`).
- Response: `{jobId, timeRange:{start,end}|null, summary:{totalToolCalls, graphToolCalls, nodesCreated, nodesDeleted, nodesModified, relationshipsCreated, relationshipsDeleted}, snapshots, deltas}`; zero graph calls → zeroed summary, empty arrays, `timeRange: null` (`:71-86`).
- SQL replacement (design: `iter_tool_calls(job_id)`): `SELECT * FROM agent_audit WHERE job_id=$1 AND step_type='tool' AND event_phase='pre' ORDER BY id` — pre rows carry the arguments the parser needs; keep results streamed/chunked since today's call is unbounded.

### 3.11 audit_count enrichers (N+1 `count_documents`)
- `GET /api/jobs` (`main.py:3933-3939`): if available, per row `job["audit_count"] = await mongodb.get_audit_count(str(job["id"]))`; else `None`.
- `GET /api/jobs/{job_id}` (`main.py:3953-3956`): single-row same.
- `GET /api/projects/{project_id}/jobs` (`main.py:18814-18819`): per-row same (design doc cites 18740-18742 — drifted).
- `get_audit_count` (`mongodb.py:379-392`): `agent_audit.count_documents({"job_id": job_id})`; 0 when unavailable (never hit — main.py pre-checks).
- SQL: collapse to `SELECT job_id, COUNT(*) FROM agent_audit WHERE job_id = ANY($1::uuid[]) [AND event_phase='pre'] GROUP BY job_id` (design § Scope explicitly wants this collapse).

## 4. Degraded-mode matrix (Mongo unavailable at startup)

| Endpoint | Behavior |
|---|---|
| /api/jobs/{id}/audit | 200, empty shape + `error:"MongoDB not available"` (`main.py:8418-8428`) |
| /api/requests/{doc_id} | **503** (`:8452-8456`) |
| /api/jobs/{id}/audit/timerange | 200 `null` (`:8487-8488`) |
| /api/jobs/{id}/chat | 200, empty + `error` (`:8514-8522`) |
| /api/jobs/{id}/audit/bulk | 200, empty + `error` (`:9328-9336`) |
| /api/jobs/{id}/chat/bulk | 200, empty + `error` (`:9365-9373`) |
| /api/jobs/{id}/graph/bulk | 200, empty `deltas` + `error` (`:9402-9410`) |
| /api/jobs/{id}/version | 200 `null` (`:9434-9435`) |
| /api/jobs/{id}/llm-requests | **503** (`:14637-14638`) |
| /api/graph/changes/{id} | **503** (`graph_routes.py:52-57`) |
| 3 enrichers | 200, `audit_count: null` |

The `error` key is added by main.py, not the adapter — mongodb.py's own degraded returns (`:315-324, 541-548, 617-624, 673-680, 728-735`) lack it and are effectively unreachable behind main.py's `is_available` pre-checks. Because `_available` is startup-latched (`mongodb.py:207-217`), a Mongo outage AFTER connect produces 500s (motor exceptions) on every endpoint, not these shapes (M8).

## 5. Aggregations — NOT in mongodb.py (M1)

`orchestrator/database/mongodb.py` contains zero `aggregate()` pipelines (`get_statistics` `:998-1016` is two whole-collection `count_documents` + `connected` flag; no callers). The four pipelines the design doc cites live in the sync write-side `LLMArchiver` (`src/core/archiver.py`) and have **zero callers** repo-wide (grep over src/, orchestrator/, tests/; the MCP/builder `get_job_stats` is unrelated — it GETs `/api/stats/jobs`, a Postgres job-queue stat, `orchestrator/mcp/client.py:1060-1064`):
- `get_job_stats(job_id)` (`archiver.py:479-544`), collection `llm_requests`: P1 `[{$match:{job_id}}, {$group:{_id:"$job_id", total_requests:{$sum:1}, total_input_chars:{$sum:"$metrics.input_chars"}, total_output_chars:{$sum:"$metrics.output_chars"}, total_tool_calls:{$sum:"$metrics.tool_calls"}, avg_latency_ms:{$avg:"$latency_ms"}, first_request:{$min:"$timestamp"}, last_request:{$max:"$timestamp"}, models_used:{$addToSet:"$model"}}}]`; P2 `[{$match}, {$group:{_id:"$call_type", count:{$sum:1}, input_chars:..., output_chars:...}}]`. Output: flat dict of P1 fields (minus `_id`) + `by_call_type: {<call_type>: {count, input_chars, output_chars}}`; `{}` degraded.
- `get_audit_stats(job_id)` (`archiver.py:1041-1103`), collection `agent_audit`: P3 `[{$match}, {$group:{_id:"$step_type", count:{$sum:1}, avg_latency_ms:{$avg:"$latency_ms"}, total_latency_ms:{$sum:"$latency_ms"}}}]`; P4 `[{$match}, {$group:{_id:null, first_step:{$min:"$timestamp"}, last_step:{$max:"$timestamp"}, max_iteration:{$max:"$iteration"}}}]`. Output: `{total_steps, by_step_type:{<step_type>:{count, avg_latency_ms, total_latency_ms}}, first_step, last_step, max_iteration}`; `{}` degraded.
- SQL: P1+P2 → `SELECT count(*), sum((metrics->>'input_chars')::bigint), ..., avg(latency_ms), min(timestamp), max(timestamp), array_agg(DISTINCT model) FROM llm_requests WHERE job_id=$1` + `... GROUP BY call_type`; P3+P4 → `SELECT step_type, count(*), avg(latency_ms), sum(latency_ms) FROM agent_audit WHERE job_id=$1 [AND event_phase='pre'] GROUP BY ROLLUP(step_type)` or two queries. `$addToSet` is unordered vs `array_agg(DISTINCT)` ordered (roadmap risk noted). ~30 LoC, trivial — but consider not porting at all since both are caller-less.

## 6. MONGODB_INDEX_DECLARATIONS + ensure_indexes (`mongodb.py:80-119, 228-274`)

14 indexes total, `(collection, [(keys, name), ...])` in pymongo create_index format:
- `llm_requests` (5): `job_id`/idx_job_id; `agent_type`/idx_agent_type; `timestamp`/idx_timestamp; `model`/idx_model; `[(job_id,1),(agent_type,1),(timestamp,-1)]`/idx_job_agent_time.
- `agent_audit` (7): `job_id`/idx_audit_job_id; `step_type`/idx_audit_step_type; `node_name`/idx_audit_node_name; `timestamp`/idx_audit_timestamp; `[(job_id,1),(step_number,1)]`/idx_audit_job_step; `[(job_id,1),(iteration,1),(step_number,1)]`/idx_audit_job_iter_step; `[(job_id,1),(agent_type,1),(step_type,1)]`/idx_audit_job_agent_type.
- `chat_history` (2): `job_id`/idx_chat_job_id; `[(job_id,1),(timestamp,1)]`/idx_chat_job_timestamp.
- `ensure_indexes()`: no-op returning 0 when unavailable; per-index `create_index(keys, name=...)` (idempotent no-op on identical existing); per-failure ERROR log + aggregate ERROR summary; returns asserted count. Called every startup `main.py:3332`; also consumed by `orchestrator/init.py:1590` (`_create_mongodb_indexes`, `:1573-1593`). Matches design doc § Indexing inventory exactly. Postgres analogue: none — migration family owns DDL (design agrees).

## 7. Sort keys and ordering provenance

- `agent_audit` reads sort by `step_number` everywhere (`mongodb.py:355, 449-459, 639, 758, 824-827`; `graph_routes.py:152`); `chat_history` and `llm_requests` reads sort by `timestamp` (`:565, 695, 898-903`). Legacy dead methods sort by `timestamp`.
- `step_number` is written by the racy in-process per-job counter `LLMArchiver._get_next_step_number` (`archiver.py:271`, `dict[job_id] += 1`). Design replaces it with BIGSERIAL `id` ordering + read-time `ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY id)` — strictly stronger; but the wire field `step_number` (used by cockpit and echoed as `stepNumber` in graph deltas) must keep existing, synthesized from ROW_NUMBER over logical rows.

## M. Code-vs-doc mismatches (code wins)

- **M1**: Design § Adapter lists `get_job_stats` / `get_audit_stats` under "Read surface (mirrors orchestrator/database/mongodb.py)" and roadmap P3 schedules them in the reader — they actually live in `src/core/archiver.py:479/:1041` (sync, write-side) and have zero callers anywhere. Porting them creates an uncalled API; decide drop-vs-port explicitly.
- **M2**: Seven mongodb.py read members exist that neither design nor roadmap sentences: `get_page_for_timestamp` (`:471-517`, zero callers incl. cockpit), `get_job_ids_with_audit` (`:394-405`), `get_chat_history_count` (`:581-593`), legacy `get_job_audit_trail` (`:938-966`), `get_llm_conversation` (`:968-996`), `get_statistics` (`:998-1016`), `db` property (`:1018-1025` — even graph_routes uses the private `_db`, not this property). All dead; should be explicitly dropped, not silently ported.
- **M3**: Line drift vs design doc: `/llm-requests` endpoint is `main.py:14623-14649` (doc says 14563/14572); project-jobs enricher is `main.py:18814-18819` (doc says 18740-18742). All other cited anchors verified accurate (96/227, 237, 3325-3332, 3391, 3672, 3933-3936, 3953-3954, 8418/8431, 8444-8459, 8487/8491, 8514/8525, 9328/9339, 9365/9376, 9402/9413, 9434/9438, graph_routes 20/142-144).
- **M4**: `list_llm_requests` projects top-level `token_usage` that the writer never sets (nested at `metrics.token_usage`, `archiver.py:393-403`) — responses silently lack it while the endpoint docstring (`main.py:14630-14634`) promises token usage. Design doc doesn't mention it; the SQL port must decide fix-vs-parity.
- **M5**: Append-only read contract is ambiguous in the docs: design § "agent_audit append-only" says reads stitch pre/post with "a JOIN or `DISTINCT ON (request_id) ... ORDER BY id DESC`", but `DISTINCT ON` latest-only returns the post row, which carries only result fields (no `tool.arguments`) — not equivalent to a JOIN-merge. Meanwhile design § Verification 2 and roadmap P6 (`roadmap:505-508`) expect the doubled post row to appear on the wire with the cockpit collapsing. These are three different contracts (JOIN-merge server-side / latest-only / wire-doubled+client-collapse); `total`, `step_number`, `auditEntryCount`, and `get_audit_count` parity all hinge on which is chosen.
- **M6**: `/api/requests/{doc_id}` with `doc_id: int` makes FastAPI 422 non-numeric input; today invalid 24-hex returns 404 (after `require_approved_user`). Wire-visible status change for non-cockpit consumers; also today's 404-on-invalid does auth before disclosure — the int route loses that probe-auth call (no auth on 422).
- **M7**: `/api/graph/changes/{job_id}` has no auth gate (router mounted bare `main.py:3796`; no `require_job_access` / `Depends` in graph_routes.py) while all nine main.py audit reads gate via `require_job_access` (or fetch-then-gate). Design's `iter_tool_calls` refactor is the natural moment to add the gate.
- **M8**: "Graceful degradation" is startup-only: `_available` is latched in `connect()` (`mongodb.py:207-217`) and never re-evaluated; runtime Mongo loss → motor exceptions → 500s on all endpoints (the 200-with-error/null shapes only occur when Mongo was down at boot). The Postgres `AuditStore.is_available` semantics (static config vs live pool health) should be specified.
- **M9**: Serialization inconsistencies to either preserve or normalize: bulk/timerange/llm-requests pre-convert top-level timestamps via `_to_iso_utc` while /audit, /chat, /requests and all nested datetimes flow through `CustomJSONEncoder`; identical output for naive datetimes (microsecond `...Z`) but tz-aware values (asyncpg default) truncate to milliseconds → cutover changes timestamp precision on the wire. `/api/graph/changes` timestamps additionally lack the `Z` suffix today (`graph_routes.py:156-162`).

## Boundary note (out of scope, confirmed)

`src/tools/mongodb/` (customer-datasource tools), datasource type enums / test-connection / tool catalogs in main.py, and `DEFAULT_DS_MONGODB_URL` in `orchestrator/init.py:701/738` are customer-datasource surface — untouched by this migration. `src/database/mongo_db.py` (sync, 338 LoC) is write-side: its `get_job_audit_trail`/`get_llm_conversation`/`get_statistics` (`:249-319`) and `db` property (`:321-330`, consumed by `archiver.py:245-252`) serve the agent process, not the cockpit read path.

---

# 3. Cockpit / API-consumer contract

# Mongo Document-Shape Consumers — Verification Report

Scope: internal audit store consumers only. `src/tools/mongodb/` (customer datasources) confirmed untouched by any finding below; boundary holds.

## 0. Consumer topology (verified, corrects implicit doc assumptions)

The cockpit calls only these endpoints (grep of all `ApiService` callers):
- `/api/jobs/{id}/audit/bulk` — `data.service.ts:528`
- `/api/jobs/{id}/chat/bulk` — `data.service.ts:548`
- `/api/jobs/{id}/graph/bulk` — `data.service.ts:562`
- `/api/jobs/{id}/version` — `data.service.ts:294, :497`
- `/api/requests/{doc_id}` — `request.service.ts:69` (via `api.service.ts:256-257`)
- `/api/graph/changes/{id}` — `graph.service.ts:171` (via `api.service.ts:268-270`)

`api.service.ts` methods `getJobAudit` (:225), `getChatHistory` (:302), `getAuditTimeRange` (:283, already `@deprecated`) have **no cockpit callers** — paginated `/audit`, `/chat`, `/audit/timerange` are consumed only by the MCP client (`orchestrator/mcp/client.py:132, :145, :173, :511, :549, :578`) and builder dispatch, all rendered to text via `orchestrator/services/formatters.py` (shape-tolerant f-strings). `/api/jobs/{id}/llm-requests` (`main.py:14623`) has **no cockpit consumer at all** — only `mcp/client.py:1465` → `formatters.format_llm_requests`.

## 1. Every site assuming id is a 24-hex string

### Cockpit type/interface fields
- `cockpit/src/app/core/models/audit.model.ts:65` — `AuditEntry._id: string`
- `cockpit/src/app/core/models/audit.model.ts:43` — `AuditLLMInfo.request_id?: string | null` — **value is `str(ObjectId)` of the llm_requests doc** (writer: `src/core/archiver.py:432` `request_id=doc_id`, stored at `:980` as `llm.request_id`). Becomes BIGINT under the new schema (`agent_audit.request_id BIGINT REFERENCES llm_requests(id)`). The design doc's cockpit list omits this field.
- `cockpit/src/app/core/models/chat.model.ts:50` — `ChatEntry._id: string`
- `cockpit/src/app/core/models/chat.model.ts:63` — `ChatEntry.request_id?: string` (same ObjectId carrier; writer `archiver.py:654`)
- `cockpit/src/app/debug/request.model.ts:72` — `LLMRequest._id: string`
- `cockpit/src/app/core/models/cache.model.ts:29-34` — `CachedChatEntry.id: string` with comment "MongoDB _id (unique per entry)"; populated from `entry._id` at `indexed-db.service.ts:250` and used as the Dexie **primary key** for `chatEntries` (`indexed-db.service.ts:43, :62`)
- `cockpit/src/app/debug/graph.model.ts:73` — `GraphDelta.toolCallId: string`, comment "Tool call ID from MongoDB" — value is `str(audit_doc._id)` (`orchestrator/database/mongodb.py:773` and `orchestrator/graph_routes.py:100`). **No cockpit code reads `.toolCallId`** (only model + cache passthrough) — it is an opaque token.

### Regex / validation / input surfaces
- `cockpit/src/app/debug/services/request.service.ts:60` — `/^[a-fA-F0-9]{24}$/.test(docId)` hard gate; `:61` error text "expected 24 hex characters"; `:53` `loadRequest(docId: string)`; `:14` `currentDocId = signal<string | null>`
- `cockpit/src/app/debug/components/request-viewer/request-viewer.component.ts:22` — placeholder "Enter document ID (24 hex chars)..."; `:611` `docIdInput = ''`; `:614` submit; `:67` renders `request()?._id`

### Set / Map / trackBy keyed on these ids
- `agent-activity.component.ts:671` — `expandedIds = signal<Set<string>>(new Set())`; consumed via `track entry._id` (`:73`), `isExpanded/toggleExpanded(entry._id)` (`:77, :82, :95, :98`), typed `entryId: string` (`:799, :812`)
- `chat-history.component.ts:690` — `selectedTabs = signal<Map<string, string>>` keyed by `entry._id`; template `track entry._id` (`:94`), `getSelectedTab/selectTab(entry._id, ...)` (`:143, :151`), typed `entryId: string` (`:868-877`); comment `:689`
- (`parsedShellCache` at `chat-history.component.ts:693` is keyed on the raw shell_state string, NOT an id — not affected)

### Runtime-fatal string-method calls (worse than type errors — template `.slice()` on a number throws at runtime)
- `chat-history.component.ts:191` — `{{ entry.request_id.slice(0, 8) }}...`
- `agent-activity.component.ts:156` — `{{ getRequestId(entry)!.slice(0, 12) }}...`; `getRequestId` at `:942-944` does untyped `this.asAny(entry.llm)?.['request_id']` (escapes TS checking entirely), feeds `requestService.loadRequest()` at `:947` → hits the 24-hex regex → with an integer id the regex **silently rejects every lookup** ("Invalid document ID format")
- `chat-history.component.ts:764-765` — `onRequestIdClick(requestId: string)` → same regex gate

### Test fixtures that fabricate string `_id`s
- `cockpit/src/app/core/services/data.service.spec.ts:25` (`_id: audit_${i}`), `:40` (`_id: chat_${i}`)
- `cockpit/src/app/core/services/indexed-db.service.spec.ts:67, :93, :267, :275, :285, :290`

## 2. IndexedDB sync flow (exact behavior)

**Feeders**: `DataService.loadJob` (`data.service.ts:281-318`) → version check → on miss `fetchAndCacheJob` (`:520-585`) loops `/audit/bulk` → `cacheAuditEntries`, `/chat/bulk` → `cacheChatEntries`, `/graph/bulk` → `cacheGraphDeltas` (BULK_FETCH_SIZE=5000, `:39`).

**Cache keys** (`indexed-db.service.ts`): audit = composite `${jobId}_${index}` positional (`:157`, NOT the `_id` — type flip irrelevant here); chat = `entry._id` PK (`:250`); graph = `${jobId}_${toolCallIndex}` (`:318`). Dexie schema v4 (`:58-67`).

**Validity check**: `data.service.ts:296-297` — `metadata && versionInfo && metadata.auditEntryCount === versionInfo.auditEntryCount`. **Nothing else.** `JobVersionInfo.version`, `chatEntryCount`, `graphDeltaCount`, `lastUpdate` are all ignored by the cockpit.

**CRITICAL DOC MISMATCH — version bump is a no-op**: the design doc (§ Cockpit/API "Cache invalidation", file-by-file list) and roadmap P4 say to "bump `cache.model.ts` version so existing IndexedDB entries with string IDs are discarded cleanly". In code, `CACHE_VERSION = 4` lives at `indexed-db.service.ts:17` and is only ever **written** into metadata (`:236, :304, :377`). `JobCacheMetadata.version` (`cache.model.ts:67`) is **never read or compared anywhere** (verified by grep). Bumping it discards nothing. Actual invalidation requires either (a) a `metadata.version !== CACHE_VERSION → treat as invalid + clearJob` branch in `data.service.ts:296`, or (b) a Dexie `version(5).upgrade(tx → clear tables)` migration in `CockpitDatabase`.

**Shape-mismatch behavior today**: on count mismatch, `loadJob:308-311` calls `fetchAndCacheJob` **without clearing old rows**. Audit/graph composite keys overwrite in place, but chat rows keyed by `_id` would NOT overwrite old string-keyed rows → duplicates returned by the `[jobId+timestamp]` read (`indexed-db.service.ts:267-272`). Only `refresh()` (`:402`) and `autoRefreshTick` (`:501-507`, fires when `auditEntryCount > currentMax`) clear first. Cached `data` payloads are served verbatim with zero shape validation (`loadWindow` → `e.data`). The planned cluster wipe makes job IDs disjoint, which is the only thing actually protecting the cutover — old cached jobs just become unreachable garbage, not corruption. State that explicitly rather than relying on the fictional version mechanism.

**Bonus footgun**: `version` on the wire is Python `hash((audit, chat, graph))` (`mongodb.py:835`) — a 64-bit int that can exceed `Number.MAX_SAFE_INTEGER` in the `JobVersionInfo.version: number` field (`api.service.ts:98`). Harmless only because it's unused; drop it or make the new adapter return the counts tuple as a string.

## 3. Wire-format recommendation: rename `_id` → `id` (agree with design doc), with caveats

**Option A — keep `_id`, value becomes integer.** Forced changes: the 5 model fields in §1 (`_id: string → number`, `request_id` likewise); `CachedChatEntry.id` type; `Set<string>→Set<number>` + `Map<string,…>→Map<number,…>` + method signatures in both components; regex `request.service.ts:60` → `/^\d+$/` + error text + placeholder; `api.getRequest(docId)` param; the two `.slice()` templates (TS flags `.slice` on number); spec fixtures. Untouched: template `track entry._id` interpolations, `indexed-db.service.ts:250`, `formatters.py:491/:574`, `main.py:14633` docstring. Leaves a permanently misleading field name and the `getRequestId` untyped-access path (`agent-activity:943`) silently passing a number into a string pathway.

**Option B — rename to `id` + integer.** Everything in Option A **plus** ~10 mechanical sites: `audit.model/chat.model/request.model` field renames; templates `agent-activity:73,77,82,95,98` and `chat-history:94,143,151` (+ comment `:689`); `request-viewer:67`; `indexed-db.service.ts:250`; both spec files. TS compiles these as hard errors → the compiler enumerates the cockpit blast radius exhaustively, which Option A does not (interpolation-only sites keep compiling). The only **silent** breaks under rename are Python text formatters: `orchestrator/services/formatters.py:491` `entry.get("_id", "?")` and `:574` `request.get('_id', 'unknown')` (would display "?" / "unknown"), plus the `main.py:14633` docstring — the design doc's claim that "builder_dispatch.py and formatters.py are already clean" is true for **ObjectId/24-hex strings** but FALSE for the `_id` field name; these two `.get("_id")` sites must ride along.

**Recommendation: Option B (rename)**, because (1) clean cutover removes all compat pressure, (2) TS turns the larger blast radius into a compile-time checklist, (3) every other Postgres-backed cockpit model already uses `id` (`JobSummary.id`, `audit.model.ts:99`), (4) only 2 display-only Python sites can fail silently and they're enumerated here. Additionally: have the adapter emit `GraphDelta.toolCallId` as `str(id)` (it's already a stringified value today, no validation anywhere, nothing reads it) — zero cockpit change for graph deltas; and keep `llm.request_id`/`ChatEntry.request_id` the **same name** but integer, replacing the `.slice()` displays with plain interpolation (BIGINTs are short).

## 4. Non-cockpit consumers — verified

- `orchestrator/mcp/server.py:347-361` — `get_llm_request(doc_id: str)` FastMCP tool; `:354` "MongoDB ObjectId (24 hex characters)". ✓ exact.
- `orchestrator/mcp/client.py:259-270` (sync) and `:695-707` (async) — `get_llm_request` docstrings `:263`/`:700` ✓ exact; both GET `/api/requests/{doc_id}`.
- `orchestrator/services/builder_tools.py:1058-1078` — `get_llm_request` OpenAI tool schema, `:1072` "MongoDB ObjectId (24 hex characters)" ✓ exact; dispatched via `builder_dispatch.py:302-306` through the MCP client.
- Fresh grep `ObjectId|24 hex|{24}` over `orchestrator/`: **only** the above 4 + `orchestrator/database/mongodb.py` itself (`:40, :46, :362, :411, :420, :430-431, :569`) — design doc's "three sites + one builder site" count confirmed, no stragglers.
- Extra field-name consumers (not in doc): `formatters.py:491, :574` (`_id` reads, §3); `formatters.py:85-92` + `builder_dispatch.py:354-357` read `llm.request_id` into f-strings (int-safe, name-stable); `main.py:14633` docstring tells agents to use `_id`.
- Route param: `main.py:8444-8445` `doc_id: str` → `int` (✓ matches doc). Invalid-id handling moves from `InvalidId → None → 404` (`mongodb.py:420-422`) to FastAPI 422 — MCP retry decorator treats 4xx fine, but the "not found vs malformed" distinction changes shape (404 body vs 422 validation error) for agent tooling.
- N+1 enrichers: `main.py:3936, :3954, :18816-18819` (third site drifted from doc's 18740-18742; main.py is locally modified). Returns `audit_count` int — type-stable, no consumer break (`JobSummary.audit_count?: number|null`, `audit.model.ts:112`).
- `graph_routes.py:100` `toolCallId: entry["_id"]`, `:142` raw `mongodb._db["agent_audit"]` cursor, `:156` stringification — feeds `/api/graph/changes/{id}` → cockpit `graph.service.ts:171` (uses deltas/snapshots, never toolCallId) + `mcp/client.py:251/:687`.
- Dead code found: `mongodb.py:471-523` `get_page_for_timestamp` has **no caller** in main.py — exclude from the adapter surface (not in the design doc's read-surface list either; consistent).

## 5. Timestamp serialization risks (Postgres timestamptz)

**Today's wire is THREE inconsistent formats**:
1. Bulk audit/chat (`mongodb.py:646, :701`), graph deltas (`:766`), `/version.lastUpdate` (`:832`), `/audit/timerange` (`:466-467`), `/llm-requests` (`:909`) → `_to_iso_utc` (`mongodb.py:21-36`): naive branch `isoformat()+"Z"` = 6-digit microseconds + Z (`:30`, the live path — motor returns naive UTC); aware branch truncates to 3-digit millis + Z (`:34`).
2. Paginated `/audit` (`mongodb.py:361-364`), `/chat` (`:568-571`), `/requests/{id}` (`:431`) → datetimes left raw → FastAPI `isoformat()` → **naive, NO suffix** (`2026-…T12:34:56.789000`). JS `new Date()` parses suffix-less date-times as **local time** — a latent bug that's invisible only because the cockpit never calls these endpoints (MCP renders them as text).
3. `/graph/changes` (`graph_routes.py:157-161`) → `.isoformat()`, no Z.

**Postgres effect**: asyncpg returns tz-aware datetimes for timestamptz → FastAPI default = `…+00:00` with 6-digit microseconds. All cockpit `new Date()` parse sites handle both `Z` and `+00:00` (`data.service.ts:208, :211, :368, :375`; `chat-history.component.ts:755`; `agent-activity.component.ts:867`) — **parsing is safe**. The naive/no-suffix local-time bug actually gets FIXED if the adapter returns aware datetimes.

**Real risks**:
- **Lexicographic comparisons**: `indexed-db.service.ts:512-520` (`minTimestamp`/`maxTimestamp` use `<`/`>` on strings) and the Dexie `chatEntries [jobId+timestamp]` compound index (`:43`, read at `:267-272`) sort timestamps as strings. Mixed `…Z` (old cache) vs `…+00:00` (new) entries inside one job mis-order at same-millisecond granularity; mitigated by the wipe, but the new adapter must emit ONE canonical format for ALL endpoints.
- **Cross-endpoint consistency is load-bearing**: `visibleChatEntries` (`data.service.ts:200-212`) joins chat entries to the audit slider position by comparing `new Date(chat.timestamp)` against `new Date(audit.timestamp)`. If the adapter ever serialized one table aware (+00:00) and another naive (no suffix → parsed local), the pane filters by a timezone-offset-shifted clock. Keep audit + chat + graph on identical serialization.
- **Precision diff**: Mongo BSON = millis (`.789000` padded); PG = true micros (`.789012`). Cosmetic for parsing; the roadmap P6 byte-diff harness must normalize sub-second precision and suffix (already flagged there as "timestamp precision" — confirmed real).
- **`/audit/timerange` + `lastUpdate`** shapes are plain `{start,end}` / ISO strings — only MCP text consumers + the unused deprecated cockpit method; low risk.
- Recommendation: serialize everything as UTC with `Z` suffix and fixed 3-digit millis (i.e., today's `_to_iso_utc` aware branch, `mongodb.py:34`) in the new adapter — matches the dominant existing cockpit-visible format, keeps lexicographic == chronological, and minimizes diff noise in P6.

## Doc-vs-code mismatch summary (code wins)

1. **Cache "version bump" mechanism is fictional** — `JobCacheMetadata.version` written (`indexed-db.service.ts:236/:304/:377`), never read; invalidation is `auditEntryCount` equality only (`data.service.ts:296-297`). Doc/roadmap P4+P6 rely on it. Needs a real check or Dexie upgrade-clear.
2. **`request_id` (audit.model.ts:43, chat.model.ts:63) missing from the doc's cockpit change list** — it's an ObjectId carrier with two runtime-fatal `.slice()` template calls and a regex-gated lookup path.
3. **`formatters.py:491/:574` consume the `_id` field name** — doc says formatters are "already clean"; true only for the 24-hex descriptions, breaks silently under the doc's own rename recommendation.
4. **`cache.model.ts:30`** in the doc points at the `CachedChatEntry.id` comment; the actual version constant to bump is `indexed-db.service.ts:17` (and per #1, bumping alone does nothing).
5. Minor drift: third N+1 enricher now at `main.py:18816-18819` (doc: 18740-18742; main.py locally modified).
6. `mongodb.py:471` `get_page_for_timestamp` is dead code — correctly absent from the doc's adapter surface, safe to drop.
7. The doc's "all reads by job_id" and endpoint inventory otherwise verified accurate, including the `/graph/changes` raw-cursor leak (`graph_routes.py:142`) and all four ObjectId description sites.

---

# 4. Infrastructure contract

## Postgres Audit Store — Definitive Infrastructure Change List (verified against code 2026-06-10)

Scope note: `src/tools/mongodb/` (customer-datasource tools) is out of scope; boundary markers are noted where its surfaces touch infra (pymongo dep, workspace NetPol 27017, DEFAULT_DS_MONGODB_*).

### 1. NEW: /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/databases/postgres-audit.yaml

Copy source: **`postgres-vector.yaml` is the truer template**, not `postgres-keycloak.yaml` as the design doc says. Reasons (code):
- `postgres-vector.yaml:1` uses the 2-flag conditional `{{- if and .Values.databases.vector.enabled .Values.databases.vector.internal }}` the roadmap wants (keycloak's is the 4-flag `keycloak.enabled && keycloak.internal && databases.keycloak.enabled && databases.keycloak.internal`, `postgres-keycloak.yaml:1`).
- `postgres-vector.yaml:54-69` already demonstrates the exact env pattern auditdb needs: container `POSTGRES_USER`/`POSTGRES_PASSWORD` from `secretKeyRef` keys `VECTOR_POSTGRES_USER`/`VECTOR_POSTGRES_PASSWORD`, `POSTGRES_DB` from `configMapKeyRef` key `VECTOR_POSTGRES_DB`. Audit mirrors with `AUDIT_POSTGRES_*` keys.
- `postgres-keycloak.yaml:58-66` instead hardcodes user/db (`keycloak`) and uses `KC_DB_PASSWORD` — wrong pattern for audit.
Structure to replicate: PVC with `helm.sh/resource-policy: keep` (vector:16-17), StatefulSet `{{fullname}}-auditdb`, ClusterIP Service port 5432 (vector:90-103), `pg_isready` probes (vector:73-82; note pgvector's probe hardcodes `-U srw` even though the user is Secret-driven — harmless, pg_isready doesn't authenticate). Component label `auditdb` (7 chars, fits the 52-char StatefulSet budget per keycloakdb's comment, postgres-keycloak.yaml:10-12). Image: design doc wants `postgres:15`-class or `pgpartman/pgpartman:15` depending on gate G0 (stock postgres:15 lacks pg_partman — roadmap P5 risk confirmed plausible, not code-verifiable here).

### 2. /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/databases/network-policies.yaml — CRITICAL CHECK 1

Current shape: one Ingress-only policy per internal DB. Main postgres policy (lines 18-51) allows `from` podSelectors: orchestrator (33-35), agent (36-38), llm-seed (39-41), pgadmin-if-enabled (42-46), port 5432. pgvector policy (53-78): orchestrator + agent only. mongodb policy (80-110): orchestrator + agent + mongo-express-if-enabled, port 27017. keycloakdb (142-163): keycloak only. Selectors are `srw.componentSelectorLabels` = `app.kubernetes.io/name` + `app.kubernetes.io/instance` + `app.kubernetes.io/component` (_helpers.tpl:133-136).

How dynamic agent pods satisfy these: `agent_provisioner.py:1018-1035` stamps `app.kubernetes.io/name` from env `AGENT_LABEL_NAME`, `app.kubernetes.io/instance` from `AGENT_LABEL_INSTANCE`, and `app.kubernetes.io/component: agent` (only when at least one label env is set). Those envs are injected at orchestrator/deployment.yaml:803-810 (`AGENT_LABEL_NAME` = `include "srw.name"`, `AGENT_LABEL_INSTANCE` = `.Release.Name`) with an explicit comment that DB NetworkPolicies depend on them. Env reads at agent_provisioner.py:84-86.

**What the new auditdb policy needs**: clone the pgvector block — podSelector `component=auditdb`, ingress `from` orchestrator + agent component selectors, port 5432/TCP. The agent allowance is mandatory: the writer (`LLMArchiver` in `src/core/archiver.py`) runs in the agent pod and will open direct connections to `srw-auditdb:5432` (today it talks straight to mongo, which is why agent is on the mongo policy at network-policies.yaml:98-100). Optionally add the pgadmin conditional block (mirroring 42-46) if the auditdb should be registrable in pgadmin — note pre-existing inconsistency: pgadmin today can only reach main postgres, not pgvector, and `optional/pgadmin.yaml` does no server pre-population at all (just `PGADMIN_DEFAULT_PASSWORD` from `POSTGRES_PASSWORD`, pgadmin.yaml:40-44), so the design doc's "pgadmin only pre-populates srw-postgres" overstates — registration is fully manual either way.

Post-cutover deletion: the mongodb policy block (80-110) goes with the StatefulSet.

Latent gap found: `orchestrator/services/persistent_provisioner.py` (fallback path, used at main.py:1936-1937 when agent_provisioner unavailable) builds pods with envFrom (persistent_provisioner.py:502-509) but **without** the chart labels — zero hits for `AGENT_LABEL`/`app.kubernetes.io` in that file. Pods from that path are blocked by every DB NetworkPolicy today (postgres/pgvector/mongo) and will be blocked from auditdb too. Pre-existing, consistent, but worth knowing when copying the policy.

Boundary: `workspace-network-policy.yaml:168-175` egress "MongoDB (for datasource shell tools)" targets `podSelector` component=mongodb on port 27017 — i.e. it only ever reaches the **bundled** mongo pod. The design doc says this egress "must stay" for customer mongosh; in fact, once `databases/mongodb.yaml` is deleted the selector matches nothing (dead rule), and external customer Mongo on 27017 is blocked by the workspace tier allowlist (internet egress is TCP 80/443/22 only) regardless. Keep-or-delete is cosmetic; the doc's rationale is wrong.

### 3. /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/orchestrator/deployment.yaml

- Reloader: `reloader.stakater.com/auto: "true"` annotation rendered when `.Values.reloader.enabled` (deployment.yaml:7-10; default true at values.yaml:918-919). **Yes — adding/altering keys on the referenced `srw` Secret or ConfigMap bounces the orchestrator automatically.** Dynamic agent pods are not Deployments and are not Reloader-managed; they pick up new env only on next pod creation (pool churn).
- initContainers block 26-51: `wait-for-mongodb` at **37-41** (gated `databases.mongodb.enabled && internal`, nc to `{{fullname}}-mongodb 27017`). Replace with `wait-for-auditdb` gated `databases.audit.enabled && internal`, `nc -z {{fullname}}-auditdb 5432` (mirror wait-for-pgvector at 32-36).
- env: `POSTGRES_*` component-parts block at **80-104** (USER/PASSWORD secretKeyRef, HOST/PORT/DB configMapKeyRef; rationale comment 76-79), `VECTOR_POSTGRES_*` at **105-129**, `MONGODB_URL` configMapKeyRef at **130-134**. Replace 130-134 with five `AUDIT_POSTGRES_{USER,PASSWORD,HOST,PORT,DB}` entries mirroring 105-129. The orchestrator uses an explicit env list (no envFrom) — these five entries are mandatory here, unlike agents.
- If the env refs are non-`optional`, a missing Secret key wedges the pod in CreateContainerConfigError — see Risks for ordering.

### 4. /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/configmap.yaml

- Add `AUDIT_POSTGRES_HOST/PORT/DB` mirroring the `VECTOR_POSTGRES_*` trio at lines 19-21 (helpers: add `srw.auditPostgresHost/Port/Db` beside `srw.vectorPostgres*` at _helpers.tpl:456-478, internal host `{{fullname}}-auditdb`, default db e.g. `srw_audit`).
- Drop `MONGODB_URL` at lines **30-31** post-cutover. Note: this ConfigMap is the same one agents inherit wholesale via envFrom, so `MONGODB_URL` currently reaches agent pods through it (feeds `archiver.from_env`, src/core/archiver.py:209) and `AUDIT_POSTGRES_HOST/PORT/DB` will flow to agents the same way with no further change.

### 5. Secrets — CRITICAL CHECK 2 (ESO/Vault)

- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/external-secret.yaml:19-21 — `dataFrom: - extract: key: {{ .Values.externalSecrets.vaultPath }}` bulk-projects the entire Vault bundle. **Confirmed: zero template changes for new keys; `AUDIT_POSTGRES_USER`/`AUDIT_POSTGRES_PASSWORD` (and optional external-mode `AUDIT_DB_URL`) just need to exist in the Vault path.** Target Secret name = `srw.secretName` (external-secret.yaml:17), which resolves existingSecret-override → fullname (_helpers.tpl:59-65); vaultPath knob at values.yaml:323. RefreshInterval default "1h" (values.yaml:320) — see Risks for the upgrade-before-sync window.
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/secret.yaml (chart-create dev mode): renders only keys present in `.Values.secrets.values` (lines 41-45) — no template change, but dev overlays must add the two keys (see §10).
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/README.md § "Secret schema" line 192: add `AUDIT_POSTGRES_USER`/`AUDIT_POSTGRES_PASSWORD` to the DB-credentials list (after CITATION pair at 212-214) + the skeleton env at 257-269; external-mode note at 226-228 already documents the externalHost/Port/Db convention the audit block should follow. Also update component table line 31 (`databases.mongodb | Audit trail`), line 5, 36 (admin UIs), 54 (managed DBs), 61, 97 (stale: claims `databases.*.externalUrl` for Postgres/vector — wrong since the component-parts cutover), 123 (hostname list incl. `mongo`).

### 6. /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/_helpers.tpl

- Add `srw.auditPostgresHost/Port/Db` beside vector helpers (456-478) using `databases.audit.internal` / `externalHost|externalPort|externalDb`.
- Post-cutover delete: `srw.mongoHost` **408-410**, `srw.mongodbUrl` **480-489** (comment 480-482, define 483-489; doc's "481-487" is ~right).

### 7. /home/ghost/Repositories/Superhuman-Remote-Worker/helm/values.yaml (+ example/CI values)

- Add `databases.audit` block mirroring `databases.vector` (364-381): `{enabled, internal, externalHost, externalPort, externalDb, image, storageClass, storageSize, resources}`. **Doc mismatch**: design doc's file-by-file list says `databases.audit.{...externalUrl...}` — chart convention is component parts (externalHost/Port/Db, values.yaml:345-353 rationale comment); the doc's own helpers section says so. Use parts.
- Post-cutover delete: `global.hostnames.mongo` line **61**; `databases.mongodb` block **404-418** (doc said 404-409 — drifted); `mongoExpress` block **881-890** (doc said 866-868 — drifted).
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/values.example.yaml: mongodb external block **80-83** → replace with `databases.audit` external example; mongoExpress **126-127** delete. **Pre-existing staleness found (flag, don't copy)**: postgres/vector entries at 68/72 still use `externalUrl`, which the helpers ignore (`required ... externalHost`, _helpers.tpl:436/460); also stale `llm.baseUrl/visionModel/embeddingModel` (139-141) and `headscale.enabled` (123). Fix while touching.
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/ci/customer-external-values.yaml: mongodb block **45-48** → `databases.audit` external; mongoExpress **74-75** delete; same stale `externalUrl` at 40/44.
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/ci/test-values.yaml: `mongoExpress.enabled: true` at **19-20** must be removed in P8 or main.yml's blocking helm lint renders a template referencing deleted values. (Verified live: `helm lint` does NOT fail on missing `required` values — they surface as engine.go INFO lines and lint passes — so lint is a weak gate; it will only catch hard template errors like references to deleted named templates.)

### 8. Other helm templates (post-cutover removals)

- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/databases/mongodb.yaml — delete whole file (PVC+StatefulSet+Service, conditional line 1).
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/optional/mongo-express.yaml — delete (includes its own `wait-for-mongodb` initContainer at 26-31).
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/ingress.yaml — `$mongoHost` assignment line **11**; mongo-express Ingress block **443-484** (doc said 443-481; block ends 484).
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/cockpit/deployment.yaml:**17** — `window['env']['mongoExpressUrl']` env-init line.
- /home/ghost/Repositories/Superhuman-Remote-Worker/helm/templates/mcp/deployment.yaml — verified: zero mongo references, nothing to do (doc claim confirmed).
- Full helm mongo-touching file inventory (grep-verified): databases/mongodb.yaml, databases/network-policies.yaml, configmap.yaml, _helpers.tpl, ingress.yaml, cockpit/deployment.yaml, orchestrator/deployment.yaml, optional/mongo-express.yaml, workspace-network-policy.yaml. NOTES.txt and Chart.yaml are clean.
- helm/templates/agent/ contains only `pdb.yaml` + `service.yaml` — confirmed no agent deployment template; agent env arrives via envFrom (agent_provisioner.py:1060-1063, names from `AGENT_CONFIGMAP`/`AGENT_SECRET` envs, provisioner lines 66-67, set at orchestrator/deployment.yaml:788-791).

### 9. CRITICAL CHECK 3 — init.py inventories

/home/ghost/Repositories/Superhuman-Remote-Worker/orchestrator/init.py (audit-store Mongo, all to be replaced/dropped):
- `get_mongodb_url()` reads `MONGODB_URL` at **1485-1487**; "not set" log lines 1515, 1656, 1703 (doc's list matches).
- `_parse_mongodb_url` **1490-1500** (feeds mongodump/mongorestore args).
- `init_mongodb` **1502-1571** (collections bootstrap); `_create_mongodb_indexes` **1573-1609** — imports `MONGODB_INDEX_DECLARATIONS` at **1590** (doc said path 1576-1593 ✓).
- `verify_mongodb` **1612-1643** (doc missed this one).
- `backup_mongodb` **1645-1689** (mongodump); `restore_mongodb` **1692-1757** (mongorestore; doc said ends 1740 — extends to ~1757).
- Orchestration the doc under-counted: `initialize(skip_mongodb=...)` **1889/1910-1915**; `verify_databases` **1936/1953-1954**; backup driver **1980-1985**; restore driver **2013-2022**; CLI usage/flag/reporting **19, 2041, 2058-2060, 2096, 2109-2119, 2170**.
- STAYS (customer datasource): `DEFAULT_DS_MONGODB_URL/NAME/READ_ONLY` seeding at **701, 737-746**.

/home/ghost/Repositories/Superhuman-Remote-Worker/init.py (root): docstring 5, 23; `initialize(skip_mongodb=...)` 129-151, import 161, init step 177-182, passthrough 232; `verify` 242-287 (import 246, verify call 279-287); backup import 327 + driver 368-375; restore import 427 + driver 471-478; CLI 514, 530, 543-545, 630. Doc's list (23, 129-151, 177, 232-279, 514, 543, 630) missed 161, 280-287, 327/368-375, 427/471-478, 530.

**New finding the docs miss entirely**: /home/ghost/Repositories/Superhuman-Remote-Worker/docker/Dockerfile.orchestrator installs only `libpq5, curl, openssh-client` runtime (lines 50-55) — **no mongodump/mongorestore today (the in-container backup path is already dead) and no `pg_dump`/`pg_restore` for the planned replacement**. The pg_dump swap requires adding `postgresql-client` to the runtime apt list (and pinning a client major ≥ server major for partitioned-dump correctness). Also `MONGODB_URL` doc-comment at Dockerfile.orchestrator:13.

### 10. CRITICAL CHECK 4 — utils/db_url.py contract

Two byte-identical implementations (intentionally duplicated so the orchestrator image doesn't bundle src/): /home/ghost/Repositories/Superhuman-Remote-Worker/orchestrator/utils/db_url.py:21-54 and /home/ghost/Repositories/Superhuman-Remote-Worker/src/utils/db_url.py:18-51.

Signature: `def build_postgres_url(prefix: str = "POSTGRES", *, fallback_env: Optional[str] = None, default_host: Optional[str] = None, default_port: int = 5432, default_db: Optional[str] = None) -> Optional[str]`

Contract: reads `<prefix>_USER`/`<prefix>_PASSWORD` (Secret) and `<prefix>_HOST`/`<prefix>_PORT`/`<prefix>_DB` (ConfigMap); URL-quotes user+password with `safe=""`; if user+password aren't both set, returns `os.getenv(fallback_env)`; returns None if neither layout configured. So `build_postgres_url("AUDIT_POSTGRES", fallback_env="AUDIT_DB_URL")` works as designed in both processes — agent-side gate replacement for archiver.py:209 must use the **src/** copy.

### 11. CRITICAL CHECK 5 — PostgresDB / migrations / lifespan slot

- /home/ghost/Repositories/Superhuman-Remote-Worker/orchestrator/database/postgres.py:169-170 — `MIGRATIONS_APP_DIR` / `MIGRATIONS_VECTOR_DIR` constants; add `MIGRATIONS_AUDIT_DIR = Path(__file__).parent / "migrations" / "audit"` here.
- `PostgresDB.__init__(connection_string=None, min_connections=None, max_connections=None, command_timeout=None, migrations_dir: Optional[Path] = None)` (postgres.py:275-325); default connection string composed via `build_postgres_url("POSTGRES", fallback_env="DATABASE_URL")` (304-311); pool sizes from `POSTGRES_MIN/MAX_CONNECTIONS` envs (313-318) — note these env knobs are **shared** across all PostgresDB instances, so the design's "throughput-tuned audit pool" needs explicit constructor args, not envs.
- `apply_migrations()` at postgres.py:**7369-7407**: thin wrapper over `run_migrations(self._pool, self._migrations_dir)` (7398) with dual import (`orchestrator.database.migrate` host-side / `database.migrate` in-container, 7385-7393 — the orchestrator image flattens to /app); **caveat at 7404-7405**: it additionally calls `migrate_existing_users_verified()` when `_migrations_dir == MIGRATIONS_APP_DIR` — harmless for an audit instance (different dir) but confirms instance-binding is the dispatch mechanism. `run_migrations` signature in migrate.py:110-112 takes `(pool, migrations_dir, dry_run=...)`; discovery glob `[0-9][0-9][0-9][0-9]_*.sql` (migrate.py:76).
- Wiring sites in /home/ghost/Repositories/Superhuman-Remote-Worker/orchestrator/main.py: import of `MIGRATIONS_VECTOR_DIR` at **99** (add AUDIT), instances at **236-255** (`postgres_db = PostgresDB()` 236; `mongodb = MongoDB()` 237; vector DSN via `_build_pg_url("VECTOR_POSTGRES", fallback_env="VECTOR_DB_URL")` 245 with hard `RuntimeError` if missing 246-251 — **decide deliberately whether audit copies this hard-fail or degrades to no-op like Mongo does today**; `vector_db = PostgresDB(connection_string=..., migrations_dir=MIGRATIONS_VECTOR_DIR)` 252-255 is the exact pattern for the audit pool). Lifespan: connects at **3323-3325** (postgres, vector, mongo), `mongodb.ensure_indexes()` at **3332** (no PG analogue — deleted on cutover), `apply_migrations()` for app+vector at **3339-3340** — the third audit `connect()` + `apply_migrations()` slots into this exact block, before traffic. `set_mongodb(mongodb)` share at **3391**; shutdown `mongodb.disconnect()` at **3672**.
- graph_routes raw-cursor leak confirmed: /home/ghost/Repositories/Superhuman-Remote-Worker/orchestrator/graph_routes.py:20 `set_mongodb`, **line 143** `collection = mongodb._db["agent_audit"]` inside `_get_all_tool_calls` (sorts by `step_number`, 152).
- Endpoint anchors (current tree; main.py is locally modified so doc refs drifted): `/audit` @8390 (checks 8418/8431), `/requests/{doc_id}` @8444 with `doc_id: str` at **8445** (→ int), `/audit/timerange` @8479, `/chat` @8496, `/audit/bulk` @9311, `/chat/bulk` @9348, `/graph/bulk` @9385, `/version` @9422, `/llm-requests` @**14623** (avail-check 14637 — doc said 14563/14572), N+1 enrichers @3933-3936, 3953-3954, and **18814-18816** (doc said 18740-18742). MongoDB import at main.py:96; graph_routes import 227.
- Writers/readers being swapped (for completeness): src/core/archiver.py `from_env` gates on `MONGODB_URL` at **209-211**; src/database/mongo_db.py:64 reads `MONGODB_URL`; orchestrator/database/mongodb.py — `MONGODB_INDEX_DECLARATIONS` at **80**, `ensure_indexes` at **228**, env fallback at 162. Package exports: src/database/__init__.py:41/47, orchestrator/database/__init__.py:43/55.

### 12. CRITICAL CHECK 6 — CI

- /home/ghost/Repositories/Superhuman-Remote-Worker/.github/workflows/main.yml: test-deps install at **244** (`uv pip install --system -r requirements.txt -r orchestrator/requirements.txt pytest pytest-asyncio`), `pytest tests/ -x -q --tb=short` at 246 — **no database available; tests are pure-unit**. Helm lint (blocking) at **65-68** with both ci values files. Chart packaging at 876-925.
- /home/ghost/Repositories/Superhuman-Remote-Worker/.github/workflows/develop.yml: same install at **469**, pytest 471; helm lint at 96-101 is `continue-on-error: true` (non-blocking). test-python gate `python-changed` filter diff paths at **302-305**: `src/ orchestrator/ tests/ config/ agent.py requirements.txt orchestrator/requirements.txt` — **`orchestrator/database/migrations/audit/` is already covered by `orchestrator/`** (no filter change needed for the migrations dir, contra the worry), and orchestrator image build path filter includes `orchestrator/` (374-375), agent image includes `src/ config/ agent.py requirements.txt` (371-372). **Gap: a new root `requirements-dev.txt` is NOT in any filter** — add it to the test-paths diff list at 302-305 (and to the two install lines per the design doc).
- **No migration dry-run gate exists anywhere in CI** (migrate.py has a `--dry-run` CLI at migrate.py:319, unused by workflows). Audit unit tests (testcontainers) would run inside the existing test-python jobs — note GH-hosted ubuntu-latest has Docker, but the current jobs assume no services; testcontainers adds image-pull time to both workflows.
- helm lint verified live (helm v3.20): missing `required` values print as INFO and lint still passes (`0 chart(s) failed`) — so neither workflow truly render-gates the chart. If you want the roadmap's "three render shapes" enforced, add `helm template` runs to CI.

### 13. CRITICAL CHECK 7 — Local dev loop

- /home/ghost/Repositories/Superhuman-Remote-Worker/Tiltfile: zero mongo references; no DB port-forwards; deploys the whole chart via `helm_resource(chart='./helm', '--values=deployment/values-local.yaml', '--values=deployment/values-tilt.yaml')` (282-288). The auditdb StatefulSet appears automatically on the next `tilt up`/`helm upgrade`; no Tiltfile change needed.
- /home/ghost/Repositories/Superhuman-Remote-Worker/scripts/local-dev-up.sh and scripts/local-dev-tilt-up.sh: zero mongo references, no changes.
- /home/ghost/Repositories/Superhuman-Remote-Worker/deployment/values-local.example.yaml: developer must (a) add `AUDIT_POSTGRES_USER`/`AUDIT_POSTGRES_PASSWORD` to `secrets.values` in the "App DBs" block at **91-97** (this overlay uses `secrets.create: true`, line 72-73 — keys absent ⇒ Secret keys absent ⇒ orchestrator+auditdb pods wedge on secretKeyRef unless marked optional), and (b) optionally add `databases.audit.storageSize` to the trim block at **196-206**; post-cutover remove `databases.mongodb.storageSize` at **201-202** (the only mongo reference in the file). Existing `values-local.yaml` copies on dev machines need the same hand-edit — a silent upgrade-breaker for every dev.

### 14. CRITICAL CHECK 8 — Monitoring / health

- Orchestrator `/api/health` (main.py:**3843-3846**) returns static `{"status": "ok"}` — no DB checks, no mongo; k8s probes hit it (orchestrator/deployment.yaml:904-915). Nothing to change.
- No other health/status endpoint references mongo (workspace status at 3849+ is filesystem-only). Agent-side `/health` (src/api/dual_app.py:587) has no mongo.
- Cockpit env-init: only `mongoExpressUrl` at helm/templates/cockpit/deployment.yaml:**17** (delete in P8). cockpit/README.md mongo refs at 30, 49, **126** (`MONGODB_URL`). orchestrator/README.md:66 has a `MONGODB_URL` example line (doc cleanup).

### 15. Dependencies (post-cutover)

- /home/ghost/Repositories/Superhuman-Remote-Worker/orchestrator/requirements.txt:8-9 — `asyncpg>=0.30.0` already present; `motor>=3.3.0` deletable in P8 (pymongo reaches the orchestrator image only transitively via motor; dropping motor also kills orchestrator/init.py's `from pymongo import MongoClient` path — fine, it's being replaced).
- /home/ghost/Repositories/Superhuman-Remote-Worker/requirements.txt:5,62 — `asyncpg>=0.29.0` present; `pymongo>=4.6.0` **stays** (customer datasource tools `src/tools/mongodb/` — gate G5 confirmed by code: that's the only remaining consumer post-cutover; add the keep-comment).
- Memory-file rule "orchestrator deps go in both requirements files" applies to anything new the audit adapter needs (it shouldn't — asyncpg is in both).

### 16. Compose (gate G6 status)

All three files still exist: /home/ghost/Repositories/Superhuman-Remote-Worker/docker-compose.yaml, docker-compose.dev.yaml, docker-compose.local.yaml. The G6 decision (deprecate first vs. mirror `postgres-audit` into all three) remains open; if kept, mirror the keycloakdb compose shape + dev port 5434, and replace `MONGODB_URL` envs/depends_on per the design doc.
